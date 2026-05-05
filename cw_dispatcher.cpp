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
#include "libbmorse.h"

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

// Decoder type for PFB-aware channels.
enum decoder_kind_t {
    DEC_NONE   = 0,   // slot free
    DEC_UHSDR  = 1,
    DEC_BMORSE = 2,
};

// Per-channel slot. The PFB-aware fields (bin_idx, residual_hz, ...) are
// only meaningful when the channel was added via cw_disp_add_pfb_channel
// AND the dispatcher has a valid PFB. The legacy v1 cw_disp_add_channel
// path leaves them at their defaults — those channels are still fed via
// cw_disp_feed_batch.
struct channel_slot_t {
    decoder_kind_t  kind;             // DEC_NONE == slot is free

    // Decoder handles. Only one is non-NULL at a time (tag = kind).
    uhsdr_handle_t  uhsdr;
    bmorse_handle_t bmorse;

    uint32_t        generation;
    float           tone_freq;
    float           sample_rate;      // PCM rate fed to decoder
    float           rf_khz;
    float           snr_db;
    std::string     accum;            // Decoded text since last drain

    // PFB-aware fields. is_pfb=false → legacy v1 channel.
    bool            is_pfb;
    int             bin_idx;          // PFB bin to extract
    float           residual_hz;      // freq_offset - bin_centre
    int             dec_factor;       // pfb_output_rate / sample_rate
    float           shift_phase;      // running phase for tone shift
    float           peak_avg;         // normalize state

    // Per-channel FIR state for bmorse bandpass (unused for uhsdr).
    // Taps themselves live on the dispatcher (shared). This is the
    // delay line, size = n_taps - 1.
    std::vector<float> fir_delay;

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

