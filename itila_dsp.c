/*
 * itila_dsp.c — _ItilaScanner DSP core (C port)
 *
 * Per-bin hot path: IQ residual, complex mixing (cos/sin recurrence),
 * 16:1 block-average decimation, IIR Butterworth sosfilt (100 + 200 Hz),
 * envelope extraction, 60:1 block-average decimation → 200 Hz accumulator.
 *
 * FFT energy scan, bin spawning/eviction, and itila_feed() calls stay in Python.
 *
 * Compile:
 *   gcc -O3 -march=native -ffast-math -shared -fPIC \
 *       -o libitila_dsp.so itila_dsp.c -lm
 */

#include "itila_dsp.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

/* ---- compile-time limits ---- */
#define MAX_BINS  128
#define MAX_SOS   6         /* Butterworth order 6 = 3 sections */
#define DEC1      16        /* 192 kHz → 12 kHz */
#define DEC2      60        /* 12 kHz → 200 Hz */
#define ENV_CAP   36000     /* 3 × 12000 (3 decode windows) */

/* ---- per-bin state ---- */
typedef struct {
    double f_hz;
    int    active;

    /* oscillator state (cos/sin of current phase — avoids atan2 each block) */
    double c_phase;
    double s_phase;

    /* IIR filter state — Direct Form II transposed, per section */
    double zi100i[MAX_SOS][2];
    double zi100q[MAX_SOS][2];
    double zi200i[MAX_SOS][2];
    double zi200q[MAX_SOS][2];

    /* 12 kHz → 200 Hz decimation residuals */
    double res100[DEC2];
    double res200[DEC2];
    int    res100_n;
    int    res200_n;

    /* 200 Hz envelope accumulator */
    double env100[ENV_CAP];
    double env200[ENV_CAP];
    int    env100_n;
    int    env200_n;
} BinState;

/* ---- engine ---- */
struct ItilaDsp {
    int    sample_rate;
    double center_hz;
    int    max_bins;
    int    n_sos;
    double sos100[MAX_SOS][6];   /* [b0,b1,b2,a0,a1,a2] — a0 unused */
    double sos200[MAX_SOS][6];

    /* IQ routing residual (non-multiple-of-DEC1 chunk carry-over) */
    double iq_res_i[DEC1];
    double iq_res_q[DEC1];
    int    iq_res_n;

    int    n_bins;
    BinState bins[MAX_BINS];
};

/* ---- IIR: Direct Form II transposed ---- */
static void sosfilt_inplace(const double sos[][6], int n_sos,
                             double *x, int n, double z[][2])
{
    for (int s = 0; s < n_sos; s++) {
        double b0 = sos[s][0], b1 = sos[s][1], b2 = sos[s][2];
        double a1 = sos[s][4], a2 = sos[s][5];
        double z0 = z[s][0],   z1 = z[s][1];
        for (int i = 0; i < n; i++) {
            double xi = x[i];
            double yi = b0 * xi + z0;
            z0 = b1 * xi - a1 * yi + z1;
            z1 = b2 * xi - a2 * yi;
            x[i] = yi;
        }
        z[s][0] = z0;
        z[s][1] = z1;
    }
}

/* ---- public API ---- */

ItilaDsp *itila_dsp_create(int sample_rate, double center_hz, int max_bins,
                            const double *sos100_flat, int n_sos,
                            const double *sos200_flat)
{
    if (n_sos > MAX_SOS) n_sos = MAX_SOS;
    if (max_bins > MAX_BINS) max_bins = MAX_BINS;

    ItilaDsp *dsp = (ItilaDsp *)calloc(1, sizeof(ItilaDsp));
    if (!dsp) return NULL;

    dsp->sample_rate = sample_rate;
    dsp->center_hz   = center_hz;
    dsp->max_bins    = max_bins;
    dsp->n_sos       = n_sos;

    for (int s = 0; s < n_sos; s++) {
        memcpy(dsp->sos100[s], sos100_flat + s * 6, 6 * sizeof(double));
        memcpy(dsp->sos200[s], sos200_flat + s * 6, 6 * sizeof(double));
    }
    return dsp;
}

void itila_dsp_free(ItilaDsp *dsp)
{
    free(dsp);
}

