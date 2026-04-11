/**
 * cw_pfb.cpp — Polyphase filter bank channelizer (FFTW backend).
 *
 * Reference: openskimmer.py PFBChannelizer (numpy version). The math
 * here mirrors that code line for line; the comments call out where
 * the indexing differs from a textbook PFB derivation.
 *
 * Per-block algorithm:
 *   1. Append new IQ to a forward-time accumulator.
 *   2. n_steps = len(buf) / M  output samples produced this call.
 *   3. Build a "newest-first" extended sequence:
 *        full_rev[0]               = newest sample of the new block
 *        full_rev[n_steps*M + j]   = j-th most-recent prior sample
 *   4. For each output step s (chronological, s=0 oldest):
 *        for n in [0, N):
 *          y_input[s, n] = Σ_k h[k*N + n] * full_rev[(n_steps-1-s)*M + k*N + n]
 *   5. IFFT each row of size N (size N polyphase → N bins).
 *   6. Apply per-bin baseband phase correction:
 *        out[k, s] = ifft_row[s, k] * exp(i*(phase_state[k] + phase_inc[k]*s))
 *      with phase_inc[k] = -2π * k * M / N.
 *   7. Update phase_state[k] += phase_inc[k] * n_steps (mod 2π).
 *   8. Update history: copy newest N*K samples from full_rev[0..N*K).
 *
 * The output is laid out (N, n_steps) row-major so each bin's row is
 * contiguous in memory — downstream code (per-channel extract → shift
 * → decimate → uhsdr_feed) can read a bin with a single base pointer
 * and a step count.
 */

#include "cw_pfb.h"

#include <fftw3.h>

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include <complex>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace {
constexpr float kPi = (float)M_PI;
}

// ----------------------------------------------------------------------------
// Prototype-filter helpers — match scipy.signal.firwin(numtaps, cutoff,
// window=('kaiser', beta)) for cutoff = 1/(2*N).
// ----------------------------------------------------------------------------

// Modified Bessel function of the first kind, order 0 (used by Kaiser).
// Needham-Hopkins series; converges fast for the magnitudes we use.
static double bessel_i0(double x) {
    double y = x * x / 4.0;
    double term = 1.0;
    double sum = 1.0;
    for (int k = 1; k < 50; ++k) {
        term *= y / (double)(k * k);
        sum  += term;
        if (term < 1e-15 * sum) break;
    }
    return sum;
}

// Build the same prototype as scipy.signal.firwin(numtaps, cutoff,
// window=('kaiser', beta)) for a *lowpass*.
//
// scipy convention: `cutoff` is normalized frequency where 1.0 == Nyquist
// (i.e. cycles/sample on a [0..1] scale, NOT [0..0.5]). So a 6 dB cutoff
// at `c` corresponds to a continuous-frequency cutoff of c/2 cycles/sample,
// and the windowed-sinc formula is sinc(c*n) = sin(π*c*n) / (π*n).
//
// At n == 0 the sinc limit is c (its peak value before windowing).
static std::vector<double> firwin_kaiser_lowpass(int numtaps, double cutoff,
                                                 double beta)
{
    std::vector<double> h(numtaps);
    const double M = (double)(numtaps - 1);
    const double i0_beta = bessel_i0(beta);
    for (int n = 0; n < numtaps; ++n) {
        double x = (double)n - M / 2.0;
        // sinc — scipy convention (1 == Nyquist).
        double sinc;
        if (x == 0.0) {
            sinc = cutoff;
        } else {
            sinc = sin(M_PI * cutoff * x) / (M_PI * x);
        }
        // Kaiser window
        double r = (2.0 * (double)n / M) - 1.0;       // -1 .. +1
        double w = bessel_i0(beta * sqrt(1.0 - r * r)) / i0_beta;
        h[n] = sinc * w;
    }
    // scipy firwin normalizes the lowpass so DC gain == 1 (sum(h) == 1).
    double s = 0.0;
    for (double v : h) s += v;
    if (s != 0.0) {
        double inv = 1.0 / s;
        for (auto &v : h) v *= inv;
    }
    return h;
}

// ----------------------------------------------------------------------------
// cw_pfb_t
// ----------------------------------------------------------------------------

struct cw_pfb_t {
    int input_rate;
    int n_chan;          // N
    int oversample;      // OS  (output_rate = input_rate * OS / N)
    int taps_per_chan;   // K
    int M;               // N / OS  (input samples consumed per output step)
    int output_rate;
    float bin_spacing;

    // Polyphase coefficients laid out as h_polyphase[n*K + k] = h_proto[k*N + n]
    // (matches openskimmer.py: H[n, k] = h[k*N + n]).
    std::vector<float> h_polyphase;

