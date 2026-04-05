/**
 * cw_engine.h — Streaming CW channelizer + multi-speed decoder library
 *
 * Hot path: IQ → SSB mix → FIR (VOLK) → decimate → uhsdr × N speeds
 * Returns raw decoded text per decoder — Python SpotTracker handles matching.
 */

#ifndef CW_ENGINE_H
#define CW_ENGINE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Initialize the engine (no SCP needed — matching done in Python).
 * @return 0 on success
 */
int cw_engine_init(const char *scp_path);
void cw_engine_shutdown(void);

typedef void* channel_t;

/**
 * Create a channel for one CW signal.
 * Internally creates multi-speed uhsdr decoders after pitch detection.
 */
channel_t channel_create(float offset_hz, float sample_rate);

/**
 * Feed IQ samples. Channelizes, decimates, feeds all decoders.
 * No spots returned — use channel_read_text() to get decoded text.
 */
void channel_feed_iq(channel_t h,
                     const float *i_samples, const float *q_samples, int n);

/**
 * Get the number of decoder instances in this channel.
 */
int channel_decoder_count(channel_t h);

/**
 * Read new decoded text from a specific decoder instance.
 * Returns number of new chars since last call. Text is null-terminated.
 * @param decoder_idx  0..channel_decoder_count()-1
 * @param buf          Output buffer for new text
 * @param buflen       Size of output buffer
 * @param wpm          Output: current WPM estimate for this decoder
 * @return             Number of new chars written to buf
 */
int channel_read_text(channel_t h, int decoder_idx,
                      char *buf, int buflen, int *wpm);

/** Get the WPM speed setting for a decoder (0=auto, 15, 30, etc.) */
int channel_decoder_speed(channel_t h, int decoder_idx);

/** Get the detected pitch for this channel (after pitch detection). */
float channel_get_pitch(channel_t h);

void channel_set_pitch(channel_t h, float pitch_hz);
int channel_get_wpm(channel_t h);
void channel_destroy(channel_t h);

#ifdef __cplusplus
}
#endif

#endif /* CW_ENGINE_H */
