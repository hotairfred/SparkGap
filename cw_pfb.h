/**
 * cw_pfb.h — Polyphase filter bank channelizer (standalone module).
 *
 * Mirrors openskimmer.py's PFBChannelizer (numpy):
 *   - n_chan bins, oversample = N/M, output_rate = input_rate * os / n_chan
 *   - Prototype: firwin lowpass at 1/(2*n_chan), kaiser(beta=10) window
 *   - Scaling: h *= sqrt(n_chan) / taps_per_chan  → unity passband gain
 *   - Per-step IFFT (size n_chan), then per-bin baseband phase correction
 *     so each bin output sits at the residual frequency (NOT at f0)
 *
 * Built on FFTW3 (single precision). Plans are created once at
 * cw_pfb_create() and reused for every cw_pfb_process() call.
 *
 * Output is laid out (n_chan, n_steps) row-major as complex64 (interleaved
 * float real, imag). cw_pfb_last_output() returns a pointer + n_steps so
 * downstream consumers can extract a single bin row by stride.
 */
#ifndef CW_PFB_H
#define CW_PFB_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct cw_pfb_t cw_pfb_t;

cw_pfb_t *cw_pfb_create(int input_rate,
                        int n_chan,
                        int oversample,
                        int taps_per_chan);

void cw_pfb_destroy(cw_pfb_t *pfb);

int   cw_pfb_input_rate(const cw_pfb_t *pfb);
int   cw_pfb_output_rate(const cw_pfb_t *pfb);
int   cw_pfb_n_chan(const cw_pfb_t *pfb);
float cw_pfb_bin_spacing(const cw_pfb_t *pfb);

/**
 * Ingest one block of complex IQ samples (n_in float pairs).
 * Returns the number of output steps produced (>= 0). The output is
 * stored internally and accessible via cw_pfb_last_output().
 *
 * If n_in is too small to produce at least one step, returns 0 and
 * leaves last_output unchanged for this call.
 */
int cw_pfb_process(cw_pfb_t *pfb,
                   const float *i_samples,
                   const float *q_samples,
                   int n_in);

/**
 * Pointer to the most recent output. Layout: (n_chan, n_steps) row-major
 * complex64 (real, imag interleaved). The pointer is valid until the
 * next call to cw_pfb_process() or cw_pfb_destroy().
 *
 * Returns NULL if no block has been processed yet.
 */
const float *cw_pfb_last_output(const cw_pfb_t *pfb, int *out_n_steps);

#ifdef __cplusplus
}
#endif

#endif /* CW_PFB_H */