    // Shared FIR taps used by all bmorse channels. Installed via
    // cw_disp_set_bmorse_fir. Empty == no FIR (pass-through).
    // Dual filter: wide taps for fast CW (>threshold_wpm), narrow for slow/DX.
    std::vector<float>           bmorse_fir_taps;          // wide (default)
    std::vector<float>           bmorse_fir_taps_narrow;   // narrow (for ≤threshold_wpm)
    int                          bmorse_fir_threshold_wpm; // 0 = single-width mode
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
        d->slots[i].kind        = DEC_NONE;
        d->slots[i].uhsdr       = NULL;
        d->slots[i].bmorse      = NULL;
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

// Helper: free the decoder owned by a slot (whichever kind) and reset
// tag fields. Does NOT touch accum, generation, or audio_ring metadata.
static void free_slot_decoder(channel_slot_t &s)
{
    if (s.kind == DEC_UHSDR && s.uhsdr) {
        uhsdr_free(s.uhsdr);
    } else if (s.kind == DEC_BMORSE && s.bmorse) {
        bmorse_destroy(s.bmorse);
    }
    s.uhsdr  = NULL;
    s.bmorse = NULL;
    s.kind   = DEC_NONE;
}

void cw_disp_destroy(cw_dispatcher_handle_t d)
{
    if (!d) return;
    for (auto &s : d->slots) {
        free_slot_decoder(s);
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
        if (d->slots[i].kind == DEC_NONE) { slot = i; break; }
    }
    if (slot < 0) return -1;  // pool full

    // uhsdr_init touches a global template slot in uhsdr_cw_lib.cpp —
    // serialized by our structural mutex, which we already hold.
    uhsdr_handle_t h = uhsdr_init(tone_freq, sample_rate, wpm);
    if (!h) return -1;

    channel_slot_t &s = d->slots[slot];
    s.kind        = DEC_UHSDR;
    s.uhsdr       = h;
    s.bmorse      = NULL;
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
    if (s.kind == DEC_NONE) return;
    if ((s.generation & 0xFFu) != gen) return;  // stale id, ignore

    free_slot_decoder(s);
    s.accum.clear();
    s.is_pfb       = false;
    s.audio_count  = 0;
    s.audio_head   = 0;
    s.audio_read   = 0;
    s.peak_avg     = 0.0f;
    s.shift_phase  = 0.0f;
    s.fir_delay.clear();
    s.generation = (s.generation + 1u) & 0xFFu;
}

int cw_disp_channel_count(cw_dispatcher_handle_t d)
{
    if (!d) return 0;
    std::lock_guard<std::mutex> lk(d->mu);
    int n = 0;
    for (auto &s : d->slots) {
        if (s.kind != DEC_NONE) ++n;
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
            if (s.kind != DEC_UHSDR) return -2;  // legacy API is uhsdr-only
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
        int n = uhsdr_feed(s->uhsdr, row, n_samples, out, sizeof(out));
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
        if (s.kind == DEC_NONE) continue;
        if (s.accum.empty()) continue;

        cw_decoded_record_t &r = out[n_out++];
        r.channel_id = pack_id((uint32_t)slot, s.generation);
        r.rf_khz     = s.rf_khz;
        r.snr_db     = s.snr_db;
        r.wpm        = (s.kind == DEC_UHSDR)  ? uhsdr_get_wpm(s.uhsdr)
                    : (s.kind == DEC_BMORSE) ? bmorse_get_wpm(s.bmorse)
                    : 0;

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
    if (s.kind == DEC_NONE) return 0;
    if ((s.generation & 0xFFu) != gen) return 0;

    if (s.kind == DEC_UHSDR)  return uhsdr_get_wpm(s.uhsdr);
    if (s.kind == DEC_BMORSE) return bmorse_get_wpm(s.bmorse);
    return 0;
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
            if (s.kind != DEC_NONE && s.is_pfb) {
                free_slot_decoder(s);
                s.is_pfb = false;
                s.accum.clear();
                s.audio_count = 0;
                s.audio_head  = 0;
                s.audio_read  = 0;
                s.fir_delay.clear();
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
        if (d->slots[i].kind == DEC_NONE) { slot = i; break; }
    }
    if (slot < 0) return -1;

    // Init uhsdr at tone_freq, output_rate.
    uhsdr_handle_t h = uhsdr_init(tone_freq, output_rate, wpm);
    if (!h) return -1;

    channel_slot_t &s = d->slots[slot];
    s.kind        = DEC_UHSDR;
    s.uhsdr       = h;
    s.bmorse      = NULL;
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
    s.fir_delay.clear();

    return pack_id((uint32_t)slot, s.generation);
}

// Per-channel work for one IQ block. Reads a contiguous bin row from the
// PFB output, frequency-shifts so the carrier lands at tone_freq Hz in the
// real-valued audio (matching sparkgap.py PFBChannel), decimates,
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

    // Update running peak (matches sparkgap.py PFBChannel logic):
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
    int n = uhsdr_feed(s->uhsdr, pcm.data(), n_out, outbuf, sizeof(outbuf));
    if (n > 0) {
        s->accum.append(outbuf, (size_t)n);
    }
}

// bmorse variant of process_pfb_channel: same PFB bin extract + shift +
// decimate pattern, but with a post-decimation FIR bandpass and a call
// to bmorse_feed (single-threaded — libbmorse is not thread-safe).
static void process_pfb_bmorse_channel(channel_slot_t *s,
                                       const std::complex<float> *bin_row,
                                       int n_steps,
                                       int pfb_rate,
                                       const std::vector<float> &fir_taps_wide,
                                       const std::vector<float> &fir_taps_narrow,
                                       int threshold_wpm)
{
    const float shift_hz = s->tone_freq - s->residual_hz;
    const float dphi = 2.0f * (float)M_PI * shift_hz / (float)pfb_rate;

    int dec = s->dec_factor;
    int n_out = n_steps / dec;
    if (n_out <= 0) return;

    // Dual filter width: select narrow taps for slow CW (≤threshold_wpm),
    // wide taps for fast CW. Use wide as default until spdhat is known.
    // SDC uses this to get better SNR on weak DX (narrow) while keeping
    // full keying sidebands on fast contest exchanges (wide).
    int spdhat = bmorse_get_wpm(s->bmorse);
    const std::vector<float> &fir_taps =
        (threshold_wpm > 0 && spdhat > 0 && spdhat <= threshold_wpm
         && !fir_taps_narrow.empty())
        ? fir_taps_narrow : fir_taps_wide;

    // Ensure FIR delay line is sized (n_taps state entries).
    // Both narrow and wide should use the same n_taps (256) so the delay
    // line stays valid across width switches. If they differ, resize.
    int n_taps = (int)fir_taps.size();
    if (n_taps > 0 && (int)s->fir_delay.size() != n_taps) {
        s->fir_delay.assign(n_taps, 0.0f);
    }

    // Shift + decimate (same math as uhsdr path).
    std::vector<float> shifted(n_out);
    float ph = s->shift_phase;
    for (int i = 0; i < n_out; ++i) {
        std::complex<float> v = bin_row[i * dec];
        float a = ph + dphi * (float)(i * dec);
        float c = cosf(a), si = sinf(a);
        std::complex<float> rot(c, si);
        std::complex<float> y = v * rot;
        shifted[i] = y.real();
    }
    s->shift_phase = fmodf(ph + dphi * (float)(n_out * dec), 2.0f * (float)M_PI);

    // Post-decimation FIR bandpass. Direct-form with a per-instance
    // delay line so state carries across blocks. If no taps are
    // installed, pass-through.
    if (n_taps > 0) {
        std::vector<float> filtered(n_out);
        for (int i = 0; i < n_out; ++i) {
            // Shift delay line: delay[0] = newest, delay[n_taps-1] = oldest.
            for (int k = n_taps - 1; k > 0; --k) {
                s->fir_delay[k] = s->fir_delay[k - 1];
            }
            s->fir_delay[0] = shifted[i];
            float acc = 0.0f;
            for (int k = 0; k < n_taps; ++k) {
                acc += fir_taps[k] * s->fir_delay[k];
            }
            filtered[i] = acc;
        }
        shifted.swap(filtered);
    }

    // Peak-normalise to int16.
    float peak = s->peak_avg;
    float local_peak = 0.0f;
    for (float v : shifted) {
        float a = fabsf(v);
        if (a > local_peak) local_peak = a;
    }
    if (local_peak > peak) peak = local_peak;
    else                   peak = 0.9999f * peak + 0.0001f * local_peak;
    s->peak_avg = peak;

    std::vector<int16_t> pcm(n_out);
    if (peak > 1e-9f) {
        const float gain = 0.3f / peak * 32767.0f;
        for (int i = 0; i < n_out; ++i) {
            float v = shifted[i] * gain;
            if (v >  32767.0f) v =  32767.0f;
            if (v < -32767.0f) v = -32767.0f;
            pcm[i] = (int16_t)v;
        }
    }

    // Append to audio ring (same as uhsdr path).
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
            s->audio_read += (s->audio_count - cap);
            if (s->audio_read >= cap) s->audio_read %= cap;
            s->audio_count = cap;
        }
        written += chunk;
    }

    // Feed bmorse — NOT thread safe, caller must serialise.
    char outbuf[1024];
    int n = bmorse_feed(s->bmorse, pcm.data(), n_out, outbuf, sizeof(outbuf));
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

    // Snapshot live PFB channel pointers under the structural mutex,
    // split by decoder kind. uhsdr channels fan out under OpenMP; bmorse
    // channels run serially (libbmorse is not thread-safe).
    std::vector<channel_slot_t*> live_uhsdr;
    std::vector<channel_slot_t*> live_bmorse;
    std::vector<float>           fir_wide_snap;
    std::vector<float>           fir_narrow_snap;
    int                          fir_threshold_wpm;
    {
        std::lock_guard<std::mutex> lk(d->mu);
        live_uhsdr.reserve(d->max_channels);
        live_bmorse.reserve(d->max_channels);
        for (auto &s : d->slots) {
            if (!s.is_pfb) continue;
            if (s.kind == DEC_UHSDR)  live_uhsdr.push_back(&s);
            else if (s.kind == DEC_BMORSE) live_bmorse.push_back(&s);
        }
        fir_wide_snap     = d->bmorse_fir_taps;
        fir_narrow_snap   = d->bmorse_fir_taps_narrow;
        fir_threshold_wpm = d->bmorse_fir_threshold_wpm;
    }

    // Parallel fan-out for uhsdr channels.
    int n_uhsdr = (int)live_uhsdr.size();
    #pragma omp parallel for schedule(static)
    for (int i = 0; i < n_uhsdr; ++i) {
        channel_slot_t *s = live_uhsdr[i];
        const std::complex<float> *bin_row =
            out_c + (size_t)s->bin_idx * (size_t)out_n_steps;
        process_pfb_channel(s, bin_row, out_n_steps, pfb_rate);
    }

    // Parallel fan-out for bmorse channels. libbmorse is now fully
    // re-entrant as of arc-bmorse-reentrant — all function-local
    // statics migrated into ProcessState, noise_/trelis_ statics
    // promoted to morse class members, output buffer + FFT_filter
    // moved off globals. Concurrent bmorse_feed on different handles
    // is safe; verified by a 4-handle OpenMP test producing the same
    // output as sequential feeding.
    int n_bmorse = (int)live_bmorse.size();
    #pragma omp parallel for schedule(static)
    for (int i = 0; i < n_bmorse; ++i) {
        channel_slot_t *s = live_bmorse[i];
        const std::complex<float> *bin_row =
            out_c + (size_t)s->bin_idx * (size_t)out_n_steps;
        process_pfb_bmorse_channel(s, bin_row, out_n_steps, pfb_rate,
                                   fir_wide_snap, fir_narrow_snap,
                                   fir_threshold_wpm);
    }

    (void)n_chan;  // not used directly; referenced via PFB helpers
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
    if (s.kind == DEC_NONE || !s.is_pfb) return 0;
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

