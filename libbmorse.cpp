/**
 * libbmorse.cpp — Bayesian CW decoder library implementation
 *
 * Wraps AG1LE's bmorse morse class in a C API for in-process use.
 * Handles: envelope detection, decimation to 200 Hz, Bayesian decode.
 */

#include "libbmorse.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

// Include bmorse headers
// The morse class and all its methods are in the bmorse source tree
#include "bmorse.h"
#include "fftfilt.h"
#include "complex.h"

// Global params required by bmorse internals (normally in bmorse.cxx)
PARAMS params = {
    FALSE, FALSE, FALSE, FALSE, FALSE, FALSE, 8192, 32, 0, 600, 5, 4000, 10.0, 0.0, 0, 0, 20, 20, FALSE
};

// FFT filter (required by some bmorse paths, may be NULL for library use)
fftfilt *FFT_filter = NULL;

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// Internal state wrapping the morse class + signal processing
struct bmorse_state {
    morse *decoder;          // AG1LE's Bayesian decoder

    // Signal processing (mirrors bmorse.cxx rx_FFTprocess + process_data)
    float sample_rate;
    float tone_freq;
    int dec_ratio;           // sample_rate / BAYES_RATE (200 Hz)
    int dec_counter;         // decimation counter

    // Envelope detection (FFT filter — matches bmorse subprocess quality)
    fftfilt *fft_filter;
    double fft_phase;
    int nominal_wpm;

    // Noise estimation
    float noise_buf[200];    // 1-second circular buffer
    int noise_pos;
    float noise_estimate;

    // Speed tracking
    float speed_wpm;

    // Output buffer
    char outbuf[1024];
    int outbuf_len;
};

// Envelope detection using bmorse's own FFT filter (matches subprocess quality)
static void init_fft_filter(bmorse_state *s) {
    int FilterFFTLen = 4096;
    float bw = (float)s->nominal_wpm / (1.2f * s->sample_rate);
    if (bw <= 0) bw = 25.0f / (1.2f * s->sample_rate);
    s->fft_filter = new fftfilt(bw, FilterFFTLen);
    s->fft_phase = 0.0;
}

static float detect_envelope_fft(bmorse_state *s, float sample) {
    // Same as bmorse's rx_FFTprocess: mix to baseband, FFT filter, magnitude
    double phase_inc = 2.0 * M_PI * s->tone_freq / s->sample_rate;
    s->fft_phase += phase_inc;
    if (s->fft_phase > 2.0 * M_PI) s->fft_phase -= 2.0 * M_PI;

    // Complex mix to baseband
    complex z_in(sample * cos(s->fft_phase), sample * sin(s->fft_phase));
    complex z_out;

    // FFT bandpass filter (same as bmorse's rx_FFTprocess)
    complex *z_out_ptr;
    int n_out = s->fft_filter->run(z_in, &z_out_ptr);

    // Magnitude of last output sample = envelope
    if (n_out > 0) {
        complex &z = z_out_ptr[n_out - 1];
        return (float)sqrt(z.re * z.re + z.im * z.im);
    }
    return 0.0f;
}

// Update noise estimate (running minimum of windowed averages)
static void update_noise(bmorse_state *s, float env) {
    s->noise_buf[s->noise_pos] = env;
    s->noise_pos = (s->noise_pos + 1) % 200;

    // Simple noise floor: percentile of buffer
    // Full sort is expensive — use running min of recent window
    float mn = 1e10f;
    for (int i = 0; i < 200; i++) {
        if (s->noise_buf[i] < mn) mn = s->noise_buf[i];
    }
    // Smooth noise estimate
    s->noise_estimate = 0.99f * s->noise_estimate + 0.01f * (mn + 0.001f);
}


