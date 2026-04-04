/**
 * uhsdr_cw_lib.h — Reusable CW decoder API (extracted from uhsdr_cw.cpp)
 *
 * Based on UHSDR Firmware Project by Loftur E. Jonasson (TF3LJ), GPL-3.
 * Adapted for multi-instance in-process use by the OpenSkimmer project.
 *
 * Usage:
 *   uhsdr_handle_t h = uhsdr_init(700.0f, 12000.0f, 0);  // freq, rate, wpm (0=auto)
 *   while (have_audio) {
 *       int n = uhsdr_feed(h, samples, nsamples, buf, buflen);
 *       if (n > 0) process_decoded_text(buf, n);
 *   }
 *   int wpm = uhsdr_get_wpm(h);
 *   uhsdr_free(h);
 */

#ifndef UHSDR_CW_LIB_H
#define UHSDR_CW_LIB_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Opaque handle to a decoder instance. */
typedef struct cw_decoder_t* uhsdr_handle_t;

/**
 * Create a new CW decoder instance.
 *
 * @param freq       CW tone frequency in Hz (e.g. 700)
 * @param sample_rate Audio sample rate in Hz (e.g. 12000)
 * @param wpm        Initial speed (0 = auto-detect)
 * @return           Handle, or NULL on failure
 */
uhsdr_handle_t uhsdr_init(float freq, float sample_rate, int wpm);

/**
 * Feed audio samples to the decoder.
 *
 * @param h       Decoder handle
 * @param samples Pointer to 16-bit signed mono PCM samples
 * @param n       Number of samples
 * @param out     Buffer to receive decoded characters (ASCII)
 * @param outlen  Size of output buffer
 * @return        Number of characters written to out (0 if none)
 */
int uhsdr_feed(uhsdr_handle_t h, const int16_t *samples, int n,
               char *out, int outlen);

/**
 * Get the current estimated WPM.
 *
 * @param h  Decoder handle
 * @return   Estimated WPM (0 if not yet locked)
 */
int uhsdr_get_wpm(uhsdr_handle_t h);

/**
 * Destroy a decoder instance and free resources.
 *
 * @param h  Decoder handle (NULL-safe)
 */
void uhsdr_free(uhsdr_handle_t h);

#ifdef __cplusplus
}
#endif

#endif /* UHSDR_CW_LIB_H */
