/*
 * hpsdr_fast.c — C receiver for HPSDR Protocol 1 (Red Pitaya)
 *
 * Runs a dedicated receive thread that parses UDP packets at wire speed.
 * Python pulls IQ samples per receiver via hpsdr_drain().
 *
 * Replaces the Python receive loop which caps at ~1524 pkt/s.
 * 8 receivers at 192 kHz requires ~9600 pkt/s — trivial for C.
 *
 * Compile:
 *   gcc -shared -O2 -fPIC -pthread -o libhpsdr_fast.so hpsdr_fast.c
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <pthread.h>
#include <dlfcn.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <time.h>
#include <math.h>

#define MAX_RX        8
typedef void (*sc_feed_iq_fn)(void *, const double *, const double *, int);

#define RING_SIZE     (192000 * 4)   /* 4 seconds per receiver at 192 kHz */
#define PKT_SIZE      1032
#define FRAME_SIZE    512
#define IQ_DATA_SIZE  504
#define COOKIE_0      0xEF
#define COOKIE_1      0xFE

typedef struct {
    double i[RING_SIZE];
    double q[RING_SIZE];
    volatile int write_pos;
    volatile int read_pos;
} RxRing;

typedef struct {
    int sock;
    int n_receivers;
    int sample_rate;
    int running;
    uint32_t sdr_ip;
    uint16_t sdr_port;
    uint16_t listen_port;
    uint32_t seq_tx;
    uint32_t frequencies[MAX_RX];
    int lna_gain;

    RxRing rx[MAX_RX];
    pthread_t thread;
    pthread_mutex_t lock;

    uint8_t rx_enabled[MAX_RX]; /* only store samples for enabled receivers */

    /* FT8 raw IQ ring — stores full 192 kHz IQ for Python extraction.
     * Only enable on bands with FT8 activity (2-3 max).
     * 65 seconds × 192 kHz × sizeof(float) × 2 = 100 MB per band. */
    #define FT8_RAW_SIZE (192000 * 65)
    struct {
        float *i;       /* heap allocated — too large for struct */
        float *q;
        volatile int write_pos;
        int enabled;
        double freq_hz;
        double center_hz;
    } ft8_raw[MAX_RX];

    /* Per-receiver scanner handles for worker thread */
    void *scanners[MAX_RX];         /* ItilaSc* per enabled receiver */
    sc_feed_iq_fn scanner_feed;     /* itila_sc_feed_iq function pointer */
    double iq_scale;                /* 8388608.0 */
    int window_samples;             /* ITILA decode window size */

    /* Decode function pointer (itila_sc_decode_ready) */
    int (*scanner_decode)(void *sc, int window_samples, void *results, int max);

    /* Result ring buffer — worker writes, Python reads */
    #define RESULT_MAX 256
    #define RESULT_SIZE 280   /* ScDecodeResult: 8+8+4+4+256 bytes */
    uint8_t result_buf[RESULT_MAX * RESULT_SIZE];
    volatile int result_write;
    volatile int result_read;
    volatile int result_count;
    pthread_mutex_t result_lock;

    /* FT8 channelizer per band — 2-stage decimation like CW */
    #define FT8_RATE 4000
    #define FT8_DEC1 16       /* 192000 → 12000 */
    #define FT8_DEC2 3        /* 12000 → 4000 */
    #define FT8_WIN  (FT8_RATE * 60)  /* 60 seconds at 4000 sps */
    #define FT8_S1_LEN 32
    #define FT8_S2_LEN 32
    struct {
        int    enabled;
        double ft8_freq_hz;
        double center_hz;
        float  buf_i[FT8_RATE * 60];
        float  buf_q[FT8_RATE * 60];
        int    buf_n;
        double mix_phase;
        /* Stage 1: 192k→12k FIR delay line */
        double s1_dl_i[FT8_S1_LEN];
        double s1_dl_q[FT8_S1_LEN];
        int    s1_pos;
        int    s1_count;
        /* Stage 2: 12k→4k FIR delay line */
        double s2_dl_i[FT8_S2_LEN];
        double s2_dl_q[FT8_S2_LEN];
        int    s2_pos;
        int    s2_count;
    } ft8[MAX_RX];
    int  ft8_last_slot;         /* last 15-second slot we decoded */
    char ft8d_path[256];        /* path to ft8d binary */

    /* Worker thread: drains ring buffers, feeds scanners, decodes */
    pthread_t worker_thread;
    int worker_running;

    /* stats */
    volatile uint64_t pkt_count;
    volatile uint64_t drop_count;
} HpsdrFast;

