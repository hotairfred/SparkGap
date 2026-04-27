/*
 * pfb_scanner.c — Polyphase channelizer-backed band scanner.
 *
 * Mirrors itila_scanner.c structurally so the Python wrapper can
 * substitute one for the other without any other code change.  The
 * difference is internal:
 *
 *   itila_scanner:  for each active bin, run NCO mix + 3-stage FIR
 *                   chain at the full 192 kHz IQ rate.  Per-bin cost
 *                   dominates; ceiling around 200 bins per scanner.
 *
 *   pfb_scanner:    one polyphase channelizer (cw_pfb) produces ALL
 *                   channels at once.  Per active bin, just extract
 *                   the channel row, fine-tune mix to centre the
 *                   signal, take envelope.  Channelisation cost is
 *                   independent of active-bin count.
 *
 * Reused unchanged from itila_scanner: FFT energy scan + CFAR peak
 * detection + bin spawning/eviction, dual env100/env200 envelope
 * accumulation, and decode_ready Bayesian decoder loop.
 *
 * Phase 1 simplification: PFB delivers a single channel bandwidth.
 * We feed the same envelope to the h100 and h200 decoder paths.
 * If validation shows we're losing recall on the wide-bandwidth path,
 * a second PFB or per-bin envelope LPF can be added.
 *
 * Build:
 *   g++ -O3 -march=native -ffast-math -shared -fPIC \
 *       -o libpfb_scanner.so pfb_scanner.c cw_pfb.cpp -lfftw3f -lm
 */

#include "pfb_scanner.h"
#include "cw_pfb.h"

#include <stdio.h>
#include <math.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* ---- compile-time limits ---- */
#define PSC_MAX_BINS   1024     /* lifted vs itila_scanner — PFB removes the per-bin DSP cost */
#define PSC_ENV_CAP    15000    /* 75s at ~200 Hz — 1.25 decode windows */

/* PFB parameters — power of 2, oversample=2.
 *   n_chan = 4096:
 *     bin_spacing = 192000 / 4096 = 46.875 Hz  (was 93.75 at n_chan=2048)
 *     output_rate = 192000 *  2 / 4096 = 93.75 Hz
 *     M (input samples per output step) = 4096 / 2 = 2048
 *
 * Bumped from 2048 to 4096 (2026-04-26 ~17:00 UTC) after recognizing the
 * "trash-in-the-bin" problem: at 94 Hz channel BW, contest CW spacing
 * (~200 Hz) puts 1-2 adjacent stations partially inside each channel.
 * Their envelopes sum non-coherently → garbage → decoder frenzy at
 * WPM_MAX.  47 Hz channels keep adjacent stations out cleanly.
 * See feedback_envelope_decoder_arch.md for the full architectural note.
 */
#define PSC_PFB_NCHAN        4096
#define PSC_PFB_OVERSAMPLE   2
#define PSC_PFB_TAPS         12

/* Per-bin narrow LPF on complex IQ (after fine-tune NCO mix, before |x|).
 *
 * This is the frequency-selective stage that envelope decoders need to
 * discriminate co-channel signals.  Once the spawn-frequency station is
 * mixed to DC, this LPF passes only ±25 Hz around DC and rejects other
 * in-bin signals (which sit at ±30-47 Hz baseband after the same mix).
 * SkimSrv's per-channel Goertzel does the equivalent thing in frequency
 * domain.  Per-bin scanner does this with FIR_S2_100/200 (75 Hz LPF on
 * complex IQ — too wide for contest, but the same architectural shape).
 *
 * 9-tap Hamming-windowed FIR LPF, fc=25 Hz at fs=93.75 Hz:
 *    0 Hz:  0 dB    (DC carrier preserved)
 *   10 Hz: -0.3 dB  (CW dot fundamentals 12-25 Hz — passed cleanly)
 *   25 Hz: -6 dB    (cutoff)
 *   30 Hz: -11 dB   (in-bin competitor rejection starts)
 *   40 Hz: -31 dB   (strong rejection)
 *   46 Hz: -58 dB   (PFB channel edge — fully gone)
 * Has small negative coefs (Gibbs); applied to complex IQ which can be
 * negative anyway, so no Bayesian decoder issue (envelope is taken AFTER).
 */