int itila_dsp_add_bin(ItilaDsp *dsp, double f_hz)
{
    /* Check for existing bin within 1 Hz */
    for (int i = 0; i < MAX_BINS; i++) {
        if (dsp->bins[i].active && fabs(dsp->bins[i].f_hz - f_hz) < 1.0)
            return 1;
    }
    if (dsp->n_bins >= dsp->max_bins)
        return 0;

    int slot = -1;
    for (int i = 0; i < MAX_BINS; i++) {
        if (!dsp->bins[i].active) { slot = i; break; }
    }
    if (slot < 0) return 0;

    BinState *b = &dsp->bins[slot];
    memset(b, 0, sizeof(BinState));
    b->f_hz    = f_hz;
    b->active  = 1;
    b->c_phase = 1.0;   /* cos(0) */
    b->s_phase = 0.0;   /* sin(0) */
    dsp->n_bins++;
    return 1;
}

void itila_dsp_remove_bin(ItilaDsp *dsp, double f_hz)
{
    for (int i = 0; i < MAX_BINS; i++) {
        if (dsp->bins[i].active && fabs(dsp->bins[i].f_hz - f_hz) < 1.0) {
            dsp->bins[i].active = 0;
            dsp->n_bins--;
            return;
        }
    }
}

int itila_dsp_bin_count(ItilaDsp *dsp)
{
    return dsp->n_bins;
}

int itila_dsp_env_n(ItilaDsp *dsp, double f_hz)
{
    for (int i = 0; i < MAX_BINS; i++) {
        if (dsp->bins[i].active && fabs(dsp->bins[i].f_hz - f_hz) < 1.0)
            return dsp->bins[i].env100_n;
    }
    return 0;
}

int itila_dsp_drain_env(ItilaDsp *dsp, double f_hz,
                         double *env100_out, double *env200_out, int max_n)
{
    for (int i = 0; i < MAX_BINS; i++) {
        BinState *b = &dsp->bins[i];
        if (!b->active || fabs(b->f_hz - f_hz) >= 1.0) continue;
        int n = b->env100_n < max_n ? b->env100_n : max_n;
        memcpy(env100_out, b->env100, n * sizeof(double));
        memcpy(env200_out, b->env200, n * sizeof(double));
        int rem = b->env100_n - n;
        memmove(b->env100, b->env100 + n, rem * sizeof(double));
        memmove(b->env200, b->env200 + n, rem * sizeof(double));
        b->env100_n = rem;
        b->env200_n = rem;
        return n;
    }
    return 0;
}

/* ---- feed ---- */

