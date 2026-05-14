/*
 * itila.h — ITILA CW decoder public API
 *
 * Bayesian CW decoder: envelope in, callsigns out.
 * Input: envelope signal at BAYES_RATE (200 Hz) — post-LPF, post-decimation.
 * Output: newline-separated decoded callsigns.
 *
 * Usage:
 *   itila_t h = itila_create(200, 100);
 *   const char *spots = itila_feed(h, envelope, n_samples, freq_khz);
 *   // spots = "W1AW\nK1GU\n" or "" if nothing found
 *   itila_free(h);
 *
 * Thread safety: each handle is independent; do not share handles across threads.
 */

#ifndef ITILA_H
#define ITILA_H

#ifdef __cplusplus
extern "C" {
#endif

typedef void* itila_t;

/* Create a decoder handle.
 *   sample_rate: envelope sample rate in Hz (should be 200)
 *   lpf_hz:      channelizing LPF bandwidth — informational only (LPF applied upstream)
 * Returns NULL on allocation failure. */
itila_t itila_create(int sample_rate, double lpf_hz);

/* Feed an envelope chunk and decode.
 *   envelope:  array of n double-precision envelope samples at sample_rate Hz
 *   n:         number of samples (up to ~180000 for a 15-min chunk)
 *   freq_khz:  channel center frequency — passed through to caller for context
 *   ev_thresh: log Bayes factor threshold; channels below this are skipped
 * Returns pointer to internal null-terminated string of newline-separated
 * callsigns.  Valid until next call to itila_feed or itila_free.
 * Returns "" if no callsigns found or evidence too low. */
const char* itila_feed(itila_t h, const double* envelope, int n,
                       double freq_khz, double ev_thresh);

/* Free handle and all associated memory. */
void itila_free(itila_t h);

/* Return the timing-cost (segmentation-quality confidence) score from the
 * most recent itila_feed / itila_feed_online call.  Lower = cleaner Morse
 * timing.  Returns 999.0 if no decode has been performed yet, or if the
 * most recent feed had insufficient structure to score.
 *
 * Designed to be ANDed with existing emit filters (SCP / patt3ch /
 * consensus) — catches the noise-decoded-to-real-call class (M5M / G7D /
 * N3T) but cannot distinguish a clean Morse non-callsign decode from a
 * real callsign, so it's a complement, not a replacement.
 *
 * Validated 2026-05-14 against B1_seg2: real calls peak at cost ~15,
 * 3-char garbage timing-fragment decodes cluster median 19 / p75 36.
 * Recommended gate: emit only if cost <= 30. */
double itila_get_last_cost(itila_t h);

/* BAYES_RATE: envelope sample rate this library expects */
#define ITILA_BAYES_RATE 200

#ifdef __cplusplus
}
#endif

#endif /* ITILA_H */