#define PSC_BIN_LPF_LEN 9
static const double PSC_BIN_LPF[9] = {
     2.570683646980142e-03,
    -2.151220437994406e-02,
    -1.773977117982848e-02,
     2.719386220946657e-01,
     5.294853396362532e-01,
     2.719386220946657e-01,
    -1.773977117982848e-02,
    -2.151220437994406e-02,
     2.570683646980142e-03,
};

/* Envelope smoothing — non-negative MA on |x|.  3-tap at 93.75 Hz output. */
#define PSC_ENV_SMOOTH_LEN 3
static const double PSC_ENV_SMOOTH[3] = { 1.0/3.0, 1.0/3.0, 1.0/3.0 };

/* ---- per-bin state ---- */
typedef struct {
    double f_hz;
    int    active;

    /* PFB routing */
    int    bin_idx;          /* which PFB channel this bin reads */
    double residual_hz;      /* signal offset within the PFB bin (Hz) */
    double mix_phase;        /* running fine-tune mix phase (radians) */

    double env100[PSC_ENV_CAP];
    double env200[PSC_ENV_CAP];
    int    env_n;

    /* Per-bin narrow LPF delay line (complex IQ, after fine-tune mix). */
    double bin_lpf_i[PSC_BIN_LPF_LEN];
    double bin_lpf_q[PSC_BIN_LPF_LEN];
    int    bin_lpf_pos;
    int    bin_lpf_count;  /* primed once it reaches PSC_BIN_LPF_LEN */

    /* Envelope smoother delay line.  Per-bin (one per active bin) circular
     * buffer of last PSC_ENV_SMOOTH_LEN raw envelope samples.  See
     * process_bin_row for use. */
    double env_dl[PSC_ENV_SMOOTH_LEN];
    int    env_dl_pos;
    int    env_dl_count;  /* primed once it reaches PSC_ENV_SMOOTH_LEN */

    int    created_sample;   /* in PFB output-rate samples */
    int    last_evidence;    /* in PFB output-rate samples */
    double snr_db;

    /* Bayesian decoder handles — created lazily, one per LPF path */
    void  *h100;
    void  *h200;
} PsBin;

/* ---- scanner ---- */
struct PfbSc {
    int    sample_rate;
    double center_hz;
    int    max_bins;
    double min_snr;
    int    window_samples;
    int    energy_win;
    double grid_hz;
    double band_min_hz;
    double band_max_hz;

    cw_pfb_t *pfb;
    int    pfb_n_chan;
    int    pfb_output_rate;
    double pfb_bin_spacing;

    /* Energy scan accumulator (raw IQ at sample_rate) */
    double *scan_i;
    double *scan_q;
    int     scan_n;

    /* Output-rate sample counter for eviction timing */
    int    total_samples;

    int    n_bins;
    PsBin  bins[PSC_MAX_BINS];

    pthread_mutex_t lock;

    /* Workspace for transferring PFB output samples (per active bin) */
    float *pfb_in_i;
    float *pfb_in_q;
    int    pfb_in_cap;

    /* Diagnostic counters */
    uint64_t env_drops;
    uint64_t bins_at_max;

    /* Bayesian decoder hookup — same shape as itila_sc_set_decoder */
    void *(*dec_create)(int sample_rate, double lpf_hz);
    const char *(*dec_feed)(void *h, const double *env, int n,
                             double freq_khz, double ev_thresh);
    void (*dec_free)(void *h);
    double (*dec_get_wpm)(void *h);
    double ev_thresh;
};

static void bin_free_decoders(PfbSc *sc, PsBin *b);

/* ---------------------------------------------------------------------------
 * FFT (Cooley-Tukey in-place, power-of-2, forward) — copied verbatim from
 * itila_scanner.c.  Energy scan is independent of channelization choice.
 * ------------------------------------------------------------------------- */
