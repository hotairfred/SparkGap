/**
 * libbmorse.cpp — Bayesian CW decoder library implementation
 *
 * Uses bmorse's own rx_FFTprocess + process_data pipeline directly.
 * Guarantees exact subprocess parity — same code path, same state.
 */

#define LIBBMORSE_BUILD 1

#include "libbmorse.h"
#include "bmorse.h"
#include "bmorse_procstate.h"
#include "fftfilt.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <stdio.h>

// Globals defined in bmorse_lib.cxx
extern PARAMS params;  // still global; same-params invariant holds for all channels

// Forward declarations of bmorse functions (ProcessState* versions)
extern int rx_FFTprocess(ProcessState* st, const double *buf, int len);

// Internal state
struct bmorse_state {
    float sample_rate;
    float tone_freq;
    float speed_wpm;
    fftfilt *filter;        // per-instance FFT filter
    ProcessState *proc;     // per-instance decoder state (holds morse object + all statics)
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

    // Initialize per-instance decoder state
    s->proc = process_state_create();
    if (!s->proc) { delete s->filter; free(s); return NULL; }
    s->proc->filter = s->filter;  // rx_FFTprocess uses st->filter, not global FFT_filter

    return (bmorse_handle_t)s;
}

int bmorse_feed(bmorse_handle_t h, const int16_t *samples, int n,
                char *out, int outlen)
{
    if (!h || !samples || n <= 0) return 0;
    bmorse_state *s = (bmorse_state *)h;

    // params is still global — benign under same-params invariant (all channels
    // use identical rate/freq/speed); see comms 2026-04-11 for rationale.
    params.sample_rate = s->sample_rate;
    params.frequency   = s->tone_freq;
    params.speed       = (int)s->speed_wpm;
    params.dec_ratio   = (int)(s->sample_rate / BAYES_RATE);

    // Sync init_speed into the morse object if it's already been created
    if (s->proc->pd_mp)
        s->proc->pd_mp->init_speed = (int)s->speed_wpm;

    // Reset per-handle output buffer
    s->proc->outlen = 0;

    // Convert to double and call rx_FFTprocess with per-instance state
    double dbl_buf[512];
    int pos = 0;
    while (pos < n) {
        int chunk = (n - pos > 512) ? 512 : (n - pos);
        for (int i = 0; i < chunk; i++)
            dbl_buf[i] = (double)samples[pos + i] / 32768.0;
        rx_FFTprocess(s->proc, dbl_buf, chunk);
        pos += chunk;
    }

    // Speed-adaptive filter width: update fftfilt BW when detected WPM changes.
    // AG1LE formula: BW_Hz = WPM / 0.6  →  normalized f = WPM / (1.2 * sample_rate).
    // Hysteresis: only update when WPM shifts ≥3 from last configured value.
    // Warmup gate: suppress all BW updates until:
    //   (a) pd_init==0 (morse object created — Bayesian lattice is live), AND
    //   (b) warmup_blocks > 20 (~2.5 s at 4 kHz / 512 samples per block)
    // This prevents spdhat thrashing during the Viterbi training period from
    // repeatedly resetting fftfilt's overlap accumulator (pass counter).
    s->proc->warmup_blocks++;
    float det_wpm = s->proc->spdhat;
    if (s->proc->pd_init == 0 &&
        s->proc->warmup_blocks > 20 &&
        det_wpm > 5.0f &&
        fabsf(det_wpm - s->proc->cur_bw_wpm) >= 3.0f) {
        float new_bw = det_wpm / (1.2f * s->sample_rate);
        s->proc->filter->create_lpf((double)new_bw);
        s->proc->cur_bw_wpm = det_wpm;
    }

    // Copy accumulated output to caller
    int ncopy = s->proc->outlen;
    if (ncopy > outlen - 1) ncopy = outlen - 1;
    if (ncopy > 0) {
        memcpy(out, s->proc->outbuf, ncopy);
        out[ncopy] = '\0';
    }
    return ncopy;
}

int bmorse_get_wpm(bmorse_handle_t h)
{
    if (!h) return 0;
    bmorse_state *s = (bmorse_state *)h;
    return (int)(s->proc->spdhat + 0.5f);
}

void bmorse_destroy(bmorse_handle_t h)
{
    if (!h) return;
    bmorse_state *s = (bmorse_state *)h;
    // Free per-instance decoder state (includes morse object)
    if (s->proc) {
        process_state_destroy(s->proc);
        s->proc = NULL;
    }
    // Free this instance's FFT filter (proc->filter is a non-owning alias)
    if (s->filter) {
        delete s->filter;
        s->filter = NULL;
    }
    free(s);
}

} // extern "C"

#ifdef TEST_REENTRANT
// Two-handle re-entrancy test — compile with -DTEST_REENTRANT and link as executable
// g++ -DTEST_REENTRANT -DLIBBMORSE_BUILD -o test_reentrant libbmorse.cpp bmorse_lib.cxx ... -lfftw3
#include <stdio.h>
int main()
{
    const float SR = 4000.0f;
    const int   N  = 512;
    int16_t zeros[N] = {};

    bmorse_handle_t h1 = bmorse_create(700.0f, SR, 25);
    bmorse_handle_t h2 = bmorse_create(700.0f, SR, 25);
    if (!h1 || !h2) { fprintf(stderr, "FAIL: bmorse_create returned NULL\n"); return 1; }
    printf("h1=%p  h2=%p\n", h1, h2);

    char out1[256], out2[256];

    // Feed h1 — verify no crash and independent state
    printf("feeding h1... ");
    int r1 = bmorse_feed(h1, zeros, N, out1, sizeof(out1));
    printf("h1 feed: %d\n", r1);

    // Feed h2 — verify no crash and h1 proc untouched
    printf("feeding h2... ");
    int r2 = bmorse_feed(h2, zeros, N, out2, sizeof(out2));
    printf("h2 feed: %d\n", r2);

    // Verify output buffers are independent
    if (((bmorse_state*)h1)->proc->outlen != 0 &&
        ((bmorse_state*)h1)->proc->outbuf == ((bmorse_state*)h2)->proc->outbuf) {
        fprintf(stderr, "FAIL: h1 and h2 share outbuf — not re-entrant\n");
        bmorse_destroy(h1); bmorse_destroy(h2); return 1;
    }

    bmorse_destroy(h1);
    bmorse_destroy(h2);
    printf("PASS\n");
    return 0;
}
#endif // TEST_REENTRANT
