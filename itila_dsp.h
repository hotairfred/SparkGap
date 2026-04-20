/*
 * itila_dsp.h — _ItilaScanner DSP core (C port)
 *
 * Handles the per-bin hot path: complex mixing, 16:1 block-average
 * decimation, IIR sosfilt (100 Hz + 200 Hz), envelope extraction,
 * 60:1 block-average decimation to 200 Hz.
 *
 * FFT energy scan and bin spawn/eviction stay in Python.
 *
 * Compile:
 *   gcc -O3 -march=native -ffast-math -shared -fPIC \
 *       -o libitila_dsp.so itila_dsp.c -lm
 */

#ifndef ITILA_DSP_H
#define ITILA_DSP_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct ItilaDsp ItilaDsp;

/*
 * Create DSP engine.
 *   sample_rate  — input IQ rate (typically 192000)
 *   center_hz    — band center in Hz (e.g. 7090000.0)
 *   max_bins     — hard cap on simultaneous channels
 *   sos100_flat  — SOS coefficients for 100 Hz LPF, row-major [n_sos][6]
 *   n_sos        — number of SOS sections (e.g. 3 for Butterworth order 6)
 *   sos200_flat  — SOS coefficients for 200 Hz LPF, same layout
 */
ItilaDsp *itila_dsp_create(int sample_rate, double center_hz, int max_bins,
                            const double *sos100_flat, int n_sos,
                            const double *sos200_flat);

void itila_dsp_free(ItilaDsp *dsp);

/* Add a frequency bin (f_hz absolute).  Returns 1 on success, 0 if full. */
int  itila_dsp_add_bin(ItilaDsp *dsp, double f_hz);

/* Remove a bin.  Silently ignores unknown frequencies. */
void itila_dsp_remove_bin(ItilaDsp *dsp, double f_hz);

int  itila_dsp_bin_count(ItilaDsp *dsp);

/*
 * Feed IQ samples.  i_arr/q_arr are float64, length n.
 * Internally handles carry-over residual so non-multiple-of-16 chunks
 * don't silently drop samples.
 */
void itila_dsp_feed(ItilaDsp *dsp,
                    const double *i_arr, const double *q_arr, int n);

/*
 * How many 200 Hz envelope samples are buffered for the given bin.
 * Returns 0 if bin not found.
 */
int  itila_dsp_env_n(ItilaDsp *dsp, double f_hz);

/*
 * Drain up to max_n samples from the bin's envelope buffer into
 * env100_out and env200_out.  Returns the number of samples written.
 * Remaining samples are shifted down (memmove).
 */
int  itila_dsp_drain_env(ItilaDsp *dsp, double f_hz,
                          double *env100_out, double *env200_out, int max_n);

#ifdef __cplusplus
}
#endif

#endif /* ITILA_DSP_H */