static void fft_forward(double *re, double *im, int n)
{
    int j = 0;
    for (int i = 1; i < n; i++) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            double t;
            t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (int len = 2; len <= n; len <<= 1) {
        double ang = -2.0 * M_PI / len;
        double wre = cos(ang), wim = sin(ang);
        for (int i = 0; i < n; i += len) {
            double cr = 1.0, ci = 0.0;
            for (int k = 0; k < len / 2; k++) {
                double ur = re[i+k],       ui = im[i+k];
                double vr = re[i+k+len/2], vi = im[i+k+len/2];
                double tr = vr*cr - vi*ci, ti = vr*ci + vi*cr;
                re[i+k]        = ur + tr; im[i+k]        = ui + ti;
                re[i+k+len/2]  = ur - tr; im[i+k+len/2]  = ui - ti;
                double nc = cr*wre - ci*wim;
                ci = cr*wim + ci*wre;
                cr = nc;
            }
        }
    }
}

/* qsort comparators */
static int cmp_dbl_asc(const void *a, const void *b)
{
    double da = *(const double *)a, db = *(const double *)b;
    return (da > db) - (da < db);
}

typedef struct { double power; double f_hz; double snr; } PsPeak;

static int cmp_peak_desc(const void *a, const void *b)
{
    const PsPeak *pa = (const PsPeak *)a;
    const PsPeak *pb = (const PsPeak *)b;
    return (pb->power > pa->power) - (pb->power < pa->power);
}

/* ---------------------------------------------------------------------------
 * Map an absolute frequency to (PFB bin index, residual Hz).
 *   bin_centre = bin_idx * bin_spacing relative to center_hz.
 *   bin_idx in [0, N) following PFB convention; N/2..N-1 represent
 *   negative offsets.
 * ------------------------------------------------------------------------- */
static void bin_route(const PfbSc *sc, double f_hz,
                       int *bin_idx_out, double *residual_out)
{
    double offset = f_hz - sc->center_hz;
    int N = sc->pfb_n_chan;
    double bsp = sc->pfb_bin_spacing;
    int idx = (int)lround(offset / bsp);
    /* Wrap into [0, N) following PFB convention. */
    idx = ((idx % N) + N) % N;
    double bin_centre = (double)idx * bsp;
    if (bin_centre > sc->sample_rate * 0.5) bin_centre -= sc->sample_rate;
    *bin_idx_out  = idx;
    *residual_out = offset - bin_centre;
}

/* ---------------------------------------------------------------------------
 * Energy scan + CFAR peak detection + bin spawn — same logic as
 * itila_scanner.c::run_scan, but spawns PsBin records with the PFB-routing
 * fields populated.
 * ------------------------------------------------------------------------- */
