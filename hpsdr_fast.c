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

#define MAX_RX        8
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

void hpsdr_stop(HpsdrFast *h) {
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

/* Drain ring buffer and feed directly to scanner — pure C, no Python */
typedef void (*sc_feed_iq_fn)(void *, const double *, const double *, int);

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

uint64_t hpsdr_pkt_count(HpsdrFast *h) { return h->pkt_count; }
uint64_t hpsdr_drop_count(HpsdrFast *h) { return h->drop_count; }
int hpsdr_n_receivers(HpsdrFast *h) { return h->n_receivers; }
