/*
 * itila_scanner.c — full _ItilaScanner in C
 *
 * Implements the complete per-feed pipeline:
 *   IQ residual → FFT energy scan → bin spawn →
 *   per-bin mix+FIR-decimate+envelope+FIR-decimate → 200 Hz accumulator
 *   All-FIR linear-phase chain. Deterministic: same envelope regardless
 *   of chunk boundaries. Replaces Butterworth IIR (nonlinear phase,
 *   chunk-dependent state was the root cause of file-vs-live decode gap).
 *
 * Python calls only: itila_sc_feed_iq(), itila_sc_ready_bins(),
 * itila_sc_drain_env().  itila_feed() (the decoder) stays in Python.
 *
 * Compile:
 *   gcc -O3 -march=native -ffast-math -shared -fPIC \
 *       -o libitila_scanner.so itila_scanner.c -lm
 */

#include "itila_scanner.h"
#include <stdio.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

/* ---- compile-time limits ---- */
#define SC_MAX_BINS   512
#define SC_DEC1       16      /* 192 kHz → 12 kHz  (FIR stage 1) */
#define SC_DEC2       6       /* 12 kHz → 2 kHz    (FIR stage 2) */
#define SC_DEC3       10      /* 2 kHz → 200 Hz    (FIR stage 3) */
#define SC_ENV_CAP    15000   /* 75s at 200 Hz — 1.25 decode windows */

#include "itila_fir_coeffs.h"

/* ---- per-bin state ---- */
typedef struct {
    double f_hz;
    int    active;
    double c_phase, s_phase;           /* oscillator state */

    /* FIR stage 1: 192k→12k (32 taps, complex I/Q) */
    double dl1_i[FIR_STAGE1_LEN];
    double dl1_q[FIR_STAGE1_LEN];
    int    dl1_count;                  /* samples fed since last output */

    /* FIR stage 2: 12k→2k (96 taps, complex I/Q, two paths) */
    double dl2_100i[FIR_S2_100_LEN];
    double dl2_100q[FIR_S2_100_LEN];
    double dl2_200i[FIR_S2_200_LEN];
    double dl2_200q[FIR_S2_200_LEN];
    int    dl2_count;

    /* FIR stage 3: 2k→200Hz (60 taps, real envelope, two paths) */
    double dl3_100[FIR_STAGE3_LEN];
    double dl3_200[FIR_STAGE3_LEN];
    int    dl3_count;

    double env100[SC_ENV_CAP];
    double env200[SC_ENV_CAP];
    int    env_n;
    int    created_sample;
    int    last_evidence;
    double snr_db;
} ScBin;

/* ---- scanner ---- */
struct ItilaSc {
    int    sample_rate;
    double center_hz;
    int    max_bins;
    double min_snr;
    int    window_samples;
    int    energy_win;
    double grid_hz;
    double band_min_hz;
    double band_max_hz;
    /* IIR SOS removed — replaced by per-bin FIR delay lines */

    double iq_res_i[SC_DEC1];
    double iq_res_q[SC_DEC1];
    int    iq_res_n;
    int    total_samples;      /* running sample count for eviction timing */

    double *scan_i;
    double *scan_q;
    int     scan_n;

    int    n_bins;
    ScBin  bins[SC_MAX_BINS];
};

