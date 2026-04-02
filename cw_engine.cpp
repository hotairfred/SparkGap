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
#include <fftw3.h>
#include <set>
#include <string>
#include <chrono>

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

/* FIR lowpass filter design (windowed sinc, Hamming window) */
static float *design_fir(int ntaps, float cutoff, float sample_rate) {
    float *fir = (float *)malloc(ntaps * sizeof(float));
    float nyq = sample_rate / 2.0f;
    float fc = cutoff / nyq;
    int M = ntaps - 1;
    float sum = 0;
    for (int i = 0; i <= M; i++) {
        float n = i - M / 2.0f;
        if (fabsf(n) < 1e-6f)
            fir[i] = 2.0f * fc;
        else
            fir[i] = sinf(2.0f * M_PI * fc * n) / (M_PI * n);
        fir[i] *= 0.54f - 0.46f * cosf(2.0f * M_PI * i / M);
        sum += fir[i];
    }
    for (int i = 0; i <= M; i++) fir[i] /= sum;
    return fir;
}

/* Per-channel dedup: track emitted calls with timestamps */
#define MAX_RECENT_SPOTS 64
#define DEDUP_WINDOW_SEC 60.0

struct recent_spot {
    char callsign[16];
    std::chrono::steady_clock::time_point emit_time;
};

struct channel_state {
    float offset_hz;
    float sample_rate;
    float pitch_hz;

    /* Mixer state */
    double phase;
    double phase_inc;

    /* FIR lowpass + decimation for uhsdr (12kHz) */
    float *uhsdr_fir;
    int    uhsdr_fir_len;
    float *uhsdr_fir_buf;      /* circular input buffer for FIR */
    int    uhsdr_fir_pos;
    int    uhsdr_dec_factor;
    int    uhsdr_dec_count;

    /* FIR lowpass + decimation for bmorse (4kHz) */
    float *bmorse_fir;
    int    bmorse_fir_len;
    float *bmorse_fir_buf;
    int    bmorse_fir_pos;
    int    bmorse_dec_factor;
    int    bmorse_dec_count;

    /* Peak normalization */
    float peak;

    /* Decoder handles */
    uhsdr_handle_t uhsdr;
    bmorse_handle_t bmorse;

    /* Decoded text accumulators */
    char uhsdr_text[8192];
    int  uhsdr_text_len;
    char bmorse_text[8192];
    int  bmorse_text_len;

    /* Pitch detection (runs on uhsdr FIR output after 15s) */
    float *pitch_buf;          /* accumulate 12kHz decimated audio */
    int    pitch_buf_len;
    int    pitch_buf_cap;
    bool   pitch_detected;

    /* Speed-adaptive bmorse (respawn with correct WPM) */
    bool   bmorse_fir_adapted;
    int    last_wpm;
    long   samples_fed;

    /* Per-channel spot dedup (wall clock) */
    recent_spot recent[MAX_RECENT_SPOTS];
    int recent_count;
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

    /* FIR filters for decimation */
    ch->uhsdr_dec_factor = (int)(sample_rate / UHSDR_RATE);
    ch->bmorse_dec_factor = (int)(sample_rate / BMORSE_RATE);

    /* uhsdr FIR: 255 taps for 16× decimation (192k→12k needs strong stopband) */
    ch->uhsdr_fir_len = 255;
    ch->uhsdr_fir = design_fir(ch->uhsdr_fir_len, UHSDR_RATE / 2.0f * 0.8f, sample_rate);
    ch->uhsdr_fir_buf = (float *)calloc(ch->uhsdr_fir_len, sizeof(float));
    ch->uhsdr_fir_pos = 0;
    ch->uhsdr_dec_count = 0;

    /* bmorse FIR: cutoff at BMORSE_RATE/2 * 0.8 */
    ch->bmorse_fir_len = ch->bmorse_dec_factor * 4 + 1;
    if (ch->bmorse_fir_len > 255) ch->bmorse_fir_len = 255;
    if (ch->bmorse_fir_len % 2 == 0) ch->bmorse_fir_len++;
    ch->bmorse_fir = design_fir(ch->bmorse_fir_len, BMORSE_RATE / 2.0f * 0.8f, sample_rate);
    ch->bmorse_fir_buf = (float *)calloc(ch->bmorse_fir_len, sizeof(float));
    ch->bmorse_fir_pos = 0;
    ch->bmorse_dec_count = 0;

