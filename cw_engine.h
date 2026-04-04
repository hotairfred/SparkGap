/**
 * cw_engine.h — Streaming CW channelizer + dual decoder library
 *
 * Owns the entire hot path: IQ → channelize → uhsdr + bmorse → spots.
 * Python calls channel_feed_iq() with raw IQ per signal.
 * C++ does mix + FIR + decimate + both decoders internally.
 */

#ifndef CW_ENGINE_H
#define CW_ENGINE_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Decoded spot from a decoder */
typedef struct {
    char callsign[16];       /* SCP-matched callsign */
    float freq_offset_hz;    /* offset from center in Hz */
    float snr_db;            /* signal SNR */
    int wpm;                 /* decoder's WPM estimate */
    int decoder;             /* 0=uhsdr, 1=bmorse */
} cw_spot_t;

/**
 * Initialize the engine — load SCP database once.
 *
 * @param scp_path  Path to MASTER.SCP or COMBINED.SCP
 * @return          0 on success, -1 on failure
 */
int cw_engine_init(const char *scp_path);

/**
 * Shut down the engine — free SCP database.
 */
void cw_engine_shutdown(void);

/** Opaque handle to a per-signal channel */
typedef void* channel_t;

/**
 * Create a channel for one CW signal.
 *
 * @param offset_hz    Signal offset from center frequency (Hz)
 * @param sample_rate  IQ sample rate (e.g. 192000)
 * @return             Channel handle, or NULL on failure
 */
channel_t channel_create(float offset_hz, float sample_rate);

/**
 * Set the CW pitch for this channel (after pitch detection).
 *
 * @param h        Channel handle
 * @param pitch_hz Detected CW tone frequency (e.g. 700)
 */
void channel_set_pitch(channel_t h, float pitch_hz);

/**
 * Feed IQ samples and get back decoded spots.
 *
 * @param h          Channel handle
 * @param i_samples  I channel samples (float, at sample_rate)
 * @param q_samples  Q channel samples (float, at sample_rate)
 * @param n          Number of samples
 * @param spots      Output array for decoded spots
 * @param max_spots  Size of spots array
 * @return           Number of spots written (0 if none)
 */
int channel_feed_iq(channel_t h,
                    const float *i_samples, const float *q_samples, int n,
                    cw_spot_t *spots, int max_spots);

/**
 * Get the current WPM estimate for this channel.
 */
int channel_get_wpm(channel_t h);

/**
 * Destroy a channel and free resources.
 */
void channel_destroy(channel_t h);

#ifdef __cplusplus
}
#endif

#endif /* CW_ENGINE_H */
