/**
 * cw_engine.cpp — Streaming CW channelizer + multi-speed decoder library
 *
 * Hot path: IQ → SSB mix → Butterworth IIR → decimate → uhsdr × N speeds → text
 * Uses 6th-order Butterworth IIR (3 biquads in SOS form) matching Python's
 * scipy.signal.butter exactly. Filter state persists across chunks.
 */

#include "cw_engine.h"
#include "uhsdr_cw_lib.h"
#include "libbmorse.h"

#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* ─── Constants ───────────────────────────────────────────────── */

#define UHSDR_RATE  12000
#define BMORSE_RATE 4000
#define CW_TONE     700.0f

/* Multi-speed decoder config */
#define MAX_UHSDR   5
static const int UHSDR_SPEEDS[MAX_UHSDR] = { 0, 15, 20, 25, 30 };

static bool g_initialized = false;

/* ─── 6th-order Butterworth IIR (SOS form) ─────────────────── */
/* Coefficients from scipy: butter(6, 6000, btype='low', fs=192000, output='sos') */

#define N_SOS 3

struct sos_section {
    double b0, b1, b2;
    double a1, a2;      /* a0 is always 1.0 */
    double z1, z2;       /* Direct Form II transposed state */
};

/* Hardcoded coefficients for 192kHz → 6kHz cutoff Butterworth */
static void init_butter_192k(sos_section sos[N_SOS]) {
    /* Section 0 */
    sos[0].b0 = 6.241911798109912e-07;
    sos[0].b1 = 1.248382359621982e-06;
    sos[0].b2 = 6.241911798109912e-07;
    sos[0].a1 = -1.650538497099866e+00;
    sos[0].a2 = 6.828744579254694e-01;
    sos[0].z1 = 0; sos[0].z2 = 0;

    /* Section 1 */
    sos[1].b0 = 1.0;
    sos[1].b1 = 2.0;
    sos[1].b2 = 1.0;
    sos[1].a1 = -1.723776172762509e+00;
    sos[1].a2 = 7.575469444788288e-01;
    sos[1].z1 = 0; sos[1].z2 = 0;

    /* Section 2 */
    sos[2].b0 = 1.0;
    sos[2].b1 = 2.0;
    sos[2].b2 = 1.0;
    sos[2].a1 = -1.867285542272102e+00;
    sos[2].a2 = 9.038678287508599e-01;
    sos[2].z1 = 0; sos[2].z2 = 0;
}

/* Process one sample through all SOS sections (Direct Form II transposed) */
static inline double sos_filter(sos_section sos[N_SOS], double x) {
    for (int i = 0; i < N_SOS; i++) {
        double y = sos[i].b0 * x + sos[i].z1;
        sos[i].z1 = sos[i].b1 * x - sos[i].a1 * y + sos[i].z2;
        sos[i].z2 = sos[i].b2 * x - sos[i].a2 * y;
        x = y;
    }
    return x;
}

/* ─── Per-channel state ────────────────────────────────────────── */

struct channel_state {
    float offset_hz;
    float sample_rate;
    float pitch_hz;

    /* Mixer */
    double phase;
    double phase_inc;

    /* Butterworth IIR (replaces FIR) — state persists across chunks */
    sos_section iir[N_SOS];
    int    dec_factor;
    int    dec_count;

    /* Peak normalization */
    float peak;

    /* Deferred spawn: 5s pitch detection buffer */
    bool   pitch_detected;
    int16_t *ring_buf;
    int    ring_len;
    int    ring_cap;
    int    replay_pos;
    bool   replaying;

    /* Multi-speed uhsdr decoders */
    uhsdr_handle_t uhsdr[MAX_UHSDR];
    int    n_uhsdr;

    /* Decoded text per decoder + read cursor */
    char uhsdr_text[MAX_UHSDR][8192];
    int  uhsdr_text_len[MAX_UHSDR];
    int  uhsdr_text_read[MAX_UHSDR];

    long   samples_fed;
};

/* ─── Engine init/shutdown ─────────────────────────────────────── */