    ch->peak = 0.0f;

    /* Create decoders */
    ch->uhsdr = uhsdr_init(CW_TONE, (float)UHSDR_RATE, 0);
    ch->bmorse = bmorse_create(CW_TONE, (float)BMORSE_RATE, 0);

    ch->uhsdr_text_len = 0;
    ch->bmorse_text_len = 0;
    ch->recent_count = 0;

    /* Pitch detection: disabled for now */
    ch->pitch_buf_cap = 0;
    ch->pitch_buf = NULL;
    ch->pitch_buf_len = 0;
    ch->pitch_detected = true;  /* skip pitch detection */

    /* Speed-adaptive bmorse */
    ch->bmorse_fir_adapted = true; // disabled — respawn hurts
    ch->last_wpm = 0;
    ch->samples_fed = 0;

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
    ch->samples_fed += n;

    for (int i = 0; i < n; i++) {
        /* SSB mix: I*cos + Q*sin → mono audio with CW tone at pitch_hz */
        float phase_f = (float)ch->phase;
        float mixed = i_samples[i] * cosf(phase_f) + q_samples[i] * sinf(phase_f);
        ch->phase += ch->phase_inc;
        if (ch->phase > 2.0 * M_PI) ch->phase -= 2.0 * M_PI;
        if (ch->phase < -2.0 * M_PI) ch->phase += 2.0 * M_PI;

        /* FIR lowpass + decimate for uhsdr (12kHz) */
        ch->uhsdr_fir_buf[ch->uhsdr_fir_pos] = mixed;
        ch->uhsdr_fir_pos = (ch->uhsdr_fir_pos + 1) % ch->uhsdr_fir_len;
        ch->uhsdr_dec_count++;
        if (ch->uhsdr_dec_count >= ch->uhsdr_dec_factor) {
            ch->uhsdr_dec_count = 0;
            /* Convolve */
            float sum = 0;
            for (int j = 0; j < ch->uhsdr_fir_len; j++) {
                int idx = (ch->uhsdr_fir_pos + j) % ch->uhsdr_fir_len;
                sum += ch->uhsdr_fir_buf[idx] * ch->uhsdr_fir[j];
            }

            /* Pitch detection: accumulate FIR output for 15s */
            if (!ch->pitch_detected && ch->pitch_buf_len < ch->pitch_buf_cap) {
                ch->pitch_buf[ch->pitch_buf_len++] = sum;
                if (ch->pitch_buf_len >= ch->pitch_buf_cap) {
                    /* FFT on full 15s of 12kHz audio for reliable pitch */
                    int fft_n = ch->pitch_buf_len;

                    /* Use FFTW for fast computation */
                    double *fft_in = (double *)fftw_malloc(fft_n * sizeof(double));
                    fftw_complex *fft_out = (fftw_complex *)fftw_malloc((fft_n/2+1) * sizeof(fftw_complex));
                    for (int k = 0; k < fft_n; k++) {
                        double win = 0.5 - 0.5 * cos(2.0 * M_PI * k / (fft_n-1));
                        fft_in[k] = ch->pitch_buf[k] * win;
                    }
                    fftw_plan p = fftw_plan_dft_r2c_1d(fft_n, fft_in, fft_out, FFTW_ESTIMATE);
                    fftw_execute(p);
                    fftw_destroy_plan(p);

                    /* Find peak in 450-850 Hz range */
                    float freq_res = (float)UHSDR_RATE / fft_n;
                    int lo_bin = (int)(450.0f / freq_res);
                    int hi_bin = (int)(850.0f / freq_res);
                    float peak_mag = 0;
                    int peak_bin = lo_bin;
                    float total_mag = 0;
                    int n_bins = 0;

                    for (int b = lo_bin; b <= hi_bin && b < fft_n/2; b++) {
                        float mag = (float)(fft_out[b][0]*fft_out[b][0] + fft_out[b][1]*fft_out[b][1]);
                        total_mag += mag;
                        n_bins++;
                        if (mag > peak_mag) {
                            peak_mag = mag;
                            peak_bin = b;
                        }
                    }
                    fftw_free(fft_in);
                    fftw_free(fft_out);

                    /* SNR check: peak must be 10dB above mean */
                    float mean_mag = (n_bins > 1) ? (total_mag - peak_mag) / (n_bins - 1) : 1e-10f;
                    float snr_db = 10.0f * log10f(peak_mag / (mean_mag + 1e-20f));

                    float detected_pitch = peak_bin * freq_res;
                    int pitch_int = (int)(detected_pitch + 0.5f);
                    if (pitch_int < 450) pitch_int = 450;
                    if (pitch_int > 850) pitch_int = 850;

                    if (snr_db >= 10.0f && abs(pitch_int - (int)ch->pitch_hz) > 5) {
                        /* Confident retune: update mixer + reinit uhsdr only */
                        ch->pitch_hz = (float)pitch_int;
                        ch->phase_inc = 2.0 * M_PI * (ch->offset_hz - pitch_int) / ch->sample_rate;
                        if (ch->uhsdr) uhsdr_free(ch->uhsdr);
                        ch->uhsdr = uhsdr_init((float)pitch_int, (float)UHSDR_RATE, 0);
                        ch->uhsdr_text_len = 0;
                        /* Keep bmorse running (less pitch-sensitive) */
                        fprintf(stderr, "  pitch: %.0f → %d Hz (SNR %.1f dB) for offset %.0f\n",
                                CW_TONE, pitch_int, snr_db, ch->offset_hz);
                    }
                    ch->pitch_detected = true;
                    free(ch->pitch_buf);
                    ch->pitch_buf = NULL;
                }
            }

            /* Peak normalize */
            float absv = fabsf(sum);
            if (absv > ch->peak) ch->peak = absv;
            else ch->peak = 0.9999f * ch->peak + 0.0001f * absv;
            if (ch->peak > 0) sum = sum / ch->peak * 0.3f;
            int16_t s = (int16_t)(sum * 32767.0f);
            if (s > 32767) s = 32767; if (s < -32767) s = -32767;
            int nc = uhsdr_feed(ch->uhsdr, &s, 1, dec_buf, sizeof(dec_buf));
            if (nc > 0 && ch->uhsdr_text_len + nc < 8191) {
                memcpy(ch->uhsdr_text + ch->uhsdr_text_len, dec_buf, nc);
                ch->uhsdr_text_len += nc;
            }
        }

        /* FIR lowpass + decimate for bmorse (4kHz) */
        ch->bmorse_fir_buf[ch->bmorse_fir_pos] = mixed;
        ch->bmorse_fir_pos = (ch->bmorse_fir_pos + 1) % ch->bmorse_fir_len;
        ch->bmorse_dec_count++;
        if (ch->bmorse_dec_count >= ch->bmorse_dec_factor) {
            ch->bmorse_dec_count = 0;
            float sum = 0;
            for (int j = 0; j < ch->bmorse_fir_len; j++) {
                int idx = (ch->bmorse_fir_pos + j) % ch->bmorse_fir_len;
                sum += ch->bmorse_fir_buf[idx] * ch->bmorse_fir[j];
            }
            float absv = fabsf(sum);
            if (absv > ch->peak) ch->peak = absv;
            if (ch->peak > 0) sum = sum / ch->peak * 0.3f;
            int16_t s = (int16_t)(sum * 32767.0f);
            if (s > 32767) s = 32767; if (s < -32767) s = -32767;
            int nc = bmorse_feed(ch->bmorse, &s, 1, dec_buf, sizeof(dec_buf));
            if (nc > 0 && ch->bmorse_text_len + nc < 8191) {
                memcpy(ch->bmorse_text + ch->bmorse_text_len, dec_buf, nc);
                ch->bmorse_text_len += nc;
            }
        }
    }