void itila_dsp_feed(ItilaDsp *dsp,
                    const double *i_arr, const double *q_arr, int n)
{
    /* Prepend carry-over residual */
    int total  = dsp->iq_res_n + n;
    int n_dec1 = (total / DEC1) * DEC1;   /* largest multiple of DEC1 */
    int new_res = total - n_dec1;

    double *i_full = (double *)malloc(total * sizeof(double));
    double *q_full = (double *)malloc(total * sizeof(double));
    if (!i_full || !q_full) { free(i_full); free(q_full); return; }

    memcpy(i_full,               dsp->iq_res_i, dsp->iq_res_n * sizeof(double));
    memcpy(q_full,               dsp->iq_res_q, dsp->iq_res_n * sizeof(double));
    memcpy(i_full + dsp->iq_res_n, i_arr,       n * sizeof(double));
    memcpy(q_full + dsp->iq_res_n, q_arr,       n * sizeof(double));

    /* Save new residual */
    memcpy(dsp->iq_res_i, i_full + n_dec1, new_res * sizeof(double));
    memcpy(dsp->iq_res_q, q_full + n_dec1, new_res * sizeof(double));
    dsp->iq_res_n = new_res;

    if (n_dec1 == 0) { free(i_full); free(q_full); return; }

    int dec_n = n_dec1 / DEC1;   /* samples at 12 kHz */

    double *dec_i  = (double *)malloc(dec_n * sizeof(double));
    double *dec_q  = (double *)malloc(dec_n * sizeof(double));
    double *fi100  = (double *)malloc(dec_n * sizeof(double));
    double *fq100  = (double *)malloc(dec_n * sizeof(double));
    double *fi200  = (double *)malloc(dec_n * sizeof(double));
    double *fq200  = (double *)malloc(dec_n * sizeof(double));
    double *e100   = (double *)malloc(dec_n * sizeof(double));
    double *e200   = (double *)malloc(dec_n * sizeof(double));

    if (!dec_i || !dec_q || !fi100 || !fq100 ||
        !fi200 || !fq200 || !e100  || !e200) {
        free(i_full); free(q_full);
        free(dec_i);  free(dec_q);
        free(fi100);  free(fq100);
        free(fi200);  free(fq200);
        free(e100);   free(e200);
        return;
    }

    for (int bi = 0; bi < MAX_BINS; bi++) {
        BinState *b = &dsp->bins[bi];
        if (!b->active) continue;

        /* ---- mix + 16:1 block-average decimation ----
         *
         * Phase advances by step radians per sample.
         * Oscillator updated via complex multiply (4 mul + 2 add per sample)
         * instead of cos/sin — avoids transcendental per sample.
         */
        double offset_hz = b->f_hz - dsp->center_hz;
        double step = -2.0 * M_PI * offset_hz / dsp->sample_rate;
        double c_step = cos(step);
        double s_step = sin(step);
        double cp = b->c_phase;
        double sp = b->s_phase;

        for (int k = 0; k < dec_n; k++) {
            double sum_i = 0.0, sum_q = 0.0;
            for (int j = 0; j < DEC1; j++) {
                int t = k * DEC1 + j;
                double ii = i_full[t], qi = q_full[t];
                sum_i += ii * cp - qi * sp;
                sum_q += ii * sp + qi * cp;
                /* advance oscillator */
                double cn = cp * c_step - sp * s_step;
                double sn = sp * c_step + cp * s_step;
                cp = cn; sp = sn;
            }
            dec_i[k] = sum_i * (1.0 / DEC1);
            dec_q[k] = sum_q * (1.0 / DEC1);
        }
        /* Renormalize oscillator state to unit circle to prevent drift */
        double norm = 1.0 / sqrt(cp * cp + sp * sp);
        b->c_phase = cp * norm;
        b->s_phase = sp * norm;

        /* ---- IIR sosfilt 100 Hz ---- */
        memcpy(fi100, dec_i, dec_n * sizeof(double));
        memcpy(fq100, dec_q, dec_n * sizeof(double));
        sosfilt_inplace(dsp->sos100, dsp->n_sos, fi100, dec_n, b->zi100i);
        sosfilt_inplace(dsp->sos100, dsp->n_sos, fq100, dec_n, b->zi100q);

        /* ---- IIR sosfilt 200 Hz ---- */
        memcpy(fi200, dec_i, dec_n * sizeof(double));
        memcpy(fq200, dec_q, dec_n * sizeof(double));
        sosfilt_inplace(dsp->sos200, dsp->n_sos, fi200, dec_n, b->zi200i);
        sosfilt_inplace(dsp->sos200, dsp->n_sos, fq200, dec_n, b->zi200q);

        /* ---- envelope ---- */
        for (int k = 0; k < dec_n; k++) {
            e100[k] = sqrt(fi100[k]*fi100[k] + fq100[k]*fq100[k]);
            e200[k] = sqrt(fi200[k]*fi200[k] + fq200[k]*fq200[k]);
        }

        /* ---- 60:1 block-average decimation with residual carry-over ---- */
        for (int pass = 0; pass < 2; pass++) {
            double *env_in  = pass == 0 ? e100   : e200;
            double *res     = pass == 0 ? b->res100 : b->res200;
            int    *res_n   = pass == 0 ? &b->res100_n : &b->res200_n;
            double *env_out = pass == 0 ? b->env100 : b->env200;
            int    *env_n   = pass == 0 ? &b->env100_n : &b->env200_n;

            int tot2  = *res_n + dec_n;
            /* combined = residual ++ env_in */
            double *comb = (double *)malloc(tot2 * sizeof(double));
            if (!comb) continue;
            memcpy(comb,         res,     *res_n * sizeof(double));
            memcpy(comb + *res_n, env_in, dec_n  * sizeof(double));

            int n60  = (tot2 / DEC2) * DEC2;
            int rem2 = tot2 - n60;
            memcpy(res, comb + n60, rem2 * sizeof(double));
            *res_n = rem2;

            int n_new = n60 / DEC2;
            for (int k = 0; k < n_new && *env_n < ENV_CAP; k++) {
                double sum = 0.0;
                for (int j = 0; j < DEC2; j++) sum += comb[k * DEC2 + j];
                env_out[(*env_n)++] = sum * (1.0 / DEC2);
            }
            free(comb);
        }
    }

    free(i_full); free(q_full);
    free(dec_i);  free(dec_q);
    free(fi100);  free(fq100);
    free(fi200);  free(fq200);
    free(e100);   free(e200);
}
