/**
 * cw_dispatcher.h — Parallel CW decoder dispatcher
 *
 * Owns a pool of uhsdr_cw decoder instances and fans out per-channel
 * PCM audio across them using an OpenMP thread pool. Lets Python hand
 * over one batch of channelized audio per IQ block and get back batched
 * decoded text without ever holding the GIL during the fanout.
 *
 * Usage (from Python via ctypes — GIL is released automatically by
 * ctypes for the duration of each C call):
 *
 *   h = cw_disp_create(256)
 *   cid0 = cw_disp_add_channel(h, 700.0f, 12000.0f, 0, 7032.5f, 28.0f)
 *   cid1 = cw_disp_add_channel(h, 620.0f, 12000.0f, 0, 7034.0f, 19.0f)
 *
 *   for each IQ block:
 *     // Python PFB produces one int16 PCM row per channel.
 *     // Build a contiguous (n_channels, n_samples) int16 buffer and
 *     // a matching channel_ids[] array (one id per row), then:
 *     cw_disp_feed_batch(h, channel_ids, n_channels, samples, n_samples);
 *
 *   // At a lower cadence (e.g. 100–500 ms):
 *   n = cw_disp_drain(h, records, max_records);
 *
 *   cw_disp_remove_channel(h, cid0);
 *   cw_disp_destroy(h);
 *
 * Thread safety:
 *   - cw_disp_add_channel / cw_disp_remove_channel are serialized by an
 *     internal structural mutex and also serialize against concurrent
 *     cw_disp_feed_batch.
 *   - cw_disp_feed_batch may be called from at most one thread at a
 *     time. Inside, OpenMP fans out across channels.
 *   - cw_disp_drain may be called from any thread but must not run
 *     concurrently with cw_disp_feed_batch on the same dispatcher.
 */
#ifndef CW_DISPATCHER_H
#define CW_DISPATCHER_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct cw_dispatcher_t* cw_dispatcher_handle_t;

/**
 * One batched decoded-text record returned by cw_disp_drain.
 * text[] is a NUL-terminated buffer; text_len is the number of bytes
 * written (not including the trailing NUL).
 */
typedef struct {
    int   channel_id;
    float rf_khz;
    float snr_db;
    int   wpm;
    int   text_len;
    char  text[256];
} cw_decoded_record_t;

/**
 * Create a dispatcher able to hold at most `max_channels` concurrent
 * uhsdr decoder instances. Returns NULL on allocation failure.
 */
cw_dispatcher_handle_t cw_disp_create(int max_channels);

/**
 * Destroy a dispatcher. NULL-safe. Frees all owned decoder instances.
 */
void cw_disp_destroy(cw_dispatcher_handle_t d);

/**
 * Add a channel. Returns a non-negative channel_id on success or -1 on
 * failure (pool full / allocation error).
 *
 * @param tone_freq   CW tone frequency (Hz), e.g. 700.0
 * @param sample_rate PCM sample rate (Hz), typically 12000
 * @param wpm         Fixed speed, 0 = auto-detect
 * @param rf_khz      RF tag for the channel (carried through to drain)
 * @param snr_db      SNR tag (carried through to drain)
 */
int cw_disp_add_channel(cw_dispatcher_handle_t d,
                        float tone_freq,
                        float sample_rate,
                        int   wpm,
                        float rf_khz,
                        float snr_db);

/**
 * Remove a channel. NULL-safe and no-op if the channel_id is not live.
 * Any decoded text still buffered for that channel is discarded.
 */
void cw_disp_remove_channel(cw_dispatcher_handle_t d, int channel_id);

/**
 * Return the number of currently live channels.
 */
int cw_disp_channel_count(cw_dispatcher_handle_t d);

/**
 * Feed one batch of per-channel PCM audio.
 *
 * `samples` points to a contiguous int16 buffer of shape
 * (n_channels_in_batch, n_samples), row-major (channel-major). Row i
 * belongs to channel_ids[i]. It is legal (and normal) to pass a batch
 * that is a subset of currently live channels — caller decides which
 * channels get fed in any given call.
 *
 * Returns 0 on success, -1 if the dispatcher is NULL, or -2 if any
 * channel_id is unknown (in which case no channels are fed).
 */
int cw_disp_feed_batch(cw_dispatcher_handle_t d,
                       const int *channel_ids,
                       int n_channels_in_batch,
                       const int16_t *samples,
                       int n_samples);

/**
 * Drain decoded text from all live channels.
 *
 * Writes up to `max_records` records into `out`. A record is emitted
 * for every channel that has at least one new character since the last
 * drain. Channels with no new text are skipped. Returns the number of
 * records written.
 */
int cw_disp_drain(cw_dispatcher_handle_t d,
                  cw_decoded_record_t *out,
                  int max_records);

/**
 * Query the decoder's current WPM estimate for a channel.
 * Returns 0 if the channel is unknown or WPM has not yet locked.
 */
int cw_disp_get_wpm(cw_dispatcher_handle_t d, int channel_id);

#ifdef __cplusplus
}
#endif

#endif /* CW_DISPATCHER_H */