/* ---- ring buffer helpers ---- */
static inline int ring_avail(const RxRing *r) {
    int d = r->write_pos - r->read_pos;
    return d >= 0 ? d : d + RING_SIZE;
}

static inline int ring_free(const RxRing *r) {
    return RING_SIZE - 1 - ring_avail(r);
}

/* ---- send EP2 packet ---- */
static void send_ep2(HpsdrFast *h, const uint8_t *c0c4_f1, const uint8_t *c0c4_f2) {
    uint8_t pkt[PKT_SIZE];
    memset(pkt, 0, PKT_SIZE);
    pkt[0] = COOKIE_0; pkt[1] = COOKIE_1;
    pkt[2] = 0x01; pkt[3] = 0x02;
    pkt[4] = (h->seq_tx >> 24) & 0xFF;
    pkt[5] = (h->seq_tx >> 16) & 0xFF;
    pkt[6] = (h->seq_tx >>  8) & 0xFF;
    pkt[7] = (h->seq_tx      ) & 0xFF;
    h->seq_tx++;

    /* Frame 1 */
    pkt[8] = 0x7F; pkt[9] = 0x7F; pkt[10] = 0x7F;
    memcpy(pkt + 11, c0c4_f1, 5);

    /* Frame 2 */
    pkt[520] = 0x7F; pkt[521] = 0x7F; pkt[522] = 0x7F;
    memcpy(pkt + 523, c0c4_f2, 5);

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = h->sdr_ip;
    addr.sin_port = htons(h->sdr_port);
    sendto(h->sock, pkt, PKT_SIZE, 0, (struct sockaddr *)&addr, sizeof(addr));
}

/* ---- parse one IQ frame (504 bytes) ---- */
static void parse_frame(HpsdrFast *h, const uint8_t *iq_data, int n_rx) {
    /* Each group: n_rx × 6 bytes IQ + 2 bytes mic padding */
    int group_size = n_rx * 6 + 2;
    int n_groups = IQ_DATA_SIZE / group_size;

    for (int g = 0; g < n_groups; g++) {
        const uint8_t *gp = iq_data + g * group_size;
        for (int rx = 0; rx < n_rx; rx++) {
            if (!h->rx_enabled[rx]) continue;

            const uint8_t *s = gp + rx * 6;

            int32_t iv = (s[0] << 16) | (s[1] << 8) | s[2];
            if (iv >= 0x800000) iv -= 0x1000000;
            int32_t qv = (s[3] << 16) | (s[4] << 8) | s[5];
            if (qv >= 0x800000) qv -= 0x1000000;

            double fi =  (double)iv / 8388608.0;
            double fq = -(double)qv / 8388608.0;  /* negate Q (Pitaya convention) */

            RxRing *r = &h->rx[rx];
            if (ring_free(r) < 1) {
                h->drop_count++;
                continue;
            }
            int wp = r->write_pos;
            r->i[wp] = fi;
            r->q[wp] = fq;
            r->write_pos = (wp + 1) % RING_SIZE;

            /* FT8: store raw 24-bit IQ (not normalized) for Python extraction */
            if (h->ft8_raw[rx].enabled && h->ft8_raw[rx].i) {
                int fwp = h->ft8_raw[rx].write_pos;
                int idx = fwp % FT8_RAW_SIZE;
                h->ft8_raw[rx].i[idx] = (float)iv;  /* raw 24-bit integer */
                h->ft8_raw[rx].q[idx] = -(float)qv; /* negate Q */
                h->ft8_raw[rx].write_pos = fwp + 1;
            }
        }
    }
}