extern "C" {

int cw_engine_init(const char *scp_path)
{
    (void)scp_path;
    if (g_initialized) return 0;
    g_initialized = true;
    fprintf(stderr, "cw_engine: initialized (Butterworth IIR, text output)\n");
    return 0;
}

void cw_engine_shutdown(void)
{
    g_initialized = false;
}

/* ─── Channel lifecycle ────────────────────────────────────────── */

channel_t channel_create(float offset_hz, float sample_rate)
{
    channel_state *ch = (channel_state *)calloc(1, sizeof(channel_state));
    if (!ch) return NULL;

    ch->offset_hz = offset_hz;
    ch->sample_rate = sample_rate;
    ch->pitch_hz = CW_TONE;

    /* Mixer */
    ch->phase = 0.0;
    ch->phase_inc = 2.0 * M_PI * (offset_hz - CW_TONE) / sample_rate;

    /* Butterworth IIR: 6th-order, cutoff 6kHz at 192kHz */
    init_butter_192k(ch->iir);
    ch->dec_factor = (int)(sample_rate / UHSDR_RATE);
    ch->dec_count = 0;

    ch->peak = 0.0f;

    /* Deferred spawn: 5s pitch detection buffer */
    ch->pitch_detected = false;
    ch->ring_cap = UHSDR_RATE * 5;
    ch->ring_buf = (int16_t *)calloc(ch->ring_cap, sizeof(int16_t));
    ch->ring_len = 0;
    ch->replay_pos = 0;
    ch->replaying = false;

    ch->n_uhsdr = 0;
    for (int i = 0; i < MAX_UHSDR; i++) ch->uhsdr[i] = NULL;

    for (int d = 0; d < MAX_UHSDR; d++) {
        ch->uhsdr_text_len[d] = 0;
        ch->uhsdr_text_read[d] = 0;
    }
    ch->samples_fed = 0;

    return (channel_t)ch;
}

void channel_set_pitch(channel_t h, float pitch_hz)
{
    if (!h) return;
    channel_state *ch = (channel_state *)h;
    ch->pitch_hz = pitch_hz;

    for (int i = 0; i < ch->n_uhsdr; i++) {
        if (ch->uhsdr[i]) uhsdr_free(ch->uhsdr[i]);
        ch->uhsdr[i] = uhsdr_init(pitch_hz, (float)UHSDR_RATE, UHSDR_SPEEDS[i]);
    }
    for (int d = 0; d < MAX_UHSDR; d++) {
        ch->uhsdr_text_len[d] = 0;
        ch->uhsdr_text_read[d] = 0;
    }
}

void channel_feed_iq(channel_t h,
                     const float *i_samples, const float *q_samples, int n)
{
    if (!h || !i_samples || !q_samples || n <= 0) return;
    channel_state *ch = (channel_state *)h;

    char dec_buf[4096];
    ch->samples_fed += n;

    /* ── Amortized replay ── */
    if (ch->replaying) {
        int chunk = 6000;
        if (ch->replay_pos < ch->ring_len) {
            int len = ch->ring_len - ch->replay_pos;
            if (len > chunk) len = chunk;
            for (int d = 0; d < ch->n_uhsdr; d++) {
                if (ch->uhsdr[d]) {
                    int nc = uhsdr_feed(ch->uhsdr[d],
                                        &ch->ring_buf[ch->replay_pos],
                                        len, dec_buf, sizeof(dec_buf));
                    if (nc > 0 && ch->uhsdr_text_len[d] + nc < 8191) {
                        memcpy(ch->uhsdr_text[d] + ch->uhsdr_text_len[d], dec_buf, nc);
                        ch->uhsdr_text_len[d] += nc;
                    }
                }
            }
            ch->replay_pos += len;
        }
        if (ch->replay_pos >= ch->ring_len) {
            free(ch->ring_buf);
            ch->ring_buf = NULL;
            ch->replaying = false;
        }
        return;
    }

    /* ── Main channelization loop ── */
    for (int i = 0; i < n; i++) {
        /* SSB mix: I*cos + Q*sin */
        float phase_f = (float)ch->phase;
        float mixed = i_samples[i] * cosf(phase_f) + q_samples[i] * sinf(phase_f);
        ch->phase += ch->phase_inc;
        if (ch->phase > 2.0 * M_PI) ch->phase -= 2.0 * M_PI;
        if (ch->phase < -2.0 * M_PI) ch->phase += 2.0 * M_PI;

        /* Butterworth IIR filter (stateful across chunks) */
        double filtered = sos_filter(ch->iir, (double)mixed);

        /* Decimate */
        ch->dec_count++;
        if (ch->dec_count >= ch->dec_factor) {
            ch->dec_count = 0;

            float out = (float)filtered;

            /* Peak normalize */
            float absv = fabsf(out);
            if (absv > ch->peak) ch->peak = absv;
            else ch->peak *= 0.9999f;
            if (ch->peak > 0) out = out / ch->peak * 0.3f;
            int16_t s = (int16_t)(out * 32767.0f);
            if (s > 32767) s = 32767; if (s < -32767) s = -32767;

            /* Before pitch detection: buffer audio */
            if (!ch->pitch_detected) {
                if (ch->ring_len < ch->ring_cap)
                    ch->ring_buf[ch->ring_len++] = s;
            } else {
                for (int d = 0; d < ch->n_uhsdr; d++) {
                    if (ch->uhsdr[d]) {
                        int nc = uhsdr_feed(ch->uhsdr[d], &s, 1,
                                            dec_buf, sizeof(dec_buf));
                        if (nc > 0 && ch->uhsdr_text_len[d] + nc < 8191) {
                            memcpy(ch->uhsdr_text[d] + ch->uhsdr_text_len[d],
                                   dec_buf, nc);
                            ch->uhsdr_text_len[d] += nc;
                        }
                    }
                }
            }
        }
    }

    /* ── Pitch detection after 5s buffer fills ── */
    if (!ch->pitch_detected && ch->ring_len >= ch->ring_cap) {
        #define BIN_W  25.0f
        #define P_LO   400.0f
        #define P_HI   900.0f
        int n_bins = (int)((P_HI - P_LO) / BIN_W);
        float bin_pwr[24];
        int best_bin = 0;
        float best_pwr = -1e20f;

        for (int b = 0; b < n_bins && b < 24; b++) {
            float freq = P_LO + b * BIN_W + BIN_W / 2.0f;
            float w = 2.0f * M_PI * freq / UHSDR_RATE;
            float coeff = 2.0f * cosf(w);
            float s0 = 0, s1 = 0, s2 = 0;
            for (int k = 0; k < ch->ring_len; k++) {
                s0 = coeff * s1 - s2 + (float)ch->ring_buf[k];
                s2 = s1; s1 = s0;
            }
            float pwr = s1 * s1 + s2 * s2 - coeff * s1 * s2;
            bin_pwr[b] = pwr;
            if (pwr > best_pwr) { best_pwr = pwr; best_bin = b; }
        }

        float sorted[24];
        int n_other = 0;
        for (int b = 0; b < n_bins; b++)
            if (b != best_bin) sorted[n_other++] = bin_pwr[b];
        for (int a = 0; a < n_other - 1; a++)
            for (int b = a + 1; b < n_other; b++)
                if (sorted[b] < sorted[a]) { float t = sorted[a]; sorted[a] = sorted[b]; sorted[b] = t; }
        float median = (n_other > 0) ? sorted[n_other / 2] : 1e-20f;
        float snr = 10.0f * log10f(best_pwr / (median + 1e-20f));

        float pitch;
        if (snr >= 8.0f) {
            pitch = P_LO + best_bin * BIN_W + BIN_W / 2.0f;
            fprintf(stderr, "  pitch: %.0f Hz (SNR %.1f dB) offset %.0f\n",
                    pitch, snr, ch->offset_hz);
        } else {
            pitch = CW_TONE;
        }

        ch->pitch_hz = pitch;
        ch->n_uhsdr = MAX_UHSDR;
        for (int d = 0; d < MAX_UHSDR; d++)
            ch->uhsdr[d] = uhsdr_init(pitch, (float)UHSDR_RATE, UHSDR_SPEEDS[d]);

        ch->replay_pos = 0;
        ch->replaying = true;
        ch->pitch_detected = true;
    }
}

/* ─── Text output API ──────────────────────────────────────────── */

int channel_decoder_count(channel_t h)
{
    if (!h) return 0;
    return ((channel_state *)h)->n_uhsdr;
}

int channel_read_text(channel_t h, int decoder_idx,
                      char *buf, int buflen, int *wpm)
{
    if (!h || !buf || buflen <= 0) return 0;
    channel_state *ch = (channel_state *)h;
    if (decoder_idx < 0 || decoder_idx >= ch->n_uhsdr) return 0;

    int avail = ch->uhsdr_text_len[decoder_idx] - ch->uhsdr_text_read[decoder_idx];
    if (avail <= 0) { buf[0] = '\0'; return 0; }
    if (avail >= buflen) avail = buflen - 1;

    memcpy(buf, ch->uhsdr_text[decoder_idx] + ch->uhsdr_text_read[decoder_idx], avail);
    buf[avail] = '\0';
    ch->uhsdr_text_read[decoder_idx] += avail;

    if (wpm && ch->uhsdr[decoder_idx])
        *wpm = uhsdr_get_wpm(ch->uhsdr[decoder_idx]);

    return avail;
}

int channel_decoder_speed(channel_t h, int decoder_idx)
{
    if (!h) return 0;
    channel_state *ch = (channel_state *)h;
    if (decoder_idx < 0 || decoder_idx >= ch->n_uhsdr) return 0;
    return UHSDR_SPEEDS[decoder_idx];
}

float channel_get_pitch(channel_t h)
{
    if (!h) return CW_TONE;
    return ((channel_state *)h)->pitch_hz;
}

int channel_get_wpm(channel_t h)
{
    if (!h) return 0;
    channel_state *ch = (channel_state *)h;
    for (int d = 0; d < ch->n_uhsdr; d++) {
        if (ch->uhsdr[d]) {
            int wpm = uhsdr_get_wpm(ch->uhsdr[d]);
            if (wpm > 0) return wpm;
        }
    }
    return 0;
}

void channel_destroy(channel_t h)
{
    if (!h) return;
    channel_state *ch = (channel_state *)h;
    for (int d = 0; d < ch->n_uhsdr; d++)
        if (ch->uhsdr[d]) uhsdr_free(ch->uhsdr[d]);
    free(ch->ring_buf);
    free(ch);
}

} /* extern "C" */
