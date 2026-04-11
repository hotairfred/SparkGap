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
#include "cw_pfb.h"
#include "uhsdr_cw_lib.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#include <complex>
#include <mutex>
#include <string>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#ifdef _OPENMP
#include <omp.h>
#endif

namespace {

// Per-channel slot. The PFB-aware fields (bin_idx, residual_hz, ...) are
// only meaningful when the channel was added via cw_disp_add_pfb_channel
// AND the dispatcher has a valid PFB. The legacy v1 cw_disp_add_channel
// path leaves them at their defaults — those channels are still fed via
// cw_disp_feed_batch.
struct channel_slot_t {
    uhsdr_handle_t handle;   // NULL == slot is free
    uint32_t       generation;
    float          tone_freq;
    float          sample_rate;     // PCM rate fed to uhsdr
    float          rf_khz;
    float          snr_db;
    std::string    accum;           // Decoded text since last drain

    // PFB-aware fields. is_pfb=false → legacy v1 channel.
    bool           is_pfb;
    int            bin_idx;          // PFB bin to extract
    float          residual_hz;      // freq_offset - bin_centre
    int            dec_factor;       // pfb_output_rate / sample_rate
    float          shift_phase;      // running phase for tone shift
    float          peak_avg;         // normalize state

    // Audio ring for cw_disp_get_channel_audio (pitch detection support).
    // Capacity is fixed; oldest samples are overwritten on overflow.
    static constexpr int kAudioRingCap = 12000 * 60;  // ~60 s @ 12 kHz
    std::vector<int16_t> audio_ring;
    int                  audio_head;  // next write index
    int                  audio_count; // number of valid samples
    int                  audio_read;  // next read index (relative to head-count)
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
    cw_pfb_t                    *pfb;  // NULL until cw_disp_init_pfb
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
        d->slots[i].handle      = NULL;
        d->slots[i].generation  = 0;
        d->slots[i].is_pfb      = false;
        d->slots[i].audio_head  = 0;
        d->slots[i].audio_count = 0;
        d->slots[i].audio_read  = 0;
        d->slots[i].peak_avg    = 0.0f;
        d->slots[i].shift_phase = 0.0f;
    }
    d->pfb = NULL;
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
    if (d->pfb) {
        cw_pfb_destroy(d->pfb);
        d->pfb = NULL;
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
    s.is_pfb      = false;
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
    s.is_pfb       = false;
    s.audio_count  = 0;
    s.audio_head   = 0;
    s.audio_read   = 0;
    s.peak_avg     = 0.0f;
    s.shift_phase  = 0.0f;
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

// ----------------------------------------------------------------------------
// v2 IQ-fed API
// ----------------------------------------------------------------------------

int cw_disp_init_pfb(cw_dispatcher_handle_t d,
                     int input_rate,
                     int n_chan,
                     int oversample,
                     int taps_per_chan)
{
    if (!d) return -1;
    std::lock_guard<std::mutex> lk(d->mu);

    // Tear down any existing PFB and clear PFB-flagged channels.
    if (d->pfb) {
        for (auto &s : d->slots) {
            if (s.handle && s.is_pfb) {
                uhsdr_free(s.handle);
                s.handle = NULL;
                s.is_pfb = false;
                s.accum.clear();
                s.audio_count = 0;
                s.audio_head  = 0;
                s.audio_read  = 0;
                s.generation  = (s.generation + 1u) & 0xFFu;
            }
        }
        cw_pfb_destroy(d->pfb);
        d->pfb = NULL;
    }
    d->pfb = cw_pfb_create(input_rate, n_chan, oversample, taps_per_chan);
    return d->pfb ? 0 : -1;
}

int cw_disp_add_pfb_channel(cw_dispatcher_handle_t d,
                            float freq_offset_hz,
                            float output_rate,
                            float tone_freq,
                            int   wpm,
                            float rf_khz,
                            float snr_db)
{
    if (!d) return -1;

    std::lock_guard<std::mutex> lk(d->mu);

    if (!d->pfb) return -1;

    int pfb_rate = cw_pfb_output_rate(d->pfb);
    if (pfb_rate <= 0 || output_rate <= 0.0f) return -1;
    if ((int)output_rate <= 0 || pfb_rate % (int)output_rate != 0) return -1;
    int dec_factor = pfb_rate / (int)output_rate;
    if (dec_factor < 1) return -1;

    // Compute bin index and residual.
    int   n_chan = cw_pfb_n_chan(d->pfb);
    float bin_spacing = cw_pfb_bin_spacing(d->pfb);
    int bin_idx = (int)lroundf(freq_offset_hz / bin_spacing);
    bin_idx = ((bin_idx % n_chan) + n_chan) % n_chan;
    float bin_centre = (float)bin_idx * bin_spacing;
    if (bin_centre > (float)cw_pfb_input_rate(d->pfb) / 2.0f) {
        bin_centre -= (float)cw_pfb_input_rate(d->pfb);
    }
    float residual_hz = freq_offset_hz - bin_centre;

    // Find a free slot.
    int slot = -1;
    for (int i = 0; i < d->max_channels; ++i) {
        if (d->slots[i].handle == NULL) { slot = i; break; }
    }
    if (slot < 0) return -1;

    // Init uhsdr at tone_freq, output_rate.
    uhsdr_handle_t h = uhsdr_init(tone_freq, output_rate, wpm);
    if (!h) return -1;

    channel_slot_t &s = d->slots[slot];
    s.handle      = h;
    s.tone_freq   = tone_freq;
    s.sample_rate = output_rate;
    s.rf_khz      = rf_khz;
    s.snr_db      = snr_db;
    s.is_pfb      = true;
    s.bin_idx     = bin_idx;
    s.residual_hz = residual_hz;
    s.dec_factor  = dec_factor;
    s.shift_phase = 0.0f;
    s.peak_avg    = 0.0f;
    s.audio_ring.assign(channel_slot_t::kAudioRingCap, 0);
    s.audio_head  = 0;
    s.audio_count = 0;
    s.audio_read  = 0;
    s.accum.clear();

    return pack_id((uint32_t)slot, s.generation);
}

// Per-channel work for one IQ block. Reads a contiguous bin row from the
// PFB output, frequency-shifts so the carrier lands at tone_freq Hz in the
// real-valued audio (matching openskimmer.py PFBChannel), decimates,
// peak-normalises to int16, appends to the audio ring, and feeds uhsdr.
static void process_pfb_channel(channel_slot_t *s,
                                const std::complex<float> *bin_row,
                                int n_steps,
                                int pfb_rate)
{
    // After the PFB the bin contains the signal at +residual_hz baseband.
    // We want the real-valued audio to carry the carrier at tone_freq Hz so
    // uhsdr's Goertzel (set up at tone_freq during uhsdr_init) locks onto
    // it. That means shifting by (tone_freq - residual_hz) Hz.
    //
    // Python:  shift_hz = CW_TONE - residual_hz
    //          audio = (ch * exp(1j*2π*shift_hz*t)).real
    const float shift_hz = s->tone_freq - s->residual_hz;
    const float dphi = 2.0f * (float)M_PI * shift_hz / (float)pfb_rate;

    int dec = s->dec_factor;
    int n_out = n_steps / dec;
    if (n_out <= 0) return;

    // Build decimated PCM into a stack-friendly buffer (n_out samples).
    // For uhsdr at 12 kHz, 100 ms IQ block → 100 ms / 8 µs = ... hmm,
    // pfb_rate = 24000, n_steps = 2400 (100 ms), dec=2, n_out=1200.
    // Stack alloca() is fine up to a few KB; for safety use a vector.
    std::vector<int16_t> pcm(n_out);

    float peak = s->peak_avg;
    float local_peak = 0.0f;
    float ph = s->shift_phase;

    // First pass: compute shifted real samples + track local peak.
    std::vector<float> shifted(n_out);
    for (int i = 0; i < n_out; ++i) {
        // Decimate by taking every dec-th sample. To match scipy's
        // simple slicing this is direct subsampling (no anti-alias).
        // PFB already band-limits each bin to a fraction of bin spacing.
        std::complex<float> v = bin_row[i * dec];
        // Phase angle at this output index in undecimated steps:
        float a = ph + dphi * (float)(i * dec);
        float c = cosf(a), si = sinf(a);
        std::complex<float> rot(c, si);
        std::complex<float> y = v * rot;
        float r = y.real();
        shifted[i] = r;
        float ar = fabsf(r);
        if (ar > local_peak) local_peak = ar;
    }

    // Update running peak (matches openskimmer.py PFBChannel logic):
    //   if peak > self._peak: self._peak = peak
    //   else:                 self._peak = 0.9999*self._peak + 0.0001*peak
    if (local_peak > peak) peak = local_peak;
    else                   peak = 0.9999f * peak + 0.0001f * local_peak;
    s->peak_avg = peak;

    // Convert to int16 with peak normalisation.
    if (peak > 1e-9f) {
        const float gain = 0.3f / peak * 32767.0f;
        for (int i = 0; i < n_out; ++i) {
            float v = shifted[i] * gain;
            if (v >  32767.0f) v =  32767.0f;
            if (v < -32767.0f) v = -32767.0f;
            pcm[i] = (int16_t)v;
        }
    } else {
        memset(pcm.data(), 0, sizeof(int16_t) * (size_t)n_out);
    }

    // Advance shift phase for next call.
    s->shift_phase = ph + dphi * (float)(n_out * dec);
    // Wrap to keep magnitude bounded.
    s->shift_phase = fmodf(s->shift_phase, 2.0f * (float)M_PI);

    // Append to audio ring (overwriting oldest if full).
    int cap = (int)s->audio_ring.size();
    int written = 0;
    while (written < n_out) {
        int chunk = n_out - written;
        int room  = cap - s->audio_head;
        if (chunk > room) chunk = room;
        memcpy(&s->audio_ring[s->audio_head], &pcm[written],
               sizeof(int16_t) * (size_t)chunk);
        s->audio_head = (s->audio_head + chunk) % cap;
        s->audio_count += chunk;
        if (s->audio_count > cap) {
            // Drop oldest by sliding the read pointer forward.
            s->audio_read += (s->audio_count - cap);
            if (s->audio_read >= cap) s->audio_read %= cap;
            s->audio_count = cap;
        }
        written += chunk;
    }

    // Feed uhsdr — appends to s->accum on success.
    char outbuf[1024];
    int n = uhsdr_feed(s->handle, pcm.data(), n_out, outbuf, sizeof(outbuf));
    if (n > 0) {
        s->accum.append(outbuf, (size_t)n);
    }
}

int cw_disp_feed_iq(cw_dispatcher_handle_t d,
                    const float *i_samples,
                    const float *q_samples,
                    int n_samples)
{
    if (!d) return -1;
    if (n_samples <= 0) return 0;
    if (!i_samples || !q_samples) return -1;

    // Run PFB outside the structural mutex (it has its own state and is
    // only touched by feed_iq, which the caller serialises).
    cw_pfb_t *pfb;
    {
        std::lock_guard<std::mutex> lk(d->mu);
        if (!d->pfb) return -1;
        pfb = d->pfb;
    }

    int n_steps = cw_pfb_process(pfb, i_samples, q_samples, n_samples);
    if (n_steps <= 0) return 0;

    int out_n_steps = 0;
    const float *out = cw_pfb_last_output(pfb, &out_n_steps);
    if (!out || out_n_steps <= 0) return 0;
    int n_chan = cw_pfb_n_chan(pfb);
    int pfb_rate = cw_pfb_output_rate(pfb);
    const std::complex<float> *out_c =
        reinterpret_cast<const std::complex<float> *>(out);

    // Snapshot live PFB channel pointers under the structural mutex.
    // The mutex protects against concurrent add/remove during the parallel
    // section. (Caller still must not call feed_iq concurrently with
    // itself, but add_pfb_channel from another thread is fine.)
    std::vector<channel_slot_t*> live;
    {
        std::lock_guard<std::mutex> lk(d->mu);
        live.reserve(d->max_channels);
        for (auto &s : d->slots) {
            if (s.handle && s.is_pfb) live.push_back(&s);
        }
    }

    // Parallel fan-out.
    int n_live = (int)live.size();
    #pragma omp parallel for schedule(static)
    for (int i = 0; i < n_live; ++i) {
        channel_slot_t *s = live[i];
        const std::complex<float> *bin_row =
            out_c + (size_t)s->bin_idx * (size_t)out_n_steps;
        process_pfb_channel(s, bin_row, out_n_steps, pfb_rate);
    }

    return 0;
}

int cw_disp_get_channel_audio(cw_dispatcher_handle_t d,
                              int channel_id,
                              int16_t *out,
                              int max_samples)
{
    if (!d || !out || max_samples <= 0) return 0;
    std::lock_guard<std::mutex> lk(d->mu);

    uint32_t slot = unpack_slot(channel_id);
    uint32_t gen  = unpack_gen(channel_id);
    if ((int)slot >= d->max_channels) return 0;
    channel_slot_t &s = d->slots[slot];
    if (!s.handle || !s.is_pfb) return 0;
    if ((s.generation & 0xFFu) != gen) return 0;

    int cap = (int)s.audio_ring.size();
    if (cap == 0 || s.audio_count == 0) return 0;

    // Available = audio_count - already-read. We track how much we've read
    // out separately so successive calls return new audio without losing
    // anything written between calls.
    // The simplest model: drop everything we return.
    int n_avail = s.audio_count;
    if (n_avail > max_samples) n_avail = max_samples;
    // Tail of the ring (oldest) is at (head - count + cap) % cap.
    int oldest = ((s.audio_head - s.audio_count) % cap + cap) % cap;
    int written = 0;
    while (written < n_avail) {
        int chunk = n_avail - written;
        int room  = cap - oldest;
        if (chunk > room) chunk = room;
        memcpy(&out[written], &s.audio_ring[oldest],
               sizeof(int16_t) * (size_t)chunk);
        oldest = (oldest + chunk) % cap;
        written += chunk;
    }
    s.audio_count -= n_avail;
    return n_avail;
}

} // extern "C"