/* ---- FFT (Cooley-Tukey in-place, power-of-2, forward) ---- */
static void fft_forward(double *re, double *im, int n)
{
    /* bit-reversal */
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
    /* butterfly */
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

/* ---- qsort comparators (file scope, not nested) ---- */
static int cmp_dbl_asc(const void *a, const void *b)
{
    double da = *(const double*)a, db = *(const double*)b;
    return da < db ? -1 : da > db ? 1 : 0;
}

typedef struct { double power; double f_hz; double snr; } ScPeak;
static int cmp_peak_desc(const void *a, const void *b)
{
    double da = ((const ScPeak*)a)->power, db = ((const ScPeak*)b)->power;
    return da > db ? -1 : da < db ? 1 : 0;
}

/* ---- IIR: Direct Form II transposed ---- */
static void sosfilt(const double sos[][6], int n_sos,
                    double *x, int n, double z[][2])
{
    for (int s = 0; s < n_sos; s++) {
        double b0=sos[s][0], b1=sos[s][1], b2=sos[s][2];
        double a1=sos[s][4], a2=sos[s][5];
        double z0=z[s][0], z1=z[s][1];
        for (int i = 0; i < n; i++) {
            double xi = x[i];
            double yi = b0*xi + z0;
            z0 = b1*xi - a1*yi + z1;
            z1 = b2*xi - a2*yi;
            x[i] = yi;
        }
        z[s][0] = z0;
        z[s][1] = z1;
    }
}

/* ---- energy scan ---- */
static void run_scan(ItilaSc *sc, const double *seg_i, const double *seg_q)
{
    int N = sc->energy_win;
    double *re = (double *)malloc(N * sizeof(double));
    double *im = (double *)malloc(N * sizeof(double));
    if (!re || !im) { free(re); free(im); return; }

    /* Blackman window + load */
    for (int k = 0; k < N; k++) {
        double w = 0.42 - 0.5*cos(2.0*M_PI*k/(N-1)) + 0.08*cos(4.0*M_PI*k/(N-1));
        re[k] = seg_i[k] * w;
        im[k] = seg_q[k] * w;
    }
    fft_forward(re, im, N);

    /* PSD in dB */
    double *psd = (double *)malloc(N * sizeof(double));
    if (!psd) { free(re); free(im); return; }
    for (int k = 0; k < N; k++)
        psd[k] = 10.0 * log10(re[k]*re[k] + im[k]*im[k] + 1e-20);
    free(re); free(im);

    /* Median noise via sort */
    double *sorted = (double *)malloc(N * sizeof(double));
    if (!sorted) { free(psd); return; }
    memcpy(sorted, psd, N * sizeof(double));
    qsort(sorted, N, sizeof(double), cmp_dbl_asc);
    double noise = (N & 1) ? sorted[N/2] : 0.5*(sorted[N/2-1]+sorted[N/2]);
    free(sorted);

    double bin_hz  = (double)sc->sample_rate / N;

    /* Collect LOCAL MAXIMA using CFAR detection — local noise estimate per bin.
     * For each candidate peak, compute median of nearby bins (excluding the peak
     * and its immediate neighbors). Self-calibrates to local QRM conditions. */
    ScPeak *peaks = (ScPeak *)malloc(N * sizeof(ScPeak));
    if (!peaks) { free(psd); return; }
    int np = 0;
    int guard = 3;   /* bins to exclude around candidate */
    int window = 20; /* bins each side for local noise estimate */
    for (int k = 1; k < N - 1; k++) {
        if (psd[k] <= psd[k-1] || psd[k] <= psd[k+1]) continue;  /* not a peak */
        /* Local noise: median of bins within ±window, excluding ±guard */
        double local[64]; int nl = 0;
        for (int j = k - window; j <= k + window && nl < 64; j++) {
            int jj = ((j % N) + N) % N;  /* wrap around */
            if (abs(j - k) <= guard) continue;
            local[nl++] = psd[jj];
        }
        if (nl < 5) continue;
        /* Sort for median */
        for (int a = 1; a < nl; a++) {
            double tmp = local[a]; int b = a-1;
            while (b >= 0 && local[b] > tmp) { local[b+1] = local[b]; b--; }
            local[b+1] = tmp;
        }
        double local_noise = local[nl/2];
        if (psd[k] <= local_noise + sc->min_snr) continue;
        /* Parabolic interpolation for sub-bin accuracy */
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

    qsort(peaks, np, sizeof(ScPeak), cmp_peak_desc);

    /* Cluster (150 Hz), keep strongest per cluster, spawn new bins.
     * Tightened from 300 Hz to match 50 Hz grid + FIR selectivity. */
    double cluster_hz = 150.0;
    for (int i = 0; i < np; i++) {
        double f_hz = peaks[i].f_hz;

        /* Skip if within 150 Hz of any active bin — but update its SNR */
        int found = 0;
        int blocking_bin = -1;
        for (int b = 0; b < SC_MAX_BINS; b++) {
            if (sc->bins[b].active && fabs(sc->bins[b].f_hz - f_hz) < cluster_hz) {
                sc->bins[b].snr_db = peaks[i].snr;
                found = 1; blocking_bin = b; break;
            }
        }
        /* Debug: log why peaks near 7047-7048 kHz are blocked */
        if (f_hz > 7047000 && f_hz < 7049000) {
            if (found)
                fprintf(stderr, "PEAK %.1f Hz (%.1f dB) BLOCKED by bin %.1f Hz (dist=%.0f)\n",
                        f_hz, peaks[i].snr,
                        sc->bins[blocking_bin].f_hz,
                        fabs(sc->bins[blocking_bin].f_hz - f_hz));
            else
                fprintf(stderr, "PEAK %.1f Hz (%.1f dB) NEW\n", f_hz, peaks[i].snr);
        }
        if (found) continue;

        /* Evict stale bins if at capacity: bins that never produced evidence
         * after 120s, or produced evidence but went silent for 300s */
        if (sc->n_bins >= sc->max_bins) {
            int evicted = -1;
            int oldest_age = 0;
            for (int b = 0; b < SC_MAX_BINS; b++) {
                if (!sc->bins[b].active) continue;
                int age = sc->total_samples - sc->bins[b].created_sample;
                int since_ev = sc->total_samples - sc->bins[b].last_evidence;
                /* Never produced evidence and >120s old
                 * total_samples counts in 12kHz units (n/SC_DEC1) */
                int rate_12k = sc->sample_rate / SC_DEC1;
                if (sc->bins[b].last_evidence == 0 &&
                    age > 300 * rate_12k) {
                    if (age > oldest_age) { oldest_age = age; evicted = b; }
                }
                /* Had evidence but silent for >300s */
                else if (sc->bins[b].last_evidence > 0 &&
                         since_ev > 300 * rate_12k) {
                    if (age > oldest_age) { oldest_age = age; evicted = b; }
                }
            }
            if (evicted >= 0) {
                sc->bins[evicted].active = 0;
                sc->n_bins--;
            } else {
                continue;  /* all bins active and producing — can't evict */
            }
        }

        int slot = -1;
        for (int b = 0; b < SC_MAX_BINS; b++) {
            if (!sc->bins[b].active) { slot = b; break; }
        }
        if (slot < 0) continue;

        ScBin *bin = &sc->bins[slot];
        memset(bin, 0, sizeof(ScBin));
        bin->f_hz           = f_hz;
        bin->created_sample = sc->total_samples;
        bin->active  = 1;
        bin->c_phase = 1.0;
        bin->s_phase = 0.0;
        bin->snr_db  = peaks[i].snr;
        sc->n_bins++;
    }
    free(peaks);
}

/* ---- FIR convolution helper: dot product of delay line with filter ---- */
static double fir_dot(const double *dl, int pos, const double *h, int len)
{
    double acc = 0.0;
    for (int k = 0; k < len; k++) {
        int idx = (pos - k + len) % len;
        acc += dl[idx] * h[k];
    }
    return acc;
}

/* ---- per-bin DSP: all-FIR linear-phase chain ---- */
static void process_bins(ItilaSc *sc, const double *i_full, const double *q_full, int n)
{
    for (int bi = 0; bi < SC_MAX_BINS; bi++) {
        ScBin *b = &sc->bins[bi];
        if (!b->active) continue;

        /* Oscillator for mixing to baseband */
        double step   = -2.0 * M_PI * (b->f_hz - sc->center_hz) / sc->sample_rate;
        double c_step = cos(step);
        double s_step = sin(step);
        double cp = b->c_phase, sp = b->s_phase;

        for (int t = 0; t < n; t++) {
            double ii = i_full[t], qi = q_full[t];
            double mixed_i = ii*cp - qi*sp;
            double mixed_q = ii*sp + qi*cp;
            double cn = cp*c_step - sp*s_step;
            double sn = sp*c_step + cp*s_step;
            cp = cn; sp = sn;

            /* Stage 1: push into FIR1 delay line, decimate 16:1 */
            int pos1 = b->dl1_count % FIR_STAGE1_LEN;
            b->dl1_i[pos1] = mixed_i;
            b->dl1_q[pos1] = mixed_q;
            b->dl1_count++;

            if (b->dl1_count % SC_DEC1 != 0) continue;
            /* Output one 12k sample */
            double s1_i = fir_dot(b->dl1_i, pos1, FIR_STAGE1, FIR_STAGE1_LEN);
            double s1_q = fir_dot(b->dl1_q, pos1, FIR_STAGE1, FIR_STAGE1_LEN);

            /* Stage 2: push into FIR2 delay lines (100 Hz + 200 Hz paths), decimate 6:1 */
            int pos2_100 = b->dl2_count % FIR_S2_100_LEN;
            int pos2_200 = b->dl2_count % FIR_S2_200_LEN;
            b->dl2_100i[pos2_100] = s1_i;
            b->dl2_100q[pos2_100] = s1_q;
            b->dl2_200i[pos2_200] = s1_i;
            b->dl2_200q[pos2_200] = s1_q;
            b->dl2_count++;

            if (b->dl2_count % SC_DEC2 != 0) continue;
            /* Output one 2k sample per path */
            double s2_100i = fir_dot(b->dl2_100i, pos2_100, FIR_S2_100, FIR_S2_100_LEN);
            double s2_100q = fir_dot(b->dl2_100q, pos2_100, FIR_S2_100, FIR_S2_100_LEN);
            double s2_200i = fir_dot(b->dl2_200i, pos2_200, FIR_S2_200, FIR_S2_200_LEN);
            double s2_200q = fir_dot(b->dl2_200q, pos2_200, FIR_S2_200, FIR_S2_200_LEN);

            /* Envelope at 2 kHz */
            double env2_100 = sqrt(s2_100i*s2_100i + s2_100q*s2_100q);
            double env2_200 = sqrt(s2_200i*s2_200i + s2_200q*s2_200q);

            /* Stage 3: push envelope into FIR3 delay lines, decimate 10:1 */
            int pos3 = b->dl3_count % FIR_STAGE3_LEN;
            b->dl3_100[pos3] = env2_100;
            b->dl3_200[pos3] = env2_200;
            b->dl3_count++;

            if (b->dl3_count % SC_DEC3 != 0) continue;
            /* Output one 200 Hz envelope sample */
            double env_100 = fir_dot(b->dl3_100, pos3, FIR_STAGE3, FIR_STAGE3_LEN);
            double env_200 = fir_dot(b->dl3_200, pos3, FIR_STAGE3, FIR_STAGE3_LEN);

            if (b->env_n < SC_ENV_CAP) {
                b->env100[b->env_n] = env_100;
                b->env200[b->env_n] = env_200;
                b->env_n++;
            }
        }

        /* Renormalize oscillator */
        double norm = 1.0 / sqrt(cp*cp + sp*sp);
        b->c_phase = cp * norm;
        b->s_phase = sp * norm;
    }
}

/* ---- public API ---- */

ItilaSc *itila_sc_create(int sample_rate, double center_hz,
                          int max_bins, double min_snr,
                          int window_samples, int energy_win,
                          double grid_hz,
                          double band_min_hz, double band_max_hz,
                          const double *sos100_flat, int n_sos,
                          const double *sos200_flat)
{
    /* SOS coefficients ignored — FIR chain replaces IIR.
     * Signature kept for backward compatibility with Python wrapper. */
    (void)sos100_flat; (void)n_sos; (void)sos200_flat;
    if (max_bins > SC_MAX_BINS) max_bins = SC_MAX_BINS;
    if (energy_win < 4) energy_win = 4096;

    ItilaSc *sc = (ItilaSc *)calloc(1, sizeof(ItilaSc));
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

    sc->scan_i = (double *)calloc(energy_win, sizeof(double));
    sc->scan_q = (double *)calloc(energy_win, sizeof(double));
    if (!sc->scan_i || !sc->scan_q) { itila_sc_free(sc); return NULL; }

    return sc;
}

void itila_sc_free(ItilaSc *sc)
{
    if (!sc) return;
    free(sc->scan_i);
    free(sc->scan_q);
    free(sc);
}

double itila_sc_get_snr(ItilaSc *sc, double f_hz)
{
    for (int i = 0; i < SC_MAX_BINS; i++) {
        if (sc->bins[i].active && fabs(sc->bins[i].f_hz - f_hz) < 1.0)
            return sc->bins[i].snr_db;
    }
    return 0.0;
}

void itila_sc_mark_evidence(ItilaSc *sc, double f_hz)
{
    for (int i = 0; i < SC_MAX_BINS; i++) {
        if (sc->bins[i].active && fabs(sc->bins[i].f_hz - f_hz) < 1.0) {
            sc->bins[i].last_evidence = sc->total_samples;
            return;
        }
    }
}

void itila_sc_feed_iq(ItilaSc *sc,
                       const double *i_arr, const double *q_arr, int n)
{
    sc->total_samples += n / SC_DEC1;  /* count in 12kHz samples */
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

    /* --- IQ routing: prepend residual, process bins --- */
    int total   = sc->iq_res_n + n;
    int n_dec1  = (total / SC_DEC1) * SC_DEC1;
    int new_res = total - n_dec1;

    double *i_full = (double *)malloc(total * sizeof(double));
    double *q_full = (double *)malloc(total * sizeof(double));
    if (!i_full || !q_full) { free(i_full); free(q_full); return; }

    memcpy(i_full,                  sc->iq_res_i, sc->iq_res_n * sizeof(double));
    memcpy(q_full,                  sc->iq_res_q, sc->iq_res_n * sizeof(double));
    memcpy(i_full + sc->iq_res_n,  i_arr,         n * sizeof(double));
    memcpy(q_full + sc->iq_res_n,  q_arr,         n * sizeof(double));

    memcpy(sc->iq_res_i, i_full + n_dec1, new_res * sizeof(double));
    memcpy(sc->iq_res_q, q_full + n_dec1, new_res * sizeof(double));
    sc->iq_res_n = new_res;

    if (n_dec1 > 0 && sc->n_bins > 0)
        process_bins(sc, i_full, q_full, n_dec1);

    free(i_full);
    free(q_full);
}

int itila_sc_ready_bins(ItilaSc *sc, double *f_hz_out, int max_out)
{
    int count = 0;
    for (int i = 0; i < SC_MAX_BINS && count < max_out; i++) {
        if (sc->bins[i].active && sc->bins[i].env_n >= sc->window_samples)
            f_hz_out[count++] = sc->bins[i].f_hz;
    }
    return count;
}

int itila_sc_drain_env(ItilaSc *sc, double f_hz,
                        double *env100_out, double *env200_out, int max_n)
{
    for (int i = 0; i < SC_MAX_BINS; i++) {
        ScBin *b = &sc->bins[i];
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

int itila_sc_peek_env(ItilaSc *sc, double f_hz,
                       double *env100_out, double *env200_out, int max_n)
{
    for (int i = 0; i < SC_MAX_BINS; i++) {
        ScBin *b = &sc->bins[i];
        if (!b->active || fabs(b->f_hz - f_hz) >= 1.0) continue;
        int n = b->env_n < max_n ? b->env_n : max_n;
        memcpy(env100_out, b->env100, n * sizeof(double));
        memcpy(env200_out, b->env200, n * sizeof(double));
        return n;
    }
    return 0;
}

int itila_sc_bin_count(ItilaSc *sc)
{
    return sc->n_bins;
}

int itila_sc_env_n(ItilaSc *sc, double f_hz)
{
    for (int i = 0; i < SC_MAX_BINS; i++) {
        if (sc->bins[i].active && fabs(sc->bins[i].f_hz - f_hz) < 1.0)
            return sc->bins[i].env_n;
    }
    return 0;
}

int itila_sc_list_bins(ItilaSc *sc, double *f_hz_out, int max_out)
{
    int count = 0;
    for (int i = 0; i < SC_MAX_BINS && count < max_out; i++) {
        if (sc->bins[i].active)
            f_hz_out[count++] = sc->bins[i].f_hz;
    }
    return count;
}