static void run_scan(PfbSc *sc, const double *seg_i, const double *seg_q)
{
    int N = sc->energy_win;
    double *re = (double *)malloc(N * sizeof(double));
    double *im = (double *)malloc(N * sizeof(double));
    if (!re || !im) { free(re); free(im); return; }

    for (int k = 0; k < N; k++) {
        double w = 0.42 - 0.5*cos(2.0*M_PI*k/(N-1)) + 0.08*cos(4.0*M_PI*k/(N-1));
        re[k] = seg_i[k] * w;
        im[k] = seg_q[k] * w;
    }
    fft_forward(re, im, N);

    double *psd = (double *)malloc(N * sizeof(double));
    if (!psd) { free(re); free(im); return; }
    for (int k = 0; k < N; k++)
        psd[k] = 10.0 * log10(re[k]*re[k] + im[k]*im[k] + 1e-20);
    free(re); free(im);

    double *sorted = (double *)malloc(N * sizeof(double));
    if (!sorted) { free(psd); return; }
    memcpy(sorted, psd, N * sizeof(double));
    qsort(sorted, N, sizeof(double), cmp_dbl_asc);
    free(sorted);

    double bin_hz = (double)sc->sample_rate / N;

    PsPeak *peaks = (PsPeak *)malloc(N * sizeof(PsPeak));
    if (!peaks) { free(psd); return; }
    int np = 0;
    int guard = 3;
    int window = 20;
    for (int k = 1; k < N - 1; k++) {
        if (psd[k] <= psd[k-1] || psd[k] <= psd[k+1]) continue;
        double local[64]; int nl = 0;
        for (int j = k - window; j <= k + window && nl < 64; j++) {
            int jj = ((j % N) + N) % N;
            if (abs(j - k) <= guard) continue;
            local[nl++] = psd[jj];
        }
        if (nl < 5) continue;
        for (int a = 1; a < nl; a++) {
            double tmp = local[a]; int b = a-1;
            while (b >= 0 && local[b] > tmp) { local[b+1] = local[b]; b--; }
            local[b+1] = tmp;
        }
        double local_noise = local[nl/2];
        if (psd[k] <= local_noise + sc->min_snr) continue;
        double delta = 0.5 * (psd[k-1] - psd[k+1]) /
                       (psd[k-1] - 2.0*psd[k] + psd[k+1]);
        double exact = (double)k + delta;
        if (exact >= N/2) exact -= N;
        double f_hz_interp = exact * bin_hz;
        double f_abs  = sc->center_hz + f_hz_interp;
        if (f_abs < sc->band_min_hz || f_abs > sc->band_max_hz) continue;
        double f_grid = round(f_abs / sc->grid_hz) * sc->grid_hz;
        peaks[np].power = psd[k];
        peaks[np].f_hz  = f_grid;
        peaks[np].snr   = psd[k] - local_noise;
        np++;
    }
    free(psd);

    qsort(peaks, np, sizeof(PsPeak), cmp_peak_desc);

    /* Cluster within 150 Hz, keep strongest, spawn new bins. */
    /* Cluster threshold: must be NARROWER than PFB bin spacing or signals
     * land in adjacent PFB channels we never spawn into.  Per-bin scanner
     * uses 150 Hz but its NCO mix can place a bin anywhere; PFB is locked
     * to the 94 Hz channel grid.  Live RF (2026-04-26 14:55 UTC, NA QSO
     * Party): W4H at 14036.82 +49 dB SDC was completely missed because
     * scan spawned bin at 14036.7 → mapped to PFB bin 1480 (centered 14036.75)
     * → the actual signal at 14036.82 was in bin 1481 (centered 14036.844),
     * but that bin's spawn was blocked by 150 Hz cluster window. */
    double cluster_hz = (double)sc->pfb_bin_spacing * 0.95;
    for (int i = 0; i < np; i++) {
        double f_hz = peaks[i].f_hz;

        int found = 0;
        for (int b = 0; b < PSC_MAX_BINS; b++) {
            if (sc->bins[b].active && fabs(sc->bins[b].f_hz - f_hz) < cluster_hz) {
                sc->bins[b].snr_db = peaks[i].snr;
                found = 1; break;
            }
        }
        if (found) continue;

        if (sc->n_bins >= sc->max_bins) {
            /* Eviction policy: same as itila_scanner.  300s grace then evict
             * by total age.  total_samples is in PFB output-rate units; a
             * bin "60s old" means sc->total_samples - created_sample >= 60 * pfb_output_rate.
             */
            int evicted = -1;
            int oldest_age = 0;
            int rate = sc->pfb_output_rate;
            for (int b = 0; b < PSC_MAX_BINS; b++) {
                if (!sc->bins[b].active) continue;
                int age = sc->total_samples - sc->bins[b].created_sample;
                int since_ev = sc->total_samples - sc->bins[b].last_evidence;
                if (sc->bins[b].last_evidence == 0 && age > 300 * rate) {
                    if (age > oldest_age) { oldest_age = age; evicted = b; }
                } else if (sc->bins[b].last_evidence > 0 && since_ev > 300 * rate) {
                    if (age > oldest_age) { oldest_age = age; evicted = b; }
                }
            }
            if (evicted >= 0) {
                bin_free_decoders(sc, &sc->bins[evicted]);
                sc->bins[evicted].active = 0;
                sc->n_bins--;
            } else {
                continue;
            }
        }

        int slot = -1;
        for (int b = 0; b < PSC_MAX_BINS; b++) {
            if (!sc->bins[b].active) { slot = b; break; }
        }
        if (slot < 0) continue;

        PsBin *bin = &sc->bins[slot];
        memset(bin, 0, sizeof(PsBin));
        bin->f_hz           = f_hz;
        bin->created_sample = sc->total_samples;
        bin->active         = 1;
        bin->snr_db         = peaks[i].snr;
        bin_route(sc, f_hz, &bin->bin_idx, &bin->residual_hz);
        sc->n_bins++;
    }
    free(peaks);
}