/* ---- receive thread ---- */
#define BATCH_SIZE 64

static void *recv_thread(void *arg) {
    HpsdrFast *h = (HpsdrFast *)arg;
    uint8_t bufs[BATCH_SIZE][2048];
    struct iovec iovecs[BATCH_SIZE];
    struct mmsghdr msgs[BATCH_SIZE];

    memset(msgs, 0, sizeof(msgs));
    for (int i = 0; i < BATCH_SIZE; i++) {
        iovecs[i].iov_base = bufs[i];
        iovecs[i].iov_len = sizeof(bufs[i]);
        msgs[i].msg_hdr.msg_iov = &iovecs[i];
        msgs[i].msg_hdr.msg_iovlen = 1;
    }

    struct timespec timeout = { .tv_sec = 0, .tv_nsec = 50000000 }; /* 50ms */

    while (h->running) {
        int n_msgs = recvmmsg(h->sock, msgs, BATCH_SIZE, MSG_WAITFORONE, &timeout);
        if (n_msgs <= 0) continue;

        for (int m = 0; m < n_msgs; m++) {
            uint8_t *buf = bufs[m];
            int len = msgs[m].msg_len;
            if (len < PKT_SIZE) continue;
            if (buf[0] != COOKIE_0 || buf[1] != COOKIE_1 ||
                buf[2] != 0x01 || buf[3] != 0x06) continue;

            h->pkt_count++;
            parse_frame(h, buf + 16, h->n_receivers);
            parse_frame(h, buf + 528, h->n_receivers);
        }
    }
    return NULL;
}

/* ---- public API ---- */

HpsdrFast *hpsdr_create(const char *ip, int port, int n_receivers,
                         int sample_rate, int lna_gain) {
    HpsdrFast *h = (HpsdrFast *)calloc(1, sizeof(HpsdrFast));
    if (!h) return NULL;

    h->n_receivers = n_receivers > MAX_RX ? MAX_RX : n_receivers;
    h->sample_rate = sample_rate;
    h->lna_gain = lna_gain;
    h->sdr_ip = inet_addr(ip);
    h->sdr_port = port;
    h->listen_port = port;
    h->seq_tx = 0;

    for (int i = 0; i < MAX_RX; i++)
        h->frequencies[i] = 7000000;

    h->sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (h->sock < 0) { free(h); return NULL; }

    int optval = 1;
    setsockopt(h->sock, SOL_SOCKET, SO_REUSEADDR, &optval, sizeof(optval));
    int rcvbuf = 32 * 1024 * 1024;
    setsockopt(h->sock, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));

    struct sockaddr_in bind_addr;
    memset(&bind_addr, 0, sizeof(bind_addr));
    bind_addr.sin_family = AF_INET;
    bind_addr.sin_addr.s_addr = INADDR_ANY;
    bind_addr.sin_port = htons(h->listen_port);
    if (bind(h->sock, (struct sockaddr *)&bind_addr, sizeof(bind_addr)) < 0) {
        close(h->sock);
        free(h);
        return NULL;
    }

    /* No socket timeout — recvmmsg uses its own timeout */

    pthread_mutex_init(&h->lock, NULL);
    return h;
}

void hpsdr_set_freq(HpsdrFast *h, int rx_index, uint32_t freq_hz) {
    if (rx_index >= 0 && rx_index < MAX_RX) {
        /* -3.9 ppm frequency calibration for Red Pitaya STEMlab 125-14 */
        h->frequencies[rx_index] = (uint32_t)(freq_hz * 0.9999961);
        h->rx_enabled[rx_index] = 1;
    }
}