    /* Speed-adaptive bmorse narrow bandpass (after decimation, before bmorse) */
    /* NOTE: This does NOT touch the bmorse anti-alias FIR (that stays for decimation).
       Instead, we apply an additional narrow CW bandpass on the decimated 4kHz audio.
       The bandpass is implemented inline in the bmorse feed path above — but we need
       a separate filter state. For now, leave this as TODO and test if the basic
       approach of narrowing at the bmorse.so's internal FFT filter level works instead.
       bmorse already has an internal FFT filter (fftfilt) — its bandwidth was set at
       bmorse_create time. A simpler approach: destroy and recreate bmorse with a
       tighter bandwidth when WPM stabilizes. */
    if (!ch->bmorse_fir_adapted && ch->uhsdr) {
        int wpm = uhsdr_get_wpm(ch->uhsdr);
        if (wpm > 0) ch->last_wpm = wpm;

        /* After 30s (enough for uhsdr to lock), recreate bmorse with tight BW */
        if (ch->last_wpm > 0 && ch->samples_fed > (int)(ch->sample_rate * 30)) {
            if (ch->bmorse) bmorse_destroy(ch->bmorse);
            ch->bmorse = bmorse_create(ch->pitch_hz, (float)BMORSE_RATE, ch->last_wpm);
            ch->bmorse_text_len = 0;
            ch->bmorse_fir_adapted = true;
            fprintf(stderr, "  bmorse respawn: WPM=%d for offset %.0f\n",
                    ch->last_wpm, ch->offset_hz);
        }
    }