// ----------------------------------------------------------------------------
// bmorse v3 additions
// ----------------------------------------------------------------------------

int cw_disp_set_bmorse_fir(cw_dispatcher_handle_t d,
                           const float *taps,
                           int n_taps)
{
    if (!d) return -1;
    if (n_taps < 0 || n_taps > 1024) return -1;
    if (n_taps > 0 && !taps) return -1;

    std::lock_guard<std::mutex> lk(d->mu);
    d->bmorse_fir_taps.assign(taps, taps + n_taps);
    return 0;
}

// Dual filter width: install narrow taps and a WPM threshold.
// Channels with spdhat ≤ threshold_wpm use narrow; above use wide.
// Set threshold_wpm=0 to disable (single-width mode).
int cw_disp_set_bmorse_fir_narrow(cw_dispatcher_handle_t d,
                                  const float *taps, int n_taps,
                                  int threshold_wpm)
{
    if (!d) return -1;
    if (n_taps < 0 || n_taps > 1024) return -1;
    if (n_taps > 0 && !taps) return -1;

    std::lock_guard<std::mutex> lk(d->mu);
    if (n_taps > 0) {
        d->bmorse_fir_taps_narrow.assign(taps, taps + n_taps);
    } else {
        d->bmorse_fir_taps_narrow.clear();
    }
    d->bmorse_fir_threshold_wpm = threshold_wpm;
    return 0;
}