void hpsdr_start(HpsdrFast *h) {
    /* Speed bits: 0=48k, 1=96k, 2=192k, 3=384k */
    int speed = 0;
    if (h->sample_rate >= 384000) speed = 3;
    else if (h->sample_rate >= 192000) speed = 2;
    else if (h->sample_rate >= 96000) speed = 1;

    int n_rx_bits = (h->n_receivers - 1) & 0x07;

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = h->sdr_ip;
    addr.sin_port = htons(h->sdr_port);

    /* Config: speed + n_receivers + duplex
     * C4: bit 2 = duplex, bits 5:3 = n_receivers - 1 */
    uint8_t config[5] = { 0x00, (uint8_t)speed, 0x00, 0x00,
                          (uint8_t)((1 << 2) | (n_rx_bits << 3)) };
    /* LNA gain */
    uint8_t lna[5] = { 0x14, 0x00, 0x00, 0x00, (uint8_t)(h->lna_gain & 0x7F) };

    /* Send config BEFORE start — Pitaya needs speed/n_rx set first */
    send_ep2(h, config, lna);
    usleep(50000);

    /* Set frequencies — send each twice with delay for reliability */
    for (int i = 0; i < h->n_receivers; i++) {
        uint8_t freq[5];
        freq[0] = (uint8_t)((i + 2) * 2);
        uint32_t f = h->frequencies[i];
        freq[1] = (f >> 24) & 0xFF;
        freq[2] = (f >> 16) & 0xFF;
        freq[3] = (f >>  8) & 0xFF;
        freq[4] = (f      ) & 0xFF;
        send_ep2(h, config, freq);
        usleep(50000);
        send_ep2(h, config, freq);
        usleep(50000);
    }

    /* Send start command */
    uint8_t start_pkt[64];
    memset(start_pkt, 0, sizeof(start_pkt));
    start_pkt[0] = COOKIE_0; start_pkt[1] = COOKIE_1;
    start_pkt[2] = 0x04; start_pkt[3] = 0x01;
    sendto(h->sock, start_pkt, 64, 0, (struct sockaddr *)&addr, sizeof(addr));
    usleep(100000);

    /* Send config again after start to ensure it takes */
    send_ep2(h, config, lna);
    usleep(10000);

    /* Start receive thread */
    h->running = 1;
    pthread_create(&h->thread, NULL, recv_thread, h);
}

static void *scanner_worker(void *arg);

void hpsdr_set_scanner(HpsdrFast *h, int rx_index, void *scanner_handle,
                        sc_feed_iq_fn feed_fn, double scale) {
    if (rx_index >= 0 && rx_index < MAX_RX) {
        h->scanners[rx_index] = scanner_handle;
        h->scanner_feed = feed_fn;
        h->iq_scale = scale;
    }
}

/* ---- FT8 setup and decode ---- */

void hpsdr_set_ft8(HpsdrFast *h, int rx_index, double ft8_freq_hz,
                    double center_hz, const char *ft8d_path) {
    if (rx_index < 0 || rx_index >= MAX_RX) return;
    h->ft8[rx_index].enabled = 1;
    h->ft8[rx_index].ft8_freq_hz = ft8_freq_hz;
    h->ft8[rx_index].center_hz = center_hz;
    h->ft8[rx_index].buf_n = 0;
    h->ft8[rx_index].mix_phase = 0;
    h->ft8[rx_index].s1_pos = 0;
    h->ft8[rx_index].s1_count = 0;
    h->ft8[rx_index].s2_pos = 0;
    h->ft8[rx_index].s2_count = 0;
    memset(h->ft8[rx_index].s1_dl_i, 0, sizeof(h->ft8[rx_index].s1_dl_i));
    memset(h->ft8[rx_index].s1_dl_q, 0, sizeof(h->ft8[rx_index].s1_dl_q));
    memset(h->ft8[rx_index].s2_dl_i, 0, sizeof(h->ft8[rx_index].s2_dl_i));
    memset(h->ft8[rx_index].s2_dl_q, 0, sizeof(h->ft8[rx_index].s2_dl_q));
    if (ft8d_path)
        strncpy(h->ft8d_path, ft8d_path, sizeof(h->ft8d_path) - 1);
}

