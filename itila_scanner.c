/*
 * itila_scanner.c — full _ItilaScanner in C
 *
 * Implements the complete per-feed pipeline:
 *   IQ residual → FFT energy scan → bin spawn →
 *   per-bin mix+decimate+IIR+envelope+decimate → 200 Hz accumulator
 *
 * Python calls only: itila_sc_feed_iq(), itila_sc_ready_bins(),
 * itila_sc_drain_env().  itila_feed() (the decoder) stays in Python.
 *
 * Compile:
 *   gcc -O3 -march=native -ffast-math -shared -fPIC \
 *       -o libitila_scanner.so itila_scanner.c -lm
 */

#include "itila_scanner.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

/* ---- compile-time limits ---- */
#define SC_MAX_BINS   128
#define SC_MAX_SOS    6
#define SC_DEC1       16      /* 192 kHz → 12 kHz */
#define SC_DEC2       60      /* 12 kHz → 200 Hz  */
#define SC_ENV_CAP    48000   /* 4 × 12000 — 4 decode windows at 200 Hz × 60 s */

/* ---- per-bin state ---- */
typedef struct {
    double f_hz;
    int    active;
    double c_phase, s_phase;           /* oscillator state */
    double zi100i[SC_MAX_SOS][2];
    double zi100q[SC_MAX_SOS][2];
    double zi200i[SC_MAX_SOS][2];
    double zi200q[SC_MAX_SOS][2];
    double res100[SC_DEC2];            /* 12k→200Hz decimation residuals */
    double res200[SC_DEC2];
    int    res_n;                      /* shared — always equal for both paths */
    double env100[SC_ENV_CAP];
    double env200[SC_ENV_CAP];
    int    env_n;
    int    created_sample;     /* sample count when bin was spawned */
    int    last_evidence;      /* sample count when itila_feed returned non-empty */
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
    int    n_sos;
    double sos100[SC_MAX_SOS][6];
    double sos200[SC_MAX_SOS][6];

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

typedef struct { double power; double f_hz; } ScPeak;
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

    double thresh  = noise + sc->min_snr;
    double bin_hz  = (double)sc->sample_rate / N;

    /* Collect LOCAL MAXIMA above threshold with parabolic interpolation */
    ScPeak *peaks = (ScPeak *)malloc(N * sizeof(ScPeak));
    if (!peaks) { free(psd); return; }
    int np = 0;
    for (int k = 1; k < N - 1; k++) {
        if (psd[k] <= thresh) continue;
        if (psd[k] <= psd[k-1] || psd[k] <= psd[k+1]) continue;  /* not a peak */
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
        np++;
    }
    free(psd);

    qsort(peaks, np, sizeof(ScPeak), cmp_peak_desc);

    /* Cluster (300 Hz), keep strongest per cluster, spawn new bins */
    double cluster_hz = 300.0;
    for (int i = 0; i < np; i++) {
        double f_hz = peaks[i].f_hz;

        /* Skip if within 300 Hz of any active bin */
        int found = 0;
        for (int b = 0; b < SC_MAX_BINS; b++) {
            if (sc->bins[b].active && fabs(sc->bins[b].f_hz - f_hz) < cluster_hz) {
                found = 1; break;
            }
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
        sc->n_bins++;
    }
    free(peaks);
}

/* ---- per-bin DSP ---- */
static void process_bins(ItilaSc *sc, const double *i_full, const double *q_full, int n)
{
    int dec_n = n / SC_DEC1;
    if (dec_n == 0) return;

    double *dec_i = (double *)malloc(dec_n * sizeof(double));
    double *dec_q = (double *)malloc(dec_n * sizeof(double));
    double *fi100 = (double *)malloc(dec_n * sizeof(double));
    double *fq100 = (double *)malloc(dec_n * sizeof(double));
    double *fi200 = (double *)malloc(dec_n * sizeof(double));
    double *fq200 = (double *)malloc(dec_n * sizeof(double));
    double *e100  = (double *)malloc(dec_n * sizeof(double));
    double *e200  = (double *)malloc(dec_n * sizeof(double));
    if (!dec_i || !dec_q || !fi100 || !fq100 ||
        !fi200 || !fq200 || !e100  || !e200) {
        free(dec_i); free(dec_q); free(fi100); free(fq100);
        free(fi200); free(fq200); free(e100);  free(e200);
        return;
    }

    for (int bi = 0; bi < SC_MAX_BINS; bi++) {
        ScBin *b = &sc->bins[bi];
        if (!b->active) continue;

        /* mix + 16:1 block-average decimation */
        double step   = -2.0 * M_PI * (b->f_hz - sc->center_hz) / sc->sample_rate;
        double c_step = cos(step);
        double s_step = sin(step);
        double cp = b->c_phase, sp = b->s_phase;

        for (int k = 0; k < dec_n; k++) {
            double si = 0.0, sq = 0.0;
            for (int j = 0; j < SC_DEC1; j++) {
                int t = k*SC_DEC1 + j;
                double ii = i_full[t], qi = q_full[t];
                si += ii*cp - qi*sp;
                sq += ii*sp + qi*cp;
                double cn = cp*c_step - sp*s_step;
                double sn = sp*c_step + cp*s_step;
                cp = cn; sp = sn;
            }
            dec_i[k] = si * (1.0 / SC_DEC1);
            dec_q[k] = sq * (1.0 / SC_DEC1);
        }
        /* renormalize oscillator to unit circle */
        double norm = 1.0 / sqrt(cp*cp + sp*sp);
        b->c_phase = cp * norm;
        b->s_phase = sp * norm;

        /* IIR sosfilt */
        memcpy(fi100, dec_i, dec_n * sizeof(double));
        memcpy(fq100, dec_q, dec_n * sizeof(double));
        sosfilt(sc->sos100, sc->n_sos, fi100, dec_n, b->zi100i);
        sosfilt(sc->sos100, sc->n_sos, fq100, dec_n, b->zi100q);
        memcpy(fi200, dec_i, dec_n * sizeof(double));
        memcpy(fq200, dec_q, dec_n * sizeof(double));
        sosfilt(sc->sos200, sc->n_sos, fi200, dec_n, b->zi200i);
        sosfilt(sc->sos200, sc->n_sos, fq200, dec_n, b->zi200q);

        /* envelope */
        for (int k = 0; k < dec_n; k++) {
            e100[k] = sqrt(fi100[k]*fi100[k] + fq100[k]*fq100[k]);
            e200[k] = sqrt(fi200[k]*fi200[k] + fq200[k]*fq200[k]);
        }

        /* 60:1 block-average decimation with residual.
         *
         * Both env100 and env200 use the same res_n counter since dec_n
         * is identical each call and they are always in sync.
         * We write both arrays starting at the same base index (b->env_n)
         * and increment env_n once after both are written.
         */
        int tot2  = b->res_n + dec_n;
        int n60   = (tot2 / SC_DEC2) * SC_DEC2;
        int n_new = n60 / SC_DEC2;
        int rem2  = tot2 - n60;

        if (n_new > 0) {
            double *comb100 = (double *)malloc(tot2 * sizeof(double));
            double *comb200 = (double *)malloc(tot2 * sizeof(double));
            if (comb100 && comb200) {
                memcpy(comb100,           b->res100, b->res_n * sizeof(double));
                memcpy(comb100 + b->res_n, e100,      dec_n    * sizeof(double));
                memcpy(comb200,           b->res200, b->res_n * sizeof(double));
                memcpy(comb200 + b->res_n, e200,      dec_n    * sizeof(double));

                int base = b->env_n;
                for (int k = 0; k < n_new && base + k < SC_ENV_CAP; k++) {
                    double s100 = 0.0, s200 = 0.0;
                    for (int j = 0; j < SC_DEC2; j++) {
                        s100 += comb100[k*SC_DEC2+j];
                        s200 += comb200[k*SC_DEC2+j];
                    }
                    b->env100[base+k] = s100 * (1.0/SC_DEC2);
                    b->env200[base+k] = s200 * (1.0/SC_DEC2);
                }
                int actual = n_new < (SC_ENV_CAP - b->env_n) ?
                             n_new : (SC_ENV_CAP - b->env_n);
                b->env_n += actual;

                /* save residual */
                memcpy(b->res100, comb100 + n60, rem2 * sizeof(double));
                memcpy(b->res200, comb200 + n60, rem2 * sizeof(double));
                b->res_n = rem2;
            }
            free(comb100); free(comb200);
        } else {
            /* No full DEC2 blocks yet — just append to residual */
            double *comb100 = (double *)malloc(tot2 * sizeof(double));
            double *comb200 = (double *)malloc(tot2 * sizeof(double));
            if (comb100 && comb200) {
                memcpy(comb100,           b->res100, b->res_n * sizeof(double));
                memcpy(comb100 + b->res_n, e100,      dec_n    * sizeof(double));
                memcpy(comb200,           b->res200, b->res_n * sizeof(double));
                memcpy(comb200 + b->res_n, e200,      dec_n    * sizeof(double));
                memcpy(b->res100, comb100, tot2 * sizeof(double));
                memcpy(b->res200, comb200, tot2 * sizeof(double));
                b->res_n = tot2;
            }
            free(comb100); free(comb200);
        }
    }

    free(dec_i); free(dec_q); free(fi100); free(fq100);
    free(fi200); free(fq200); free(e100);  free(e200);
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
    if (n_sos > SC_MAX_SOS) n_sos = SC_MAX_SOS;
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
    sc->n_sos          = n_sos;

    for (int s = 0; s < n_sos; s++) {
        memcpy(sc->sos100[s], sos100_flat + s*6, 6*sizeof(double));
        memcpy(sc->sos200[s], sos200_flat + s*6, 6*sizeof(double));
    }

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