    // Forward-time accumulator of unprocessed IQ.
    std::vector<std::complex<float>> buf;

    // Newest-first history of length N*K.
    std::vector<std::complex<float>> hist;

    // Per-bin baseband phase state and increment.
    std::vector<float> phase_state;
    std::vector<float> phase_inc;

    // Working buffer (n_steps × n_chan), used for both polyphase output and
    // in-place IFFT. Reallocated when n_steps grows.
    std::vector<std::complex<float>> work;

    // Output buffer (n_chan × n_steps), row-major. Resized as needed.
    std::vector<std::complex<float>> output;
    int output_n_steps;

    // FFTW size-n_chan inverse plan (one buffer, called once per output step).
    fftwf_plan  ifft_plan;
    fftwf_complex *ifft_buf;
};

// ----------------------------------------------------------------------------

cw_pfb_t *cw_pfb_create(int input_rate,
                        int n_chan,
                        int oversample,
                        int taps_per_chan)
{
    if (input_rate <= 0 || n_chan <= 0 ||
        oversample <= 0 || taps_per_chan <= 0) return NULL;
    if (n_chan % oversample != 0) return NULL;

    cw_pfb_t *p = new (std::nothrow) cw_pfb_t();
    if (!p) return NULL;

    p->input_rate    = input_rate;
    p->n_chan        = n_chan;
    p->oversample    = oversample;
    p->taps_per_chan = taps_per_chan;
    p->M             = n_chan / oversample;
    p->output_rate   = input_rate * oversample / n_chan;
    p->bin_spacing   = (float)input_rate / (float)n_chan;
    p->output_n_steps = 0;

    // Prototype filter — same recipe as the Python code.
    int n_taps = n_chan * taps_per_chan;
    auto h_proto = firwin_kaiser_lowpass(n_taps, 1.0 / (2.0 * (double)n_chan), 10.0);

    // Python:  h *= sqrt(N) / K   → unity passband gain at decimated rate.
    double scale = sqrt((double)n_chan) / (double)taps_per_chan;
    for (auto &v : h_proto) v *= scale;

    // Reshape (K, N) and transpose to (N, K), stored flat as [n*K + k].
    // Python: H = h.reshape(K, N).T.copy() → H[n, k] = h[k*N + n]
    p->h_polyphase.resize((size_t)n_chan * taps_per_chan);
    for (int n = 0; n < n_chan; ++n) {
        for (int k = 0; k < taps_per_chan; ++k) {
            p->h_polyphase[(size_t)n * taps_per_chan + k] =
                (float)h_proto[(size_t)k * n_chan + n];
        }
    }

    p->hist.assign((size_t)n_chan * taps_per_chan, std::complex<float>(0.0f, 0.0f));

    // Per-bin baseband phase: out[k, s] *= exp(i * (phase_state[k] + phase_inc[k]*s))
    // phase_inc[k] = -2π * k * M / N
    p->phase_state.assign(n_chan, 0.0f);
    p->phase_inc.resize(n_chan);
    for (int k = 0; k < n_chan; ++k) {
        p->phase_inc[k] = -2.0f * kPi * (float)k * (float)p->M / (float)n_chan;
    }

    // FFTW size-N inverse plan.
    p->ifft_buf = (fftwf_complex *)fftwf_malloc(sizeof(fftwf_complex) * n_chan);
    if (!p->ifft_buf) { delete p; return NULL; }
    p->ifft_plan = fftwf_plan_dft_1d(n_chan,
                                     p->ifft_buf, p->ifft_buf,
                                     FFTW_BACKWARD, FFTW_ESTIMATE);
    if (!p->ifft_plan) {
        fftwf_free(p->ifft_buf);
        delete p;
        return NULL;
    }
    return p;
}

void cw_pfb_destroy(cw_pfb_t *p)
{
    if (!p) return;
    if (p->ifft_plan) fftwf_destroy_plan(p->ifft_plan);
    if (p->ifft_buf)  fftwf_free(p->ifft_buf);
    delete p;
}

int   cw_pfb_input_rate (const cw_pfb_t *p) { return p ? p->input_rate  : 0; }
int   cw_pfb_output_rate(const cw_pfb_t *p) { return p ? p->output_rate : 0; }
int   cw_pfb_n_chan     (const cw_pfb_t *p) { return p ? p->n_chan      : 0; }
float cw_pfb_bin_spacing(const cw_pfb_t *p) { return p ? p->bin_spacing : 0.0f; }