/* ---------------------------------------------------------------------------
 * Per-active-bin envelope extraction from PFB output.
 *   - bin_row points to (n_steps,) consecutive complex samples for this bin
 *   - fine-tune mix by exp(-j 2π residual_hz t) so signal sits at DC
 *     (PFB has already removed the bin-centre carrier; this just snaps the
 *     residual offset out)
 *   - envelope = magnitude
 *   - push to env100 + env200 (Phase 1: same data both paths)
 * ------------------------------------------------------------------------- */
static void process_bin_row(PfbSc *sc, PsBin *b,
                             const float *bin_row, int n_steps)
{
    /* Shift the signal at +residual_hz baseband DOWN to DC.
     * Code does y = (xi + j*xq) * (cp + j*sp) = z * exp(+j*phase).
     * For freq shift -residual: need phase = -2π*residual*t,
     *   i.e. dphi = -2π*residual/fs.  (This sign was right originally.) */
    double dphi = -2.0 * M_PI * b->residual_hz / (double)sc->pfb_output_rate;
    double cs   = cos(dphi);
    double sn   = sin(dphi);
    double cp   = cos(b->mix_phase);
    double sp   = sin(b->mix_phase);

    for (int s = 0; s < n_steps; s++) {
        double xi = bin_row[2 * s + 0];
        double xq = bin_row[2 * s + 1];

        /* Stage 1: fine-tune mix — shift spawn-frequency to DC.
         * mix by exp(-j*phase) (per Eq 2.25 SDR4Engineers, see comment above) */
        double mi = xi * cp - xq * sp;
        double mq = xi * sp + xq * cp;
        /* advance phase */
        double cn = cp * cs - sp * sn;
        double sn2 = sp * cs + cp * sn;
        cp = cn; sp = sn2;

        /* Stage 2: per-bin narrow LPF on complex IQ for in-bin frequency
         * selectivity.  Spawn-frequency carrier sits at DC after the mix
         * above; this LPF passes ±25 Hz around DC and rejects co-channel
         * competitors that landed at ±30+ Hz baseband.  Without this stage
         * the envelope detector below sees |signal_A + signal_B| for
         * any pair of in-bin signals (the trash-in-the-bin problem). */
        b->bin_lpf_i[b->bin_lpf_pos] = mi;
        b->bin_lpf_q[b->bin_lpf_pos] = mq;
        b->bin_lpf_pos = (b->bin_lpf_pos + 1) % PSC_BIN_LPF_LEN;
        if (b->bin_lpf_count < PSC_BIN_LPF_LEN) {
            b->bin_lpf_count++;
            continue;  /* prime delay line, skip output */
        }
        double fi = 0.0, fq = 0.0;
        int p = b->bin_lpf_pos;  /* oldest sample */
        for (int k = 0; k < PSC_BIN_LPF_LEN; k++) {
            int idx = (p + k) % PSC_BIN_LPF_LEN;
            fi += PSC_BIN_LPF[k] * b->bin_lpf_i[idx];
            fq += PSC_BIN_LPF[k] * b->bin_lpf_q[idx];
        }

        /* Stage 3: envelope detect on filtered complex IQ */
        double env_raw = sqrt(fi * fi + fq * fq);

        /* Stage 4: envelope smoother (non-negative MA, kills residual noise) */
        b->env_dl[b->env_dl_pos] = env_raw;
        b->env_dl_pos = (b->env_dl_pos + 1) % PSC_ENV_SMOOTH_LEN;
        if (b->env_dl_count < PSC_ENV_SMOOTH_LEN) {
            b->env_dl_count++;
            continue;
        }
        double env = 0.0;
        int q = b->env_dl_pos;
        for (int k = 0; k < PSC_ENV_SMOOTH_LEN; k++) {
            int idx = (q + k) % PSC_ENV_SMOOTH_LEN;
            env += PSC_ENV_SMOOTH[k] * b->env_dl[idx];
        }

        if (b->env_n < PSC_ENV_CAP) {
            b->env100[b->env_n] = env;
            b->env200[b->env_n] = env;
            b->env_n++;
        } else {
            sc->env_drops++;
        }
    }

    /* renormalise + commit phase */
    double norm = 1.0 / sqrt(cp*cp + sp*sp);
    cp *= norm; sp *= norm;
    b->mix_phase = atan2(sp, cp);
}