    /* Extract spots from accumulated text — with per-channel dedup */
    auto emit_deduped = [&](const char *text, int text_len, int decoder_id, int wpm) {
        if (text_len < 4) return;
        char clean[8192];
        int clen = 0;
        for (int j = 0; j < text_len && clen < 8191; j++) {
            char c = text[j];
            if (c == '[') { /* skip [err] tags */
                while (j < text_len && text[j] != ']') j++;
                continue;
            }
            if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9'))
                clean[clen++] = c;
        }
        clean[clen] = '\0';

        for (int wlen = 4; wlen <= 7 && wlen <= clen; wlen++) {
            for (int j = 0; j <= clen - wlen && spot_count < max_spots; j++) {
                char frag[8];
                memcpy(frag, clean + j, wlen);
                frag[wlen] = '\0';
                if (!is_callsign_shaped(frag, wlen)) continue;
                if (strcmp(frag, "TEST") == 0) continue;
                if (!g_scp.count(std::string(frag))) continue;

                /* Per-channel dedup: skip if emitted in last 60s (wall clock) */
                auto now = std::chrono::steady_clock::now();
                bool recently_emitted = false;
                for (int k = 0; k < ch->recent_count; k++) {
                    if (strcmp(ch->recent[k].callsign, frag) == 0) {
                        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
                            now - ch->recent[k].emit_time).count();
                        if (elapsed < (int)DEDUP_WINDOW_SEC) {
                            recently_emitted = true;
                            break;
                        }
                    }
                }
                if (recently_emitted) continue;

                /* Emit spot */
                strncpy(spots[spot_count].callsign, frag, 15);
                spots[spot_count].callsign[15] = '\0';
                spots[spot_count].freq_offset_hz = ch->offset_hz;
                spots[spot_count].snr_db = 0;
                spots[spot_count].wpm = wpm;
                spots[spot_count].decoder = decoder_id;
                spot_count++;

                /* Record in dedup list (circular overwrite if full) */
                int slot = ch->recent_count < MAX_RECENT_SPOTS ?
                           ch->recent_count++ :
                           (ch->recent_count++ % MAX_RECENT_SPOTS);
                strncpy(ch->recent[slot].callsign, frag, 15);
                ch->recent[slot].emit_time = now;
            }
        }
    };

    emit_deduped(ch->uhsdr_text, ch->uhsdr_text_len, 0, uhsdr_get_wpm(ch->uhsdr));
    emit_deduped(ch->bmorse_text, ch->bmorse_text_len, 1, bmorse_get_wpm(ch->bmorse));

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
    free(ch->uhsdr_fir);
    free(ch->uhsdr_fir_buf);
    free(ch->bmorse_fir);
    free(ch->bmorse_fir_buf);
    free(ch->pitch_buf);
    free(ch);
}

} /* extern "C" */