int cw_pfb_process(cw_pfb_t *p,
                   const float *i_samples,
                   const float *q_samples,
                   int n_in)
{
    if (!p || n_in < 0) return 0;
    if (n_in > 0 && (!i_samples || !q_samples)) return 0;

    // Append new samples in forward time.
    if (n_in > 0) {
        size_t old_n = p->buf.size();
        p->buf.resize(old_n + n_in);
        for (int j = 0; j < n_in; ++j) {
            p->buf[old_n + j] = std::complex<float>(i_samples[j], q_samples[j]);
        }
    }

    const int N = p->n_chan;
    const int K = p->taps_per_chan;
    const int M = p->M;
    const int n_steps = (int)(p->buf.size() / (size_t)M);
    if (n_steps == 0) return 0;

    const int usable = n_steps * M;

    // Build full_rev: newest-first sequence of size n_steps*M + N*K.
    // [block reversed, prior history]
    std::vector<std::complex<float>> full_rev((size_t)usable + (size_t)N * K);
    // block reversed: full_rev[i] = buf[usable - 1 - i] for i in [0, usable)
    for (int i = 0; i < usable; ++i) {
        full_rev[(size_t)i] = p->buf[(size_t)(usable - 1 - i)];
    }
    // prior history (already newest-first)
    memcpy(&full_rev[(size_t)usable], p->hist.data(),
           sizeof(std::complex<float>) * (size_t)N * K);

    // Resize working / output buffers.
    p->work.assign((size_t)n_steps * (size_t)N, std::complex<float>(0.0f, 0.0f));
    p->output.assign((size_t)N * (size_t)n_steps, std::complex<float>(0.0f, 0.0f));
    p->output_n_steps = n_steps;

    // Polyphase filter: y[s, n] = Σ_k h[n*K + k] * full_rev[(n_steps-1-s)*M + k*N + n]
    // Layout chosen so the IFFT input is row-major over n: each row is one
    // step's N-vector, ready for an in-place size-N IFFT.
    for (int s = 0; s < n_steps; ++s) {
        const int sprime = (n_steps - 1 - s);   // Python "newest-first" index
        const int base = sprime * M;
        for (int n = 0; n < N; ++n) {
            std::complex<float> acc(0.0f, 0.0f);
            const float *hrow = &p->h_polyphase[(size_t)n * K];
            // sig[k] = full_rev[base + k*N + n]
            for (int k = 0; k < K; ++k) {
                acc += hrow[k] * full_rev[(size_t)(base + k * N + n)];
            }
            p->work[(size_t)s * N + n] = acc;
        }
    }

    // Per-step IFFT (size N, in place via the dispatcher's plan buffer).
    // Then phase-correct and transpose into output[n, s].
    for (int s = 0; s < n_steps; ++s) {
        // Copy this row into the FFTW plan buffer.
        for (int n = 0; n < N; ++n) {
            std::complex<float> v = p->work[(size_t)s * N + n];
            p->ifft_buf[n][0] = v.real();
            p->ifft_buf[n][1] = v.imag();
        }
        fftwf_execute(p->ifft_plan);
        // FFTW's BACKWARD is unnormalized — but Python multiplies by N, which
        // exactly cancels the 1/N scaling that Python's numpy.fft.ifft applies.
        // Net result: same as FFTW BACKWARD without any scale factor. So we
        // copy out raw and only apply the phase correction.
        for (int k = 0; k < N; ++k) {
            std::complex<float> v(p->ifft_buf[k][0], p->ifft_buf[k][1]);
            float ph = p->phase_state[k] + p->phase_inc[k] * (float)s;
            // wrap into [-π, π] to keep sin/cos arguments small for accuracy
            float c = cosf(ph);
            float si = sinf(ph);
            std::complex<float> rot(c, si);
            p->output[(size_t)k * (size_t)n_steps + (size_t)s] = v * rot;
        }
    }

    // Advance per-bin phase state by n_steps.
    for (int k = 0; k < N; ++k) {
        float ph = p->phase_state[k] + p->phase_inc[k] * (float)n_steps;
        // Wrap into (-2π, 2π) keeps the magnitude bounded forever.
        ph = fmodf(ph, 2.0f * kPi);
        p->phase_state[k] = ph;
    }

    // Update history: keep newest N*K samples (front of full_rev).
    memcpy(p->hist.data(), full_rev.data(),
           sizeof(std::complex<float>) * (size_t)N * K);

    // Drop consumed samples from forward-time accumulator.
    p->buf.erase(p->buf.begin(), p->buf.begin() + usable);

    return n_steps;
}

const float *cw_pfb_last_output(const cw_pfb_t *p, int *out_n_steps)
{
    if (!p || p->output_n_steps == 0) {
        if (out_n_steps) *out_n_steps = 0;
        return NULL;
    }
    if (out_n_steps) *out_n_steps = p->output_n_steps;
    return reinterpret_cast<const float *>(p->output.data());
}
