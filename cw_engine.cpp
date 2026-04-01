/**
 * cw_engine.cpp — Streaming CW channelizer + dual decoder library
 *
 * Skeleton: struct layout, init/create/destroy, feed_iq stub.
 * Decoder integration (uhsdr + bmorse) added next.
 */

#include "cw_engine.h"
#include "uhsdr_cw_lib.h"
#include "libbmorse.h"

#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdio.h>
#include <set>
#include <string>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* ─── SCP database ─────────────────────────────────────────────── */

static std::set<std::string> g_scp;
static bool g_initialized = false;

/* ─── Per-channel state ────────────────────────────────────────── */

#define UHSDR_RATE  12000
#define BMORSE_RATE 4000
#define CW_TONE     700.0f

struct channel_state {
    float offset_hz;       /* signal offset from center */
    float sample_rate;     /* input IQ sample rate */
    float pitch_hz;        /* detected CW tone (default CW_TONE) */

    /* Mixer state */
    double phase;
    double phase_inc;

    /* IIR lowpass for uhsdr path (12kHz output) */
    /* Using simple single-pole IIR for skeleton — replace with proper filter */
    float uhsdr_acc;
    int   uhsdr_dec_factor;
    int   uhsdr_dec_count;
    float uhsdr_buf[256];  /* accumulate decimated samples */
    int   uhsdr_buf_len;

    /* IIR lowpass for bmorse path (4kHz output) */
    float bmorse_acc;
    int   bmorse_dec_factor;
    int   bmorse_dec_count;
    float bmorse_buf[256];
    int   bmorse_buf_len;

    /* Decoder handles */
    uhsdr_handle_t uhsdr;
    bmorse_handle_t bmorse;

    /* Decoded text accumulators */
    char uhsdr_text[4096];
    int  uhsdr_text_len;
    char bmorse_text[4096];
    int  bmorse_text_len;
};

/* ─── SCP matching ─────────────────────────────────────────────── */

static bool is_callsign_shaped(const char *s, int len) {
    if (len < 4 || len > 7) return false;
    bool has_digit = false, has_letter = false;
    for (int i = 0; i < len; i++) {
        if (s[i] >= '0' && s[i] <= '9') has_digit = true;
        else if (s[i] >= 'A' && s[i] <= 'Z') has_letter = true;
        else return false;
    }
    return has_digit && has_letter;
}

static int extract_spots(const char *text, int text_len, float offset_hz,
                          float snr, int wpm, int decoder_id,
                          cw_spot_t *spots, int max_spots) {
    if (!g_initialized || text_len < 4) return 0;

    int count = 0;
    /* Sliding window SCP match on cleaned text */
    char clean[4096];
    int clen = 0;
    for (int i = 0; i < text_len && clen < 4095; i++) {
        char c = text[i];
        if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9'))
            clean[clen++] = c;
    }
    clean[clen] = '\0';

    for (int wlen = 4; wlen <= 7 && wlen <= clen; wlen++) {
        for (int i = 0; i <= clen - wlen && count < max_spots; i++) {
            char frag[8];
            memcpy(frag, clean + i, wlen);
            frag[wlen] = '\0';
            if (is_callsign_shaped(frag, wlen) &&
                g_scp.count(std::string(frag))) {
                /* Check not a common false positive */
                if (strcmp(frag, "TEST") == 0 || strcmp(frag, "CQ00") == 0)
                    continue;
                /* Deduplicate within this batch */
                bool dup = false;
                for (int j = 0; j < count; j++) {
                    if (strcmp(spots[j].callsign, frag) == 0) { dup = true; break; }
                }
                if (!dup) {
                    strncpy(spots[count].callsign, frag, 15);
                    spots[count].callsign[15] = '\0';
                    spots[count].freq_offset_hz = offset_hz;
                    spots[count].snr_db = snr;
                    spots[count].wpm = wpm;
                    spots[count].decoder = decoder_id;
                    count++;
                }
            }
        }
    }
    return count;
}

/* ─── Engine init/shutdown ─────────────────────────────────────── */

