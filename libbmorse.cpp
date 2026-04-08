/**
 * libbmorse.cpp — Bayesian CW decoder library implementation
 *
 * Uses bmorse's own rx_FFTprocess + process_data pipeline directly.
 * Guarantees exact subprocess parity — same code path, same state.
 */

#define LIBBMORSE_BUILD 1

#include "libbmorse.h"
#include "bmorse.h"
#include "fftfilt.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

// Global output buffer (written by process_data in library mode)
char _bmorse_outbuf[4096];
int _bmorse_outlen = 0;
float _bmorse_spdhat = 0;

// Globals defined in bmorse_lib.cxx
extern PARAMS params;
extern fftfilt *FFT_filter;

// Forward declarations of bmorse functions
extern int rx_FFTprocess(const double *buf, int len);

// Internal state
struct bmorse_state {
    float sample_rate;
    float tone_freq;
    float speed_wpm;
    fftfilt *filter;   // per-instance filter — global FFT_filter must not be freed by destroy
};

extern "C" {

bmorse_handle_t bmorse_create(float freq, float sample_rate, int wpm)
{
    bmorse_state *s = (bmorse_state *)calloc(1, sizeof(bmorse_state));
    if (!s) return NULL;

    s->sample_rate = sample_rate;
    s->tone_freq = freq;
    s->speed_wpm = wpm > 0 ? (float)wpm : 25.0f;

    // Configure global params (process_data reads these)
    params.sample_rate = sample_rate;
    params.frequency = freq;
    params.speed = wpm;
    params.dec_ratio = (int)(sample_rate / BAYES_RATE);
    params.agc = TRUE;
    params.print_text = TRUE;
    params.print_variables = FALSE;
    params.print_speed = FALSE;

    // Initialize FFT filter (per-instance — owned by this handle)
    int FilterFFTLen = 4096;
    float bw = (wpm > 0 ? wpm : 25) / (1.2f * sample_rate);
    s->filter = new fftfilt(bw, FilterFFTLen);
    FFT_filter = s->filter;   // point global at this instance's filter

    // Clear output buffer
    _bmorse_outlen = 0;
    _bmorse_spdhat = 0;

    return (bmorse_handle_t)s;
}

int bmorse_feed(bmorse_handle_t h, const int16_t *samples, int n,
                char *out, int outlen)
{
    if (!h || !samples || n <= 0) return 0;
    bmorse_state *s = (bmorse_state *)h;

    // Restore per-handle params and filter so this call uses the right state.
    // Bayesian statics in process_data are still shared — single-instance quality
    // for concurrent use, but no crash.
    params.sample_rate = s->sample_rate;
    params.frequency   = s->tone_freq;
    params.speed       = (int)s->speed_wpm;
    params.dec_ratio   = (int)(s->sample_rate / BAYES_RATE);
    FFT_filter = s->filter;

    // Reset output buffer
    _bmorse_outlen = 0;

    // Convert to double and call rx_FFTprocess (same as process_stdin)
    // Process in 512-sample blocks (same as subprocess)
    double dbl_buf[512];
    int pos = 0;
    while (pos < n) {
        int chunk = (n - pos > 512) ? 512 : (n - pos);
        for (int i = 0; i < chunk; i++)
            dbl_buf[i] = (double)samples[pos + i] / 32768.0;
        rx_FFTprocess(dbl_buf, chunk);
        pos += chunk;
    }

    // Copy accumulated output to caller
    int ncopy = _bmorse_outlen;
    if (ncopy > outlen - 1) ncopy = outlen - 1;
    if (ncopy > 0) {
        memcpy(out, _bmorse_outbuf, ncopy);
        out[ncopy] = '\0';
    }
    return ncopy;
}

int bmorse_get_wpm(bmorse_handle_t h)
{
    return (int)(_bmorse_spdhat + 0.5f);
}

void bmorse_destroy(bmorse_handle_t h)
{
    if (!h) return;
    bmorse_state *s = (bmorse_state *)h;
    // Free this instance's own filter — do NOT null the global FFT_filter since
    // other instances may still be alive and bmorse_feed re-applies it anyway.
    if (s->filter) {
        if (FFT_filter == s->filter)
            FFT_filter = NULL;  // only null global if it still points here
        delete s->filter;
        s->filter = NULL;
    }
    free(s);
}

} // extern "C"
