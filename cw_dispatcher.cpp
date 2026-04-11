/**
 * cw_dispatcher.cpp — Parallel CW decoder dispatcher implementation.
 *
 * See cw_dispatcher.h for the public contract.
 *
 * Implementation notes:
 *   - Channels are stored in a std::vector<channel_slot_t> with stable
 *     insertion indexing. Removed slots are marked empty (handle=NULL)
 *     and reused on subsequent adds; channel_id is slot index + an
 *     incrementing generation counter packed into the top bits so stale
 *     ids from a removed channel are detected.
 *   - uhsdr_init touches a global template slot inside uhsdr_cw_lib.cpp,
 *     so concurrent init calls would race. The dispatcher serializes
 *     add / remove with an internal structural mutex.
 *   - cw_disp_feed_batch fans out across rows with OpenMP. Each thread
 *     owns one channel for the duration of the call, so no mutex is
 *     needed on the per-channel accumulator during the inner loop.
 *   - Decoded characters from each uhsdr_feed call are appended to a
 *     per-channel std::string accumulator. drain swaps these out under
 *     the structural mutex.
 */

#include "cw_dispatcher.h"
#include "uhsdr_cw_lib.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#include <mutex>
#include <string>
#include <vector>

#ifdef _OPENMP
#include <omp.h>
#endif

namespace {

// Per-channel slot.
struct channel_slot_t {
    uhsdr_handle_t handle;   // NULL == slot is free
    uint32_t       generation;
    float          tone_freq;
    float          sample_rate;
    float          rf_khz;
    float          snr_db;
    std::string    accum;    // Decoded text since last drain
};

// Pack (slot_index, generation) into a single int channel_id so callers
// can distinguish stale ids after a slot is reused.
//   bits  0..23  : slot index (up to 16 M channels — plenty)
//   bits 24..31  : generation counter (wraps at 256 reuses)
static inline int pack_id(uint32_t slot, uint32_t gen) {
    return (int)((slot & 0xFFFFFFu) | ((gen & 0xFFu) << 24));
}
static inline uint32_t unpack_slot(int id) {
    return (uint32_t)id & 0xFFFFFFu;
}
static inline uint32_t unpack_gen(int id) {
    return ((uint32_t)id >> 24) & 0xFFu;
}

} // namespace

struct cw_dispatcher_t {
    int                          max_channels;
    std::vector<channel_slot_t>  slots;
    std::mutex                   mu;   // structural mutex; protects slots vector, generation counters, and add/remove vs drain
};

