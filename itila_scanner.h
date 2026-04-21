/*
 * itila_scanner.h — full _ItilaScanner in C
 *
 * Compile:
 *   gcc -O3 -march=native -ffast-math -shared -fPIC \
 *       -o libitila_scanner.so itila_scanner.c -lm
 */

#ifndef ITILA_SCANNER_H
#define ITILA_SCANNER_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct ItilaSc ItilaSc;

/*
 * Create scanner.
 *   sample_rate    — input IQ rate (typically 192000)
 *   center_hz      — band center in Hz (e.g. 7090000.0)
 *   max_bins       — hard cap on simultaneous channels (e.g. 80)
 *   min_snr        — energy scan threshold above median in dB (e.g. 12.0)
 *   window_samples — env samples per decode window (window_sec × 200)
 *   energy_win     — FFT size for energy scan (power of 2, e.g. 4096)
 *   grid_hz        — channel grid spacing in Hz (e.g. 100.0 for 0.1 kHz)
 *   band_min_hz    — lower edge of CW band to scan (absolute Hz)
 *   band_max_hz    — upper edge
 *   sos100_flat    — SOS coefficients for 100 Hz LPF, row-major [n_sos][6]
 *   n_sos          — number of SOS sections
 *   sos200_flat    — SOS coefficients for 200 Hz LPF
 */
/*
 *   window_samples  — env samples passed to itila_feed per call (e.g. 60s × 200 = 12000)
 *   feed_interval   — new samples needed to trigger a decode (e.g. 5s × 200 = 1000)
 *
 * Continuous mode: feed_interval < window_samples.  Each decode call peeks
 * the latest window_samples from the rolling buffer; the buffer is trimmed
 * to window_samples max so it doesn't grow without bound.
 */
ItilaSc *itila_sc_create(int sample_rate, double center_hz,
                          int max_bins, double min_snr,
                          int window_samples, int feed_interval,
                          int energy_win, double grid_hz,
                          double band_min_hz, double band_max_hz,
                          const double *sos100_flat, int n_sos,
                          const double *sos200_flat);

void itila_sc_free(ItilaSc *sc);

/*
 * Feed IQ — runs scan, DSP, accumulation in one call.
 * i_arr/q_arr: float64, length n.
 */
void itila_sc_feed_iq(ItilaSc *sc,
                       const double *i_arr, const double *q_arr, int n);

/*
 * Fill f_hz_out[] with bins that have accumulated >= feed_interval new
 * samples since the last itila_sc_advance() call.
 * Returns the count written (≤ max_out).
 */
int itila_sc_ready_bins(ItilaSc *sc, double *f_hz_out, int max_out);

/*
 * Peek: copy up to max_n of the most recent env samples into the output
 * buffers WITHOUT removing them from the bin's rolling buffer.
 * Returns count copied.
 */
int itila_sc_peek_env(ItilaSc *sc, double f_hz,
                       double *env100_out, double *env200_out, int max_n);

/*
 * Advance the decode pointer for a bin by feed_interval samples
 * and trim the buffer to at most window_samples.
 * Call this after each itila_feed() invocation.
 */
void itila_sc_advance(ItilaSc *sc, double f_hz);

/*
 * Drain up to max_n samples (removes from buffer — legacy path).
 * Returns count written (0 if bin not found or empty).
 */
int itila_sc_drain_env(ItilaSc *sc, double f_hz,
                        double *env100_out, double *env200_out, int max_n);

/* Current number of active bins. */
int itila_sc_bin_count(ItilaSc *sc);

/* Env samples buffered for a specific bin (0 if not found). */
int itila_sc_env_n(ItilaSc *sc, double f_hz);

/*
 * Fill f_hz_out[] with all active bin frequencies.
 * Returns count written (≤ max_out).
 */
int itila_sc_list_bins(ItilaSc *sc, double *f_hz_out, int max_out);

#ifdef __cplusplus
}
#endif

#endif /* ITILA_SCANNER_H */
int itila_sc_peek_env(ItilaSc *sc, double f_hz, double *env100_out, double *env200_out, int max_n);