/* FT8 2-stage FIR coefficients (scipy.signal.firwin) */
static const double ft8_s1[32] = {
    0.0001941042, 0.0006868266, 0.0015712643, 0.0031632958,
    0.0057532538, 0.0095608923, 0.0146958011, 0.0211287766,
    0.0286784589, 0.0370157426, 0.0456862828, 0.0541491260,
    0.0618274047, 0.0681654164, 0.0726854808, 0.0750378728,
    0.0750378728, 0.0726854808, 0.0681654164, 0.0618274047,
    0.0541491260, 0.0456862828, 0.0370157426, 0.0286784589,
    0.0211287766, 0.0146958011, 0.0095608923, 0.0057532538,
    0.0031632958, 0.0015712643, 0.0006868266, 0.0001941042,
};
static const double ft8_s2[32] = {
    0.0014602599, 0.0017446978, 0.0004315304, -0.0029179815,
    -0.0060836525, -0.0040731620, 0.0057699424, 0.0173542913,
    0.0168637213, -0.0050078389, -0.0381991061, -0.0516539187,
    -0.0126174382, 0.0846211315, 0.2046726384, 0.2876348849,
    0.2876348849, 0.2046726384, 0.0846211315, -0.0126174382,
    -0.0516539187, -0.0381991061, -0.0050078389, 0.0168637213,
    0.0173542913, 0.0057699424, -0.0040731620, -0.0060836525,
    -0.0029179815, 0.0004315304, 0.0017446978, 0.0014602599,
};

static inline double fir_conv(const double *dl, int pos, const double *h, int len) {
    double acc = 0;
    for (int j = 0; j < len; j++)
        acc += dl[(pos + j) % len] * h[j];
    return acc;
}

/* Feed raw IQ samples into FT8 channelizer — 2-stage decimation */
static void ft8_feed(HpsdrFast *h, int rx, const double *i_arr,
                      const double *q_arr, int n, double scale) {
    if (!h->ft8[rx].enabled) return;

    double offset_hz = h->ft8[rx].ft8_freq_hz - h->ft8[rx].center_hz;
    double phase = h->ft8[rx].mix_phase;
    double step = 2.0 * M_PI * offset_hz / 192000.0;

    for (int k = 0; k < n; k++) {
        /* Mix to FT8 baseband */
        double ci = cos(phase), si = sin(phase);
        double ii = i_arr[k], qi = q_arr[k];
        double mi = ii * ci + qi * si;
        double mq = -ii * si + qi * ci;
        phase += step;

        /* Stage 1: FIR + decimate 16:1 → 12 kHz */
        int p1 = h->ft8[rx].s1_pos;
        h->ft8[rx].s1_dl_i[p1] = mi;
        h->ft8[rx].s1_dl_q[p1] = mq;
        h->ft8[rx].s1_pos = (p1 + 1) % FT8_S1_LEN;
        h->ft8[rx].s1_count++;

        if (h->ft8[rx].s1_count % FT8_DEC1 != 0) continue;
        double o1_i = fir_conv(h->ft8[rx].s1_dl_i, h->ft8[rx].s1_pos, ft8_s1, FT8_S1_LEN);
        double o1_q = fir_conv(h->ft8[rx].s1_dl_q, h->ft8[rx].s1_pos, ft8_s1, FT8_S1_LEN);

        /* Stage 2: FIR + decimate 3:1 → 4 kHz */
        int p2 = h->ft8[rx].s2_pos;
        h->ft8[rx].s2_dl_i[p2] = o1_i;
        h->ft8[rx].s2_dl_q[p2] = o1_q;
        h->ft8[rx].s2_pos = (p2 + 1) % FT8_S2_LEN;
        h->ft8[rx].s2_count++;

        if (h->ft8[rx].s2_count % FT8_DEC2 != 0) continue;
        double o2_i = fir_conv(h->ft8[rx].s2_dl_i, h->ft8[rx].s2_pos, ft8_s2, FT8_S2_LEN);
        double o2_q = fir_conv(h->ft8[rx].s2_dl_q, h->ft8[rx].s2_pos, ft8_s2, FT8_S2_LEN);

        /* Store 4 kHz sample — scale to match ft8d expected amplitude */
        int bn = h->ft8[rx].buf_n;
        if (bn < FT8_WIN) {
            h->ft8[rx].buf_i[bn] = (float)o2_i;
            h->ft8[rx].buf_q[bn] = (float)o2_q;
            h->ft8[rx].buf_n = bn + 1;
        }
    }
    while (phase > 2.0 * M_PI) phase -= 2.0 * M_PI;
    while (phase < -2.0 * M_PI) phase += 2.0 * M_PI;
    h->ft8[rx].mix_phase = phase;
}