/* ---- public API ---- */

PfbSc *pfb_sc_create(int sample_rate, double center_hz,
                     int max_bins, double min_snr,
                     int window_samples, int energy_win,
                     double grid_hz,
                     double band_min_hz, double band_max_hz,
                     const double *sos100_flat, int n_sos,
                     const double *sos200_flat)
{
    (void)sos100_flat; (void)n_sos; (void)sos200_flat;
    if (max_bins > PSC_MAX_BINS) max_bins = PSC_MAX_BINS;
    if (energy_win < 4) energy_win = 4096;

    PfbSc *sc = (PfbSc *)calloc(1, sizeof(PfbSc));
    if (!sc) return NULL;

    sc->sample_rate    = sample_rate;
    sc->center_hz      = center_hz;
    sc->max_bins       = max_bins;
    sc->min_snr        = min_snr;
    sc->window_samples = window_samples;
    sc->energy_win     = energy_win;
    sc->grid_hz        = grid_hz;
    sc->band_min_hz    = band_min_hz;
    sc->band_max_hz    = band_max_hz;

    sc->pfb = cw_pfb_create(sample_rate, PSC_PFB_NCHAN,
                            PSC_PFB_OVERSAMPLE, PSC_PFB_TAPS);
    if (!sc->pfb) { free(sc); return NULL; }
    sc->pfb_n_chan      = cw_pfb_n_chan(sc->pfb);
    sc->pfb_output_rate = cw_pfb_output_rate(sc->pfb);
    sc->pfb_bin_spacing = (double)cw_pfb_bin_spacing(sc->pfb);

    sc->scan_i = (double *)calloc(energy_win, sizeof(double));
    sc->scan_q = (double *)calloc(energy_win, sizeof(double));
    if (!sc->scan_i || !sc->scan_q) { pfb_sc_free(sc); return NULL; }

    /* Workspace for PFB ingest — sized lazily on first feed. */
    sc->pfb_in_cap = 0;
    sc->pfb_in_i   = NULL;
    sc->pfb_in_q   = NULL;

    pthread_mutex_init(&sc->lock, NULL);
    return sc;
}

void pfb_sc_free(PfbSc *sc)
{
    if (!sc) return;
    for (int i = 0; i < PSC_MAX_BINS; i++)
        if (sc->bins[i].active) bin_free_decoders(sc, &sc->bins[i]);
    if (sc->pfb) cw_pfb_destroy(sc->pfb);
    pthread_mutex_destroy(&sc->lock);
    free(sc->scan_i);
    free(sc->scan_q);
    free(sc->pfb_in_i);
    free(sc->pfb_in_q);
    free(sc);
}

void pfb_sc_mark_evidence(PfbSc *sc, double f_hz)
{
    pthread_mutex_lock(&sc->lock);
    for (int i = 0; i < PSC_MAX_BINS; i++) {
        if (sc->bins[i].active && fabs(sc->bins[i].f_hz - f_hz) < 1.0) {
            sc->bins[i].last_evidence = sc->total_samples;
            break;
        }
    }
    pthread_mutex_unlock(&sc->lock);
}