int cw_disp_add_pfb_bmorse_channel(cw_dispatcher_handle_t d,
                                   float freq_offset_hz,
                                   float sample_rate,
                                   float tone_freq,
                                   int   wpm,
                                   float rf_khz,
                                   float snr_db)
{
    if (!d) return -1;

    std::lock_guard<std::mutex> lk(d->mu);

    if (!d->pfb) return -1;

    int pfb_rate = cw_pfb_output_rate(d->pfb);
    if (pfb_rate <= 0 || sample_rate <= 0.0f) return -1;
    if ((int)sample_rate <= 0 || pfb_rate % (int)sample_rate != 0) return -1;
    int dec_factor = pfb_rate / (int)sample_rate;
    if (dec_factor < 1) return -1;

    // Compute bin index and residual — same geometry as the uhsdr path.
    int   n_chan      = cw_pfb_n_chan(d->pfb);
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
        if (d->slots[i].kind == DEC_NONE) { slot = i; break; }
    }
    if (slot < 0) return -1;

    // Init bmorse at tone_freq, sample_rate.
    bmorse_handle_t h = bmorse_create(tone_freq, sample_rate, wpm);
    if (!h) return -1;

    channel_slot_t &s = d->slots[slot];
    s.kind        = DEC_BMORSE;
    s.bmorse      = h;
    s.uhsdr       = NULL;
    s.tone_freq   = tone_freq;
    s.sample_rate = sample_rate;
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
    // FIR delay line will be sized on first process call (it needs the
    // taps, which live on the dispatcher).
    s.fir_delay.clear();

    return pack_id((uint32_t)slot, s.generation);
}

} // extern "C"