/* Dump rolling 60-second window every 15 seconds */
static void ft8_check_decode(HpsdrFast *h) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    int slot = (int)(ts.tv_sec) / 15;
    if (slot == h->ft8_last_slot) return;
    h->ft8_last_slot = slot;

    int target = FT8_RATE * 60;  /* 240000 samples */

    for (int rx = 0; rx < MAX_RX; rx++) {
        if (!h->ft8[rx].enabled) continue;
        int bn = h->ft8[rx].buf_n;
        if (bn < target) continue;  /* need 60 seconds */

        /* Write last 60 seconds from the rolling buffer */
        char fname[128];
        snprintf(fname, sizeof(fname), "/tmp/ft8_rx%d.c2", rx);
        FILE *fp = fopen(fname, "wb");
        if (!fp) continue;

        double dial = h->ft8[rx].ft8_freq_hz;
        fwrite(&dial, 1, 8, fp);

        int start = bn - target;
        for (int k = start; k < start + target; k++) {
            fwrite(&h->ft8[rx].buf_i[k], sizeof(float), 1, fp);
            fwrite(&h->ft8[rx].buf_q[k], sizeof(float), 1, fp);
        }
        fclose(fp);

        /* Compact buffer — keep last 60 seconds */
        if (bn > target) {
            int keep = target;  /* keep a full window for overlap */
            memmove(h->ft8[rx].buf_i, h->ft8[rx].buf_i + bn - keep,
                    keep * sizeof(float));
            memmove(h->ft8[rx].buf_q, h->ft8[rx].buf_q + bn - keep,
                    keep * sizeof(float));
            h->ft8[rx].buf_n = keep;
        }

        /* Spawn ft8d */
        if (h->ft8d_path[0]) {
            char cmd[384];
            snprintf(cmd, sizeof(cmd), "%s %s >> /tmp/ft8_spots.log 2>/dev/null &",
                     h->ft8d_path, fname);
            (void)system(cmd);
        }
    }
}

void hpsdr_set_decode(HpsdrFast *h,
                       int (*decode_fn)(void*, int, void*, int),
                       int window_samples) {
    h->scanner_decode = decode_fn;
    h->window_samples = window_samples;
}

void hpsdr_start_worker(HpsdrFast *h) {
    if (h->worker_running) return;
    pthread_mutex_init(&h->result_lock, NULL);
    h->result_write = 0;
    h->result_read = 0;
    h->result_count = 0;
    h->worker_running = 1;
    pthread_create(&h->worker_thread, NULL, scanner_worker, h);
}

void hpsdr_stop_worker(HpsdrFast *h) {
    if (!h->worker_running) return;
    h->worker_running = 0;
    pthread_join(h->worker_thread, NULL);
}

void hpsdr_stop(HpsdrFast *h) {
    hpsdr_stop_worker(h);
    h->running = 0;
    pthread_join(h->thread, NULL);

    /* Send stop */
    uint8_t stop_pkt[64];
    memset(stop_pkt, 0, sizeof(stop_pkt));
    stop_pkt[0] = COOKIE_0; stop_pkt[1] = COOKIE_1;
    stop_pkt[2] = 0x04; stop_pkt[3] = 0x00;
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = h->sdr_ip;
    addr.sin_port = htons(h->sdr_port);
    sendto(h->sock, stop_pkt, 64, 0, (struct sockaddr *)&addr, sizeof(addr));
}