extern "C" {

int cw_engine_init(const char *scp_path)
{
    if (g_initialized) return 0;

    FILE *f = fopen(scp_path, "r");
    if (!f) {
        fprintf(stderr, "cw_engine: cannot open SCP file %s\n", scp_path);
        return -1;
    }
    char buf[64];
    while (fgets(buf, sizeof(buf), f)) {
        char *p = buf;
        while (*p && (*p == ' ' || *p == '\t')) p++;
        int len = strlen(p);
        while (len > 0 && (p[len-1] == '\n' || p[len-1] == '\r' || p[len-1] == ' '))
            p[--len] = '\0';
        if (len > 0 && p[0] != '#') {
            /* Uppercase */
            for (int i = 0; i < len; i++)
                if (p[i] >= 'a' && p[i] <= 'z') p[i] -= 32;
            g_scp.insert(std::string(p));
        }
    }
    fclose(f);
    fprintf(stderr, "cw_engine: loaded %zu SCP calls from %s\n", g_scp.size(), scp_path);
    g_initialized = true;
    return 0;
}

void cw_engine_shutdown(void)
{
    g_scp.clear();
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

    /* Decimation factors */
    ch->uhsdr_dec_factor = (int)(sample_rate / UHSDR_RATE);
    ch->bmorse_dec_factor = (int)(sample_rate / BMORSE_RATE);
    ch->uhsdr_dec_count = 0;
    ch->bmorse_dec_count = 0;

    /* Create decoders */
    ch->uhsdr = uhsdr_init(CW_TONE, (float)UHSDR_RATE, 0);
    ch->bmorse = bmorse_create(CW_TONE, (float)BMORSE_RATE, 0);

    ch->uhsdr_text_len = 0;
    ch->bmorse_text_len = 0;

    return (channel_t)ch;
}

void channel_set_pitch(channel_t h, float pitch_hz)
{
    if (!h) return;
    channel_state *ch = (channel_state *)h;
    ch->pitch_hz = pitch_hz;
    ch->phase_inc = 2.0 * M_PI * (ch->offset_hz - pitch_hz) / ch->sample_rate;

    /* Reinit uhsdr at new pitch */
    if (ch->uhsdr) uhsdr_free(ch->uhsdr);
    ch->uhsdr = uhsdr_init(pitch_hz, (float)UHSDR_RATE, 0);

    /* Reinit bmorse at new pitch */
    if (ch->bmorse) bmorse_destroy(ch->bmorse);
    ch->bmorse = bmorse_create(pitch_hz, (float)BMORSE_RATE, 0);

    ch->uhsdr_text_len = 0;
    ch->bmorse_text_len = 0;
}

int channel_feed_iq(channel_t h,
                    const float *i_samples, const float *q_samples, int n,
                    cw_spot_t *spots, int max_spots)
{
    if (!h || !i_samples || !q_samples || n <= 0) return 0;
    channel_state *ch = (channel_state *)h;

    int spot_count = 0;
    char dec_buf[4096];

    for (int i = 0; i < n; i++) {
        /* SSB mix: I*cos + Q*sin → mono audio with CW tone at pitch_hz */
        float phase_f = (float)ch->phase;
        float mixed = i_samples[i] * cosf(phase_f) + q_samples[i] * sinf(phase_f);
        ch->phase += ch->phase_inc;
        if (ch->phase > 2.0 * M_PI) ch->phase -= 2.0 * M_PI;
        if (ch->phase < -2.0 * M_PI) ch->phase += 2.0 * M_PI;

        /* Simple lowpass + decimate for uhsdr (12kHz) */
        /* TODO: replace with proper FIR or IIR */
        ch->uhsdr_acc = 0.95f * ch->uhsdr_acc + 0.05f * mixed;
        ch->uhsdr_dec_count++;
        if (ch->uhsdr_dec_count >= ch->uhsdr_dec_factor) {
            ch->uhsdr_dec_count = 0;
            /* Convert to int16 and feed uhsdr */
            int16_t s = (int16_t)(ch->uhsdr_acc * 32767.0f * 0.3f);
            int nc = uhsdr_feed(ch->uhsdr, &s, 1, dec_buf, sizeof(dec_buf));
            if (nc > 0 && ch->uhsdr_text_len + nc < 4095) {
                memcpy(ch->uhsdr_text + ch->uhsdr_text_len, dec_buf, nc);
                ch->uhsdr_text_len += nc;
            }
        }

        /* Simple lowpass + decimate for bmorse (4kHz) */
        ch->bmorse_acc = 0.95f * ch->bmorse_acc + 0.05f * mixed;
        ch->bmorse_dec_count++;
        if (ch->bmorse_dec_count >= ch->bmorse_dec_factor) {
            ch->bmorse_dec_count = 0;
            int16_t s = (int16_t)(ch->bmorse_acc * 32767.0f * 0.3f);
            int nc = bmorse_feed(ch->bmorse, &s, 1, dec_buf, sizeof(dec_buf));
            if (nc > 0 && ch->bmorse_text_len + nc < 4095) {
                memcpy(ch->bmorse_text + ch->bmorse_text_len, dec_buf, nc);
                ch->bmorse_text_len += nc;
            }
        }
    }

    /* Extract spots from accumulated text */
    if (ch->uhsdr_text_len >= 4) {
        int ns = extract_spots(ch->uhsdr_text, ch->uhsdr_text_len,
                               ch->offset_hz, 0, uhsdr_get_wpm(ch->uhsdr), 0,
                               spots + spot_count, max_spots - spot_count);
        spot_count += ns;
    }
    if (ch->bmorse_text_len >= 4) {
        int ns = extract_spots(ch->bmorse_text, ch->bmorse_text_len,
                               ch->offset_hz, 0, bmorse_get_wpm(ch->bmorse), 1,
                               spots + spot_count, max_spots - spot_count);
        spot_count += ns;
    }

    return spot_count;
}

int channel_get_wpm(channel_t h)
{
    if (!h) return 0;
    channel_state *ch = (channel_state *)h;
    int wpm = uhsdr_get_wpm(ch->uhsdr);
    if (wpm <= 0) wpm = bmorse_get_wpm(ch->bmorse);
    return wpm;
}

void channel_destroy(channel_t h)
{
    if (!h) return;
    channel_state *ch = (channel_state *)h;
    if (ch->uhsdr) uhsdr_free(ch->uhsdr);
    if (ch->bmorse) bmorse_destroy(ch->bmorse);
    free(ch);
}

} /* extern "C" */