extern "C" {

bmorse_handle_t bmorse_create(float freq, float sample_rate, int wpm)
{
    bmorse_state *s = (bmorse_state *)calloc(1, sizeof(bmorse_state));
    if (!s) return NULL;

    s->sample_rate = sample_rate;
    s->tone_freq = freq;
    s->dec_ratio = (int)(sample_rate / BAYES_RATE);
    if (s->dec_ratio < 1) s->dec_ratio = 1;
    s->dec_counter = 0;

    // Envelope detection via FFT filter
    s->nominal_wpm = wpm > 0 ? wpm : 25;
    s->fft_phase = 0.0;
    init_fft_filter(s);

    // Noise
    memset(s->noise_buf, 0, sizeof(s->noise_buf));
    s->noise_pos = 0;
    s->noise_estimate = 0.001f;

    // Create decoder
    s->decoder = new morse();
    if (wpm > 0) {
        s->decoder->init_speed = wpm;
    }
    s->speed_wpm = wpm > 0 ? (float)wpm : 25.0f;

    s->outbuf_len = 0;

    return (bmorse_handle_t)s;
}

int bmorse_feed(bmorse_handle_t h, const int16_t *samples, int n,
                char *out, int outlen)
{
    if (!h || !samples || n <= 0) return 0;
    bmorse_state *s = (bmorse_state *)h;

    s->outbuf_len = 0;

    for (int i = 0; i < n; i++) {
        float sample = (float)samples[i] / 32768.0f;

        // FFT filter: mix to baseband, bandpass filter
        double phase_inc = 2.0 * M_PI * s->tone_freq / s->sample_rate;
        s->fft_phase += phase_inc;
        if (s->fft_phase > M_PI) s->fft_phase -= 2.0 * M_PI;
        else if (s->fft_phase < -M_PI) s->fft_phase += 2.0 * M_PI;

        complex z_in(sample * cos(s->fft_phase), sample * sin(s->fft_phase));
        complex *zp;
        int n_filt = s->fft_filter->run(z_in, &zp);

        if (n_filt == 0) continue;

        // Process all filtered output samples (overlap-save produces blocks)
        for (int fi = 0; fi < n_filt; fi++) {
            s->dec_counter++;
            if (s->dec_counter % s->dec_ratio != 0) continue;  // decimate

            // Demodulate: magnitude = envelope
            float env = (float)zp[fi].mag();

            // Update noise estimate
            update_noise(s, env);

            // Feed to Bayesian decoder
            long int xhat, elmhat, imax;
            float px, spdhat, pmax;
            char buf[64] = {0};

            int result = s->decoder->proces_(
                env,                    // z: envelope sample
                s->noise_estimate,      // rn: noise estimate
                &xhat,                  // keystate estimate
                &px,                    // keystate probability
                &elmhat,               // element estimate
                &spdhat,               // speed estimate
                &imax,                 // best path index
                &pmax,                 // best path probability
                buf                    // decoded character output
            );

            // Update speed
            if (spdhat > 0) s->speed_wpm = spdhat;

            // Accumulate output
            if (buf[0] != '\0') {
                int len = strlen(buf);
                for (int j = 0; j < len && s->outbuf_len < (int)sizeof(s->outbuf) - 1; j++) {
                    s->outbuf[s->outbuf_len++] = buf[j];
                }
            }
        } // for fi (filtered output samples)
    } // for i (input samples)

    // Copy to caller
    int ncopy = s->outbuf_len;
    if (ncopy > outlen - 1) ncopy = outlen - 1;
    if (ncopy > 0) {
        memcpy(out, s->outbuf, ncopy);
        out[ncopy] = '\0';
    }
    return ncopy;
}

int bmorse_get_wpm(bmorse_handle_t h)
{
    if (!h) return 0;
    bmorse_state *s = (bmorse_state *)h;
    return (int)(s->speed_wpm + 0.5f);
}

void bmorse_destroy(bmorse_handle_t h)
{
    if (!h) return;
    bmorse_state *s = (bmorse_state *)h;
    delete s->decoder;
    if (s->fft_filter) delete s->fft_filter;
    free(s);
}

} // extern "C"