void hpsdr_destroy(HpsdrFast *h) {
    if (!h) return;
    if (h->running) hpsdr_stop(h);
    close(h->sock);
    pthread_mutex_destroy(&h->lock);
    free(h);
}

int hpsdr_drain(HpsdrFast *h, int rx_index, double *i_out, double *q_out, int max_n) {
    if (rx_index < 0 || rx_index >= h->n_receivers) return 0;
    RxRing *r = &h->rx[rx_index];
    int avail = ring_avail(r);
    int n = avail < max_n ? avail : max_n;
    int rp = r->read_pos;
    for (int k = 0; k < n; k++) {
        i_out[k] = r->i[rp];
        q_out[k] = r->q[rp];
        rp = (rp + 1) % RING_SIZE;
    }
    r->read_pos = rp;
    return n;
}

/* ---- worker thread: drain ring buffers → feed scanners → decode ---- */
static void *scanner_worker(void *arg) {
    HpsdrFast *h = (HpsdrFast *)arg;
    #define WCHUNK 19200  /* 100ms at 192 kHz */
    double i_tmp[WCHUNK], q_tmp[WCHUNK];
    uint8_t dec_buf[64 * RESULT_SIZE];  /* temp decode results */

    while (h->worker_running) {
        int did_work = 0;
        for (int rx = 0; rx < h->n_receivers; rx++) {
            if (!h->rx_enabled[rx] || !h->scanners[rx] || !h->scanner_feed)
                continue;

            /* Drain ring buffer → feed scanner */
            RxRing *r = &h->rx[rx];
            int avail = ring_avail(r);
            while (avail > 0) {
                int n = avail < WCHUNK ? avail : WCHUNK;
                int rp = r->read_pos;
                for (int k = 0; k < n; k++) {
                    i_tmp[k] = r->i[rp] * h->iq_scale;
                    q_tmp[k] = r->q[rp] * h->iq_scale;
                    rp = (rp + 1) % RING_SIZE;
                }
                r->read_pos = rp;
                h->scanner_feed(h->scanners[rx], i_tmp, q_tmp, n);
                /* Feed FT8 with raw (unscaled) IQ from the same chunk */
                {
                    double ft8_tmp[WCHUNK];
                    double ft8_q_tmp[WCHUNK];
                    for (int s = 0; s < n; s++) {
                        ft8_tmp[s] = i_tmp[s] / h->iq_scale;
                        ft8_q_tmp[s] = q_tmp[s] / h->iq_scale;
                    }
                    /* ft8_feed disabled — needs debugging */
                }
                did_work = 1;
                avail = ring_avail(r);
            }

            /* Decode ready windows */
            if (h->scanner_decode && h->window_samples > 0) {
                int n_dec = h->scanner_decode(h->scanners[rx],
                                               h->window_samples,
                                               dec_buf, 64);
                if (n_dec > 0) {
                    pthread_mutex_lock(&h->result_lock);
                    for (int d = 0; d < n_dec; d++) {
                        if (h->result_count >= RESULT_MAX) break;
                        int wp = h->result_write;
                        memcpy(h->result_buf + wp * RESULT_SIZE,
                               dec_buf + d * RESULT_SIZE, RESULT_SIZE);
                        h->result_write = (wp + 1) % RESULT_MAX;
                        h->result_count++;
                    }
                    pthread_mutex_unlock(&h->result_lock);
                    did_work = 1;
                }
            }
        }
        /* Check FT8 decode timing */
        /* ft8_check_decode disabled */

        if (!did_work) usleep(5000); /* 5ms idle sleep */
    }
    return NULL;
    #undef WCHUNK
}