void pfb_sc_feed_iq(PfbSc *sc,
                    const double *i_arr, const double *q_arr, int n)
{
    pthread_mutex_lock(&sc->lock);

    /* --- FFT energy scan: fill rolling scan buffer --- */
    int i_pos = 0;
    while (i_pos < n) {
        int room  = sc->energy_win - sc->scan_n;
        int avail = n - i_pos;
        int copy  = avail < room ? avail : room;
        memcpy(sc->scan_i + sc->scan_n, i_arr + i_pos, copy * sizeof(double));
        memcpy(sc->scan_q + sc->scan_n, q_arr + i_pos, copy * sizeof(double));
        sc->scan_n += copy;
        i_pos      += copy;
        if (sc->scan_n >= sc->energy_win) {
            run_scan(sc, sc->scan_i, sc->scan_q);
            sc->scan_n = 0;
        }
    }

    /* --- Channelize via PFB.  Convert input to float (cw_pfb expects floats). */
    if (sc->pfb_in_cap < n) {
        free(sc->pfb_in_i); free(sc->pfb_in_q);
        sc->pfb_in_i = (float *)malloc(n * sizeof(float));
        sc->pfb_in_q = (float *)malloc(n * sizeof(float));
        sc->pfb_in_cap = (sc->pfb_in_i && sc->pfb_in_q) ? n : 0;
        if (!sc->pfb_in_cap) { pthread_mutex_unlock(&sc->lock); return; }
    }
    for (int i = 0; i < n; i++) {
        sc->pfb_in_i[i] = (float)i_arr[i];
        sc->pfb_in_q[i] = (float)q_arr[i];
    }

    int n_steps = cw_pfb_process(sc->pfb, sc->pfb_in_i, sc->pfb_in_q, n);
    if (n_steps <= 0) { pthread_mutex_unlock(&sc->lock); return; }

    int out_n_steps = 0;
    const float *out = cw_pfb_last_output(sc->pfb, &out_n_steps);
    if (!out || out_n_steps <= 0) { pthread_mutex_unlock(&sc->lock); return; }

    /* Per-bin envelope extraction.  PFB output layout: (n_chan, n_steps)
     * row-major complex64 (interleaved real/imag floats).  Each bin's row
     * is contiguous; stride between bins is 2*out_n_steps floats. */
    int nch_stride = 2 * out_n_steps;
    for (int bi = 0; bi < PSC_MAX_BINS; bi++) {
        PsBin *b = &sc->bins[bi];
        if (!b->active) continue;
        const float *bin_row = out + (size_t)b->bin_idx * (size_t)nch_stride;
        process_bin_row(sc, b, bin_row, out_n_steps);
    }

    sc->total_samples += out_n_steps;
    pthread_mutex_unlock(&sc->lock);
}

int pfb_sc_ready_bins(PfbSc *sc, double *f_hz_out, int max_out)
{
    int count = 0;
    for (int i = 0; i < PSC_MAX_BINS && count < max_out; i++) {
        if (sc->bins[i].active && sc->bins[i].env_n >= sc->window_samples)
            f_hz_out[count++] = sc->bins[i].f_hz;
    }
    return count;
}

int pfb_sc_drain_env(PfbSc *sc, double f_hz,
                     double *env100_out, double *env200_out, int max_n)
{
    for (int i = 0; i < PSC_MAX_BINS; i++) {
        PsBin *b = &sc->bins[i];
        if (!b->active || fabs(b->f_hz - f_hz) >= 1.0) continue;
        int n = b->env_n < max_n ? b->env_n : max_n;
        memcpy(env100_out, b->env100, n * sizeof(double));
        memcpy(env200_out, b->env200, n * sizeof(double));
        int rem = b->env_n - n;
        memmove(b->env100, b->env100 + n, rem * sizeof(double));
        memmove(b->env200, b->env200 + n, rem * sizeof(double));
        b->env_n = rem;
        return n;
    }
    return 0;
}

int pfb_sc_peek_env(PfbSc *sc, double f_hz,
                    double *env100_out, double *env200_out, int max_n)
{
    for (int i = 0; i < PSC_MAX_BINS; i++) {
        PsBin *b = &sc->bins[i];
        if (!b->active || fabs(b->f_hz - f_hz) >= 1.0) continue;
        int n = b->env_n < max_n ? b->env_n : max_n;
        memcpy(env100_out, b->env100, n * sizeof(double));
        memcpy(env200_out, b->env200, n * sizeof(double));
        return n;
    }
    return 0;
}

int pfb_sc_bin_count(PfbSc *sc)
{
    if (sc->n_bins > (int)sc->bins_at_max) sc->bins_at_max = sc->n_bins;
    return sc->n_bins;
}

unsigned long long pfb_sc_env_drops(PfbSc *sc) { return (unsigned long long)sc->env_drops; }
int                pfb_sc_bins_peak(PfbSc *sc) { return (int)sc->bins_at_max; }

