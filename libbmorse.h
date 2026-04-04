/**
 * libbmorse.h — Bayesian CW decoder library API
 *
 * Wraps AG1LE's bmorse Bayesian Viterbi decoder in a reusable library.
 * Same API pattern as libuhsdr_cw — drop-in replacement with Bayesian
 * decoding instead of threshold decoding.
 *
 * Based on: "Machine Recognition of Hand-Sent Morse Code" (Guenther, 1973)
 * Implementation: AG1LE (Mauri Niininen), GPL-3
 */

#ifndef LIBBMORSE_H
#define LIBBMORSE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void* bmorse_handle_t;

/**
 * Create a new Bayesian CW decoder instance.
 *
 * @param freq       CW tone frequency in Hz (e.g. 700)
 * @param sample_rate Audio sample rate in Hz (e.g. 4000)
 * @param wpm        Initial speed hint (0 = auto, seeds paths at 10-50 WPM)
 * @return           Handle, or NULL on failure
 */
bmorse_handle_t bmorse_create(float freq, float sample_rate, int wpm);

/**
 * Feed audio samples to the decoder.
 *
 * @param h       Decoder handle
 * @param samples Pointer to 16-bit signed mono PCM samples
 * @param n       Number of samples
 * @param out     Buffer to receive decoded characters
 * @param outlen  Size of output buffer
 * @return        Number of characters written to out
 */
int bmorse_feed(bmorse_handle_t h, const int16_t *samples, int n,
                char *out, int outlen);

/**
 * Get current estimated WPM.
 */
int bmorse_get_wpm(bmorse_handle_t h);

/**
 * Destroy decoder instance.
 */
void bmorse_destroy(bmorse_handle_t h);

#ifdef __cplusplus
}
#endif

#endif /* LIBBMORSE_H */