/* Poll decoded results — called from Python */
int hpsdr_poll_results(HpsdrFast *h, void *out_buf, int max_results) {
    pthread_mutex_lock(&h->result_lock);
    int n = h->result_count < max_results ? h->result_count : max_results;
    for (int i = 0; i < n; i++) {
        int rp = h->result_read;
        memcpy((uint8_t *)out_buf + i * RESULT_SIZE,
               h->result_buf + rp * RESULT_SIZE, RESULT_SIZE);
        h->result_read = (rp + 1) % RESULT_MAX;
        h->result_count--;
    }
    pthread_mutex_unlock(&h->result_lock);
    return n;
}

/* Drain ring buffer and feed directly to scanner — pure C, no Python */

int hpsdr_drain_to_scanner(HpsdrFast *h, int rx_index,
                            void *scanner_handle, double scale,
                            sc_feed_iq_fn feed_fn) {
    if (rx_index < 0 || rx_index >= h->n_receivers || !scanner_handle || !feed_fn) return 0;

    RxRing *r = &h->rx[rx_index];
    int avail = ring_avail(r);
    if (avail == 0) return 0;

    #define CHUNK 19200  /* 100ms at 192 kHz */
    double i_tmp[CHUNK], q_tmp[CHUNK];
    int total = 0;

    while (avail > 0) {
        int n = avail < CHUNK ? avail : CHUNK;
        int rp = r->read_pos;
        for (int k = 0; k < n; k++) {
            i_tmp[k] = r->i[rp] * scale;
            q_tmp[k] = r->q[rp] * scale;
            rp = (rp + 1) % RING_SIZE;
        }
        r->read_pos = rp;
        feed_fn(scanner_handle, i_tmp, q_tmp, n);
        total += n;
        avail = ring_avail(r);
    }
    return total;
    #undef CHUNK
}

int hpsdr_available(HpsdrFast *h, int rx_index) {
    if (rx_index < 0 || rx_index >= h->n_receivers) return 0;
    return ring_avail(&h->rx[rx_index]);
}

/* FT8 IQ accumulator access */
void hpsdr_enable_ft8(HpsdrFast *h, int rx, double ft8_freq, double center_freq) {
    if (rx < 0 || rx >= MAX_RX) return;
    if (!h->ft8_raw[rx].i) {
        h->ft8_raw[rx].i = (float *)calloc(FT8_RAW_SIZE, sizeof(float));
        h->ft8_raw[rx].q = (float *)calloc(FT8_RAW_SIZE, sizeof(float));
        if (!h->ft8_raw[rx].i || !h->ft8_raw[rx].q) return;
    }
    h->ft8_raw[rx].enabled = 1;
    h->ft8_raw[rx].freq_hz = ft8_freq;
    h->ft8_raw[rx].center_hz = center_freq;
    h->ft8_raw[rx].write_pos = 0;
}

int hpsdr_read_ft8(HpsdrFast *h, int rx, float *i_out, float *q_out, int max_n) {
    if (rx < 0 || rx >= MAX_RX || !h->ft8_raw[rx].enabled || !h->ft8_raw[rx].i) return 0;
    int wp = h->ft8_raw[rx].write_pos;
    /* Circular read: always read from the buffer using modulo */
    int avail = wp;
    if (avail > FT8_RAW_SIZE) avail = FT8_RAW_SIZE;  /* can't read more than buffer */
    int n = avail < max_n ? avail : max_n;
    int start = wp - n;
    for (int k = 0; k < n; k++) {
        int idx = (start + k) % FT8_RAW_SIZE;
        if (idx < 0) idx += FT8_RAW_SIZE;
        i_out[k] = h->ft8_raw[rx].i[idx];
        q_out[k] = h->ft8_raw[rx].q[idx];
    }
    return n;
}

uint64_t hpsdr_pkt_count(HpsdrFast *h) { return h->pkt_count; }
uint64_t hpsdr_drop_count(HpsdrFast *h) { return h->drop_count; }
int hpsdr_n_receivers(HpsdrFast *h) { return h->n_receivers; }