extern "C" {

cw_dispatcher_handle_t cw_disp_create(int max_channels)
{
    if (max_channels <= 0) return NULL;
    cw_dispatcher_t *d = new (std::nothrow) cw_dispatcher_t();
    if (!d) return NULL;
    d->max_channels = max_channels;
    d->slots.resize(max_channels);
    for (int i = 0; i < max_channels; ++i) {
        d->slots[i].handle     = NULL;
        d->slots[i].generation = 0;
    }
    return d;
}

void cw_disp_destroy(cw_dispatcher_handle_t d)
{
    if (!d) return;
    // Free every owned uhsdr instance.
    for (auto &s : d->slots) {
        if (s.handle) {
            uhsdr_free(s.handle);
            s.handle = NULL;
        }
    }
    delete d;
}

int cw_disp_add_channel(cw_dispatcher_handle_t d,
                        float tone_freq,
                        float sample_rate,
                        int   wpm,
                        float rf_khz,
                        float snr_db)
{
    if (!d) return -1;

    std::lock_guard<std::mutex> lk(d->mu);

    // Find a free slot.
    int slot = -1;
    for (int i = 0; i < d->max_channels; ++i) {
        if (d->slots[i].handle == NULL) { slot = i; break; }
    }
    if (slot < 0) return -1;  // pool full

    // uhsdr_init touches a global template slot in uhsdr_cw_lib.cpp —
    // serialized by our structural mutex, which we already hold.
    uhsdr_handle_t h = uhsdr_init(tone_freq, sample_rate, wpm);
    if (!h) return -1;

    channel_slot_t &s = d->slots[slot];
    s.handle      = h;
    s.tone_freq   = tone_freq;
    s.sample_rate = sample_rate;
    s.rf_khz      = rf_khz;
    s.snr_db      = snr_db;
    s.accum.clear();

    return pack_id((uint32_t)slot, s.generation);
}

void cw_disp_remove_channel(cw_dispatcher_handle_t d, int channel_id)
{
    if (!d) return;

    std::lock_guard<std::mutex> lk(d->mu);

    uint32_t slot = unpack_slot(channel_id);
    uint32_t gen  = unpack_gen(channel_id);
    if ((int)slot >= d->max_channels) return;

    channel_slot_t &s = d->slots[slot];
    if (s.handle == NULL) return;
    if ((s.generation & 0xFFu) != gen) return;  // stale id, ignore

    uhsdr_free(s.handle);
    s.handle = NULL;
    s.accum.clear();
    s.generation = (s.generation + 1u) & 0xFFu;
}

int cw_disp_channel_count(cw_dispatcher_handle_t d)
{
    if (!d) return 0;
    std::lock_guard<std::mutex> lk(d->mu);
    int n = 0;
    for (auto &s : d->slots) {
        if (s.handle) ++n;
    }
    return n;
}

int cw_disp_feed_batch(cw_dispatcher_handle_t d,
                       const int *channel_ids,
                       int n_channels_in_batch,
                       const int16_t *samples,
                       int n_samples)
{
    if (!d) return -1;
    if (n_channels_in_batch <= 0 || n_samples <= 0) return 0;
    if (!channel_ids || !samples) return -1;

    // Resolve channel_ids → slot pointers under the structural mutex.
    // Then release the mutex and run the parallel fanout. Slots cannot
    // be removed concurrently because remove also takes the mutex — but
    // our contract already says feed_batch / add / remove are not
    // called concurrently. Still, holding the mutex for validation is
    // cheap and lets us fail fast on bad ids.
    std::vector<channel_slot_t*> resolved;
    resolved.resize(n_channels_in_batch);

    {
        std::lock_guard<std::mutex> lk(d->mu);
        for (int i = 0; i < n_channels_in_batch; ++i) {
            uint32_t slot = unpack_slot(channel_ids[i]);
            uint32_t gen  = unpack_gen(channel_ids[i]);
            if ((int)slot >= d->max_channels) return -2;
            channel_slot_t &s = d->slots[slot];
            if (!s.handle) return -2;
            if ((s.generation & 0xFFu) != gen) return -2;
            resolved[i] = &s;
        }
    }

    // Parallel fanout. Each iteration owns exactly one channel; no two
    // threads touch the same slot, so the per-channel accumulator is
    // safe without an extra mutex.
    #pragma omp parallel for schedule(static)
    for (int i = 0; i < n_channels_in_batch; ++i) {
        channel_slot_t *s = resolved[i];
        const int16_t *row = samples + (size_t)i * (size_t)n_samples;

        char out[1024];
        int n = uhsdr_feed(s->handle, row, n_samples, out, sizeof(out));
        if (n > 0) {
            // std::string::append is not thread-safe across threads, but
            // each thread has its own channel, so its own string.
            s->accum.append(out, (size_t)n);
        }
    }

    return 0;
}

int cw_disp_drain(cw_dispatcher_handle_t d,
                  cw_decoded_record_t *out,
                  int max_records)
{
    if (!d || !out || max_records <= 0) return 0;

    std::lock_guard<std::mutex> lk(d->mu);

    int n_out = 0;
    for (int slot = 0; slot < d->max_channels && n_out < max_records; ++slot) {
        channel_slot_t &s = d->slots[slot];
        if (!s.handle) continue;
        if (s.accum.empty()) continue;

        cw_decoded_record_t &r = out[n_out++];
        r.channel_id = pack_id((uint32_t)slot, s.generation);
        r.rf_khz     = s.rf_khz;
        r.snr_db     = s.snr_db;
        r.wpm        = uhsdr_get_wpm(s.handle);

        size_t copy_n = s.accum.size();
        if (copy_n > sizeof(r.text) - 1) copy_n = sizeof(r.text) - 1;
        memcpy(r.text, s.accum.data(), copy_n);
        r.text[copy_n] = '\0';
        r.text_len = (int)copy_n;

        // Drop what we just reported; keep any overflow for the next drain.
        if (s.accum.size() > copy_n) {
            s.accum.erase(0, copy_n);
        } else {
            s.accum.clear();
        }
    }
    return n_out;
}

int cw_disp_get_wpm(cw_dispatcher_handle_t d, int channel_id)
{
    if (!d) return 0;
    std::lock_guard<std::mutex> lk(d->mu);

    uint32_t slot = unpack_slot(channel_id);
    uint32_t gen  = unpack_gen(channel_id);
    if ((int)slot >= d->max_channels) return 0;
    channel_slot_t &s = d->slots[slot];
    if (!s.handle) return 0;
    if ((s.generation & 0xFFu) != gen) return 0;

    return uhsdr_get_wpm(s.handle);
}

} // extern "C"