int pfb_sc_env_n(PfbSc *sc, double f_hz)
{
    for (int i = 0; i < PSC_MAX_BINS; i++) {
        if (sc->bins[i].active && fabs(sc->bins[i].f_hz - f_hz) < 1.0)
            return sc->bins[i].env_n;
    }
    return 0;
}

int pfb_sc_list_bins(PfbSc *sc, double *f_hz_out, int max_out)
{
    int count = 0;
    for (int i = 0; i < PSC_MAX_BINS && count < max_out; i++) {
        if (sc->bins[i].active)
            f_hz_out[count++] = sc->bins[i].f_hz;
    }
    return count;
}

double pfb_sc_get_snr(PfbSc *sc, double f_hz)
{
    for (int i = 0; i < PSC_MAX_BINS; i++) {
        if (sc->bins[i].active && fabs(sc->bins[i].f_hz - f_hz) < 1.0)
            return sc->bins[i].snr_db;
    }
    return 0.0;
}

int    pfb_sc_n_chan      (PfbSc *sc) { return sc ? sc->pfb_n_chan      : 0; }
double pfb_sc_bin_spacing (PfbSc *sc) { return sc ? sc->pfb_bin_spacing : 0.0; }
int    pfb_sc_output_rate (PfbSc *sc) { return sc ? sc->pfb_output_rate : 0; }

/* ---- integrated decode: ready check + drain + dec_feed in C ---- */

void pfb_sc_set_decoder(PfbSc *sc,
                         void *(*create_fn)(int, double),
                         const char *(*feed_fn)(void*, const double*, int, double, double),
                         void (*free_fn)(void*),
                         double (*get_wpm_fn)(void*),
                         double ev_thresh)
{
    sc->dec_create  = create_fn;
    sc->dec_feed    = feed_fn;
    sc->dec_free    = free_fn;
    sc->dec_get_wpm = get_wpm_fn;
    sc->ev_thresh   = ev_thresh;
}

int pfb_sc_decode_ready(PfbSc *sc, int window_samples,
                         PfbScDecodeResult *results, int max_results)
{
    if (!sc->dec_create || !sc->dec_feed) return 0;

    pthread_mutex_lock(&sc->lock);
    int n_results = 0;
    int rate = sc->pfb_output_rate;

    for (int bi = 0; bi < PSC_MAX_BINS && n_results < max_results; bi++) {
        PsBin *b = &sc->bins[bi];
        if (!b->active || b->env_n < window_samples) continue;

        if (!b->h100) b->h100 = sc->dec_create(rate, 100.0);
        if (!b->h200) b->h200 = sc->dec_create(rate, 200.0);

        double f_khz = b->f_hz / 1000.0;

        while (b->env_n >= window_samples) {
            void *handles[2] = { b->h100, b->h200 };
            double *envs[2]  = { b->env100, b->env200 };

            for (int p = 0; p < 2; p++) {
                if (!handles[p]) continue;
                const char *raw = sc->dec_feed(handles[p], envs[p],
                                               window_samples, f_khz,
                                               sc->ev_thresh);
                if (raw && raw[0] && n_results < max_results) {
                    PfbScDecodeResult *r = &results[n_results];
                    r->f_hz = b->f_hz;
                    r->snr  = b->snr_db;
                    r->wpm  = (int)(sc->dec_get_wpm(handles[p]) + 0.5);
                    int len = (int)strlen(raw);
                    if (len > 255) len = 255;
                    memcpy(r->text, raw, len);
                    r->text[len] = '\0';
                    while (len > 0 && (r->text[len-1] == ' ' || r->text[len-1] == '\n'))
                        r->text[--len] = '\0';
                    if (len > 0) n_results++;
                }
            }

            int rem = b->env_n - window_samples;
            memmove(b->env100, b->env100 + window_samples, rem * sizeof(double));
            memmove(b->env200, b->env200 + window_samples, rem * sizeof(double));
            b->env_n = rem;
        }
    }
    pthread_mutex_unlock(&sc->lock);
    return n_results;
}

static void bin_free_decoders(PfbSc *sc, PsBin *b)
{
    if (sc->dec_free) {
        if (b->h100) { sc->dec_free(b->h100); b->h100 = NULL; }
        if (b->h200) { sc->dec_free(b->h200); b->h200 = NULL; }
    }
}
