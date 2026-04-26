/*
 * itila_core.c — ITILA CW decoder — full C implementation
 *
 * Port of itila_cw.py (Python prototype, M8, arc-itila-decoder branch).
 * Architecture: 2-state HMM forward-backward (via fb_core.c), EM parameter
 * estimation, speed marginalization, beam-search run-length decode, 3-pass
 * callsign extraction, M8 multi-station GMM WPM detection.
 *
 * Compile:
 *   gcc -O3 -march=native -ffast-math -shared -fPIC \
 *       -o libitila.so itila_core.c fb_core.c -lm
 *
 * See itila.h for public API.
 */

#include "itila.h"
#include <math.h>
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#include <assert.h>

/* -------------------------------------------------------------------------
 * Constants — must match itila_cw.py exactly
 * ---------------------------------------------------------------------- */
#define BAYES_RATE      200
#define N_SPEED_BINS    16
#define WPM_MIN         8.0
#define WPM_MAX         60.0
#define MAX_ENV         200000   /* ~16.7 min at 200 Hz */
#define MAX_BEAM        64       /* working beam before pruning */
#define MAX_TEXTS       32       /* final hypothesis cap */
#define MAX_SYM         8        /* max Morse symbol length + null */
#define MAX_TEXT        2048     /* max decoded text per chunk */
#define MAX_CALLS       64       /* max callsigns per chunk */
#define MAX_CALL        8        /* max callsign length + null */
#define RESULT_BUF      2048     /* output buffer — must hold MAX_TEXT */

/* Forward declaration of fb_core (fb_core.c) */
void fb_core(const double* log_B, const double* log_T, int T,
             double* log_alpha, double* log_beta, double* log_Z_out);

/* -------------------------------------------------------------------------
 * Scratch buffer types (moved here from function-local definitions for
 * use as per-handle fields in itila_state_t below).  Live mode runs
 * multiple per-RX worker threads concurrently in itila_feed; the
 * function-local statics that previously held these were shared across
 * threads and corrupted each other's beam/extraction state.  Storing
 * them per-handle eliminates that contention.
 * ---------------------------------------------------------------------- */
typedef struct { int is_mark; int dur; } run_t;

typedef struct {
    double score;
    char   sym[MAX_SYM];
    char   txt[MAX_TEXT];
} beam_state_t;

typedef struct { char call[MAX_CALL]; int count; } callsign_t;

/* -------------------------------------------------------------------------
 * Internal state
 * ---------------------------------------------------------------------- */
typedef struct {
    int    sample_rate;
    double lpf_hz;

    /* Working buffers — heap allocated in itila_create */
    double *log_B;       /* [MAX_ENV * 2] observation log-likelihoods */
    double *log_alpha;   /* [MAX_ENV * 2] forward log-probs */
    double *log_beta;    /* [MAX_ENV * 2] backward log-probs */
    double *gamma;       /* [MAX_ENV * 2] per-bin posteriors */
    double *gamma_marg;  /* [MAX_ENV * 2] marginalized posteriors */
    double *env_norm;    /* [MAX_ENV]     normalized envelope */
    int8_t *marks;       /* [MAX_ENV]     binary mark/space */

    /* Per-handle scratch — moved out of function-local statics for
     * thread-safety (root cause found 2026-04-26).  Used by
     * decode_runs_beam, extract_callsigns, itila_feed, itila_feed_online. */
    run_t        *sc_runs;          /* MAX_ENV */
    beam_state_t *sc_beamA;         /* MAX_BEAM */
    beam_state_t *sc_beamB;         /* MAX_BEAM */
    beam_state_t *sc_dedup;         /* MAX_BEAM */
    char         *sc_up;            /* MAX_TEXT  — uppercase copy in extract_callsigns */
    char         (*sc_tokens)[MAX_CALL+4];  /* 256 entries — tokens in extract_callsigns */
    callsign_t   *sc_calls;         /* MAX_CALLS */
    char         (*sc_out_texts)[MAX_TEXT]; /* MAX_TEXTS entries */
    char         *sc_primary;       /* MAX_TEXT — primary_text */

    /* Speed bins */
    double speed_bins[N_SPEED_BINS];
    double log_Z_bins[N_SPEED_BINS];
    double speed_post[N_SPEED_BINS];

    /* Warm-start: converged EM params from the previous itila_feed() call.
     * Stored normalized (divided by env_scale) so they survive envelope
     * scale changes between calls.  All zero = cold start. */
    double ws_wpm;
    double ws_A_norm;
    double ws_nm_norm;
    double ws_s2_norm;

    /* Online EM sufficient statistics (raw envelope units, λ-weighted).
     *
     * oss_N{0,1}: sum of per-sample responsibilities for noise/signal state.
     * oss_S{0,1}: sum of responsibility-weighted envelope values.
     * oss_Q{0,1}: sum of responsibility-weighted envelope squares.
     * oss_{nm,A,s2}: current M-step params (raw units).
     * oss_wpm: EMA of per-call wpm estimates.
     * oss_init: 0 = first call (cold-start needed).
     */
    double oss_N0, oss_S0, oss_Q0;
    double oss_N1, oss_S1, oss_Q1;
    double oss_nm, oss_A, oss_s2;
    double oss_wpm;
    int    oss_init;

    /* Output */
    char result_buf[RESULT_BUF];
    double last_wpm;       /* WPM from most recent successful decode */
} itila_state_t;

/* -------------------------------------------------------------------------
 * Math helpers
 * ---------------------------------------------------------------------- */
static inline double logaddexp(double a, double b) {
    if (a > b) {
        double d = b - a;
        return a + (d < -40.0 ? 0.0 : log1p(exp(d)));
    } else {
        double d = a - b;
        return b + (d < -40.0 ? 0.0 : log1p(exp(d)));
    }
}

static inline double log_normal(double x, double mu, double var) {
    return -0.5 * ((x - mu) * (x - mu) / var + log(2.0 * M_PI * var));
}

/* Percentile of array (modifies a scratch copy) */
static int cmp_double(const void *a, const void *b) {
    double x = *(const double*)a, y = *(const double*)b;
    return (x > y) - (x < y);
}

static double percentile(const double *arr, int n, double pct, double *scratch) {
    memcpy(scratch, arr, n * sizeof(double));
    qsort(scratch, n, sizeof(double), cmp_double);
    double idx = pct / 100.0 * (n - 1);
    int lo = (int)idx;
    int hi = lo + 1 < n ? lo + 1 : lo;
    double frac = idx - lo;
    return scratch[lo] * (1.0 - frac) + scratch[hi] * frac;
}

/* -------------------------------------------------------------------------
 * Morse table
 * ---------------------------------------------------------------------- */
typedef struct { const char *pat; char ch; } morse_entry_t;

static const morse_entry_t MORSE_TABLE[] = {
    {".-",   'A'}, {"-...", 'B'}, {"-.-.", 'C'}, {"-..",  'D'}, {".",    'E'},
    {"..-.", 'F'}, {"--.",  'G'}, {"....", 'H'}, {"..",   'I'}, {".---", 'J'},
    {"-.-",  'K'}, {".-..", 'L'}, {"--",   'M'}, {"-.",   'N'}, {"---",  'O'},
    {".--.", 'P'}, {"--.-", 'Q'}, {".-.",  'R'}, {"...",  'S'}, {"-",    'T'},
    {"..-",  'U'}, {"...-", 'V'}, {".--",  'W'}, {"-..-", 'X'}, {"-.--", 'Y'},
    {"--..", 'Z'},
    {"-----",'0'}, {".----",'1'}, {"..---",'2'}, {"...--",'3'}, {"....-",'4'},
    {".....", '5'},{"-....","6"[0]},{"--...","7"[0]},{"---..","8"[0]},{"----.","9"[0]},
    {"..--..","?"[0]}, {".-.-.-","."[0]}, {"--..--",","[0]},
    {"-..-.","/"[0]}, {"-....-","-"[0]},
    {NULL, 0}
};

static char morse_lookup(const char *sym) {
    for (int i = 0; MORSE_TABLE[i].pat; i++)
        if (strcmp(sym, MORSE_TABLE[i].pat) == 0)
            return MORSE_TABLE[i].ch;
    return '?';
}

/* -------------------------------------------------------------------------
 * unit_samples and transition_probs — match Python exactly
 * ---------------------------------------------------------------------- */
static double unit_samples(double wpm) {
    double u = 240.0 / wpm;  /* = 1200/wpm * BAYES_RATE/1000 */
    return u < 1.0 ? 1.0 : u;
}

static void transition_probs(double wpm, double *p01, double *p10) {
    double d = unit_samples(wpm);
    double pm2s = 1.0 / (2.0 * d);
    double ps2m = 1.0 / (2.5 * d);
    if (pm2s < 1e-6) pm2s = 1e-6; if (pm2s > 0.5) pm2s = 0.5;
    if (ps2m < 1e-6) ps2m = 1e-6; if (ps2m > 0.5) ps2m = 0.5;
    *p01 = ps2m;
    *p10 = pm2s;
}

/* -------------------------------------------------------------------------
 * forward_backward_fast — builds log_B then calls fb_core
 * ---------------------------------------------------------------------- */
static double forward_backward_fast(
    itila_state_t *st,
    const double *env, int T,
    double wpm, double A, double noise_mean, double sigma2_obs,
    double *gamma_out)   /* [T*2] output, row-major */
{
    double p01, p10;
    transition_probs(wpm, &p01, &p10);
    double log_T[4] = {
        log(1.0 - p01 + 1e-300), log(p01 + 1e-300),
        log(p10 + 1e-300),       log(1.0 - p10 + 1e-300)
    };

    double log_norm = -0.5 * log(2.0 * M_PI * sigma2_obs);
    for (int t = 0; t < T; t++) {
        st->log_B[t*2+0] = log_norm - 0.5*(env[t]-noise_mean)*(env[t]-noise_mean)/sigma2_obs;
        st->log_B[t*2+1] = log_norm - 0.5*(env[t]-A)*(env[t]-A)/sigma2_obs;
    }

    double log_Z;
    fb_core(st->log_B, log_T, T, st->log_alpha, st->log_beta, &log_Z);

    /* Compute gamma = softmax(log_alpha + log_beta) */
    for (int t = 0; t < T; t++) {
        double la0 = st->log_alpha[t*2+0], la1 = st->log_alpha[t*2+1];
        double lb0 = st->log_beta[t*2+0],  lb1 = st->log_beta[t*2+1];
        double lg0 = la0 + lb0, lg1 = la1 + lb1;
        double lZ  = logaddexp(lg0, lg1);
        gamma_out[t*2+0] = exp(lg0 - lZ);
        gamma_out[t*2+1] = exp(lg1 - lZ);
    }
    return log_Z;
}

/* -------------------------------------------------------------------------
 * estimate_wpm_from_gamma — GMM dit-component estimation
 *
 * Fits a 2-component Gaussian mixture to log mark-run durations.
 * The lower-mean component is the dit cluster; WPM = 240 / exp(mu_dit).
 * Falls back to P25 if insufficient runs or GMM doesn't converge.
 * ---------------------------------------------------------------------- */
static double estimate_wpm_from_gamma(const double *gamma, int T) {
    /* Extract mark runs */
    double runs[8192]; int n_runs = 0;
    int val = gamma[1] > 0.5 ? 1 : 0, cnt = 1;
    for (int i = 1; i < T; i++) {
        int v = gamma[i*2+1] > 0.5 ? 1 : 0;
        if (v == val) { cnt++; }
        else {
            if (val == 1 && cnt >= 2 && n_runs < 8192)
                runs[n_runs++] = log((double)cnt + 0.5);
            val = v; cnt = 1;
        }
    }
    if (val == 1 && cnt >= 2 && n_runs < 8192)
        runs[n_runs++] = log((double)cnt + 0.5);
    if (n_runs < 5) return -1.0;

    /* K=1 mean and variance */
    double mu1 = 0.0;
    for (int i = 0; i < n_runs; i++) mu1 += runs[i];
    mu1 /= n_runs;
    double var1 = 0.0;
    for (int i = 0; i < n_runs; i++) { double d = runs[i]-mu1; var1 += d*d; }
    var1 = var1/n_runs + 1e-6;

    /* Check if there's enough spread for bimodality (dit vs dah) */
    double sorted[8192];
    memcpy(sorted, runs, n_runs * sizeof(double));
    qsort(sorted, n_runs, sizeof(double), cmp_double);
    double q25 = sorted[(int)(0.25*(n_runs-1))];
    double q75 = sorted[(int)(0.75*(n_runs-1))];

    if (q75 - q25 < 0.3 || n_runs < 15) {
        /* Unimodal (single station) — P25 targets the dit cluster naturally */
        double dit_samples = exp(sorted[(int)(0.25*(n_runs-1))]);
        if (dit_samples < 1.0) return -1.0;
        double wpm = 240.0 / dit_samples;
        if (wpm < WPM_MIN) wpm = WPM_MIN;
        if (wpm > WPM_MAX) wpm = WPM_MAX;
        return wpm;
    }

    /* K=2 EM in log-space: separate dit and dah clusters */
    double mu[2] = {q25, q75};
    double vr[2] = {var1*0.5, var1*0.5};
    double pi[2] = {0.5, 0.5};
    double g0[8192];

    for (int iter = 0; iter < 20; iter++) {
        for (int i = 0; i < n_runs; i++) {
            double lr0 = log(pi[0]+1e-300) - 0.5*(runs[i]-mu[0])*(runs[i]-mu[0])/vr[0] - 0.5*log(2*M_PI*vr[0]);
            double lr1 = log(pi[1]+1e-300) - 0.5*(runs[i]-mu[1])*(runs[i]-mu[1])/vr[1] - 0.5*log(2*M_PI*vr[1]);
            double lZ  = lr0 > lr1 ? lr0 + log(1+exp(lr1-lr0)) : lr1 + log(1+exp(lr0-lr1));
            g0[i] = exp(lr0 - lZ);
        }
        double N0=1e-10, N1=1e-10, s0=0, s1=0;
        for (int i=0;i<n_runs;i++){N0+=g0[i];N1+=(1-g0[i]);s0+=g0[i]*runs[i];s1+=(1-g0[i])*runs[i];}
        pi[0]=N0/n_runs; pi[1]=N1/n_runs;
        mu[0]=s0/N0; mu[1]=s1/N1;
        double v0=1e-6,v1=1e-6;
        for(int i=0;i<n_runs;i++){
            double d0=runs[i]-mu[0],d1=runs[i]-mu[1];
            v0+=g0[i]*d0*d0; v1+=(1-g0[i])*d1*d1;
        }
        vr[0]=v0/N0; vr[1]=v1/N1;
    }

    /* Dit component is the one with the smaller mean (shorter runs) */
    double mu_dit = mu[0] < mu[1] ? mu[0] : mu[1];
    double dit_samples = exp(mu_dit);
    if (dit_samples < 1.0) return -1.0;
    double wpm = 240.0 / dit_samples;
    if (wpm < WPM_MIN) wpm = WPM_MIN;
    if (wpm > WPM_MAX) wpm = WPM_MAX;
    return wpm;
}

/* -------------------------------------------------------------------------
 * em_estimate — two-phase EM for A, noise_mean, sigma2_obs, wpm
 * ---------------------------------------------------------------------- */
static void em_estimate(
    itila_state_t *st,
    const double *env_raw, int T,
    double wpm_seed,   /* initial WPM; 0 = use default 25 */
    double *A_out, double *noise_mean_out, double *sigma2_out, double *wpm_out)
{
    /* Normalize to [0,1] via p99 */
    double env_scale = percentile(env_raw, T, 99.0, st->env_norm);
    if (env_scale < 1e-30) {
        *A_out = 0.0; *noise_mean_out = 0.0;
        *sigma2_out = 1.0; *wpm_out = 25.0;
        return;
    }
    double *env = st->env_norm;
    for (int i = 0; i < T; i++) env[i] = env_raw[i] / env_scale;

    /* Initialize — warm-start if valid params passed, else use percentile heuristics */
    double nm, A, s2, wpm;
    int half;

    if (wpm_seed > 0.0 && st->ws_A_norm > 0.0 && st->ws_nm_norm > 0.0) {
        /* Warm start: rescale stored normalized params to current envelope scale */
        nm  = st->ws_nm_norm;
        A   = st->ws_A_norm;
        s2  = st->ws_s2_norm;
        wpm = wpm_seed;
        /* All 10 iterations on warm WPM — no need for phase-1 cold search */
        half = 0;
    } else {
        /* Cold start: percentile initialization */
        nm = percentile(env, T, 30.0, st->gamma);  /* scratch: gamma buf */
        double p95 = percentile(env, T, 95.0, st->gamma);
        double p5  = percentile(env, T, 5.0,  st->gamma);
        double p40 = percentile(env, T, 40.0, st->gamma);

        double var_lo = 0.0; int nlo = 0;
        for (int i = 0; i < T; i++)
            if (env[i] < p40) { var_lo += (env[i]-nm)*(env[i]-nm); nlo++; }
        if (nlo > 2) var_lo /= nlo; else var_lo = 0.0;

        double env_spread = p95 - p5;
        s2 = var_lo;
        double floor1 = (env_spread * 0.15) * (env_spread * 0.15);
        if (s2 < floor1) s2 = floor1;
        if (s2 < 1e-6)   s2 = 1e-6;

        double thresh = nm + 3.0 * sqrt(s2);
        A = 0.0; int nhi = 0;
        for (int i = 0; i < T; i++) if (env[i] > thresh) { A += env[i]; nhi++; }
        if (nhi > 10) A /= nhi; else A = p95;

        wpm = 25.0;
        half = 5;
    }

    /* One EM step: update A, nm, s2 given current wpm */
    #define EM_STEP(wpm_val, n_iter)  do { \
        for (int _iter = 0; _iter < (n_iter); _iter++) { \
            forward_backward_fast(st, env, T, wpm_val, A, nm, s2, st->gamma); \
            double dm = 1e-10, ds = 1e-10, sA = 0, snm = 0; \
            for (int _i = 0; _i < T; _i++) { \
                double gm = st->gamma[_i*2+1], gs = st->gamma[_i*2+0]; \
                dm += gm; ds += gs; sA += gm*env[_i]; snm += gs*env[_i]; \
            } \
            double A_new = sA/dm; double nm_new = snm/ds; \
            if (A_new < nm_new + 1e-10) A_new = nm_new + 1e-10; \
            double vm = 0, vs = 0; \
            for (int _i = 0; _i < T; _i++) { \
                double gm = st->gamma[_i*2+1], gs = st->gamma[_i*2+0]; \
                double da = env[_i]-A_new, dn = env[_i]-nm_new; \
                vm += gm*da*da; vs += gs*dn*dn; \
            } \
            s2 = (vm/dm + vs/ds) / 2.0; \
            if (s2 < 1e-20) s2 = 1e-20; \
            A = A_new; nm = nm_new; \
        } \
        forward_backward_fast(st, env, T, wpm_val, A, nm, s2, st->gamma); \
    } while(0)

    EM_STEP(wpm, half);

    /* Phase 2: re-estimate WPM from gamma (always — even warm start refines it) */
    double wpm_new = estimate_wpm_from_gamma(st->gamma, T);
    if (wpm_new > 0.0) wpm = wpm_new;
    EM_STEP(wpm, 10 - half);

    #undef EM_STEP

    /* Post-hoc sharpening: one extra FB pass with per-state variances
     * for sharper mark/space boundaries at decode time. Does NOT feed
     * back into EM — parameters (A, nm, wpm) are already converged. */
    {
        double dm = 1e-10, ds = 1e-10, vs_post = 0, vm_post = 0;
        for (int i = 0; i < T; i++) {
            double gm = st->gamma[i*2+1], gs = st->gamma[i*2+0];
            dm += gm; ds += gs;
            vm_post += gm * (env[i]-A)*(env[i]-A);
            vs_post += gs * (env[i]-nm)*(env[i]-nm);
        }
        double s2s = vs_post / ds, s2m = vm_post / dm;
        if (s2s < 0.1 * s2) s2s = 0.1 * s2;  /* floor prevents collapse */
        if (s2m < 0.1 * s2) s2m = 0.1 * s2;
        /* Shared normalization (geometric mean) with separate exponents */
        double s2_norm = sqrt(s2s * s2m);
        double ln_norm = -0.5 * log(2.0 * M_PI * s2_norm);
        for (int t = 0; t < T; t++) {
            st->log_B[t*2+0] = ln_norm - 0.5*(env[t]-nm)*(env[t]-nm)/s2s;
            st->log_B[t*2+1] = ln_norm - 0.5*(env[t]-A)*(env[t]-A)/s2m;
        }
        double p01, p10;
        transition_probs(wpm, &p01, &p10);
        double log_T[4] = {
            log(1.0-p01+1e-300), log(p01+1e-300),
            log(p10+1e-300),     log(1.0-p10+1e-300)
        };
        double log_Z_post;
        fb_core(st->log_B, log_T, T, st->log_alpha, st->log_beta, &log_Z_post);
        for (int t = 0; t < T; t++) {
            double lg0 = st->log_alpha[t*2+0] + st->log_beta[t*2+0];
            double lg1 = st->log_alpha[t*2+1] + st->log_beta[t*2+1];
            double lZ  = logaddexp(lg0, lg1);
            st->gamma[t*2+0] = exp(lg0 - lZ);
            st->gamma[t*2+1] = exp(lg1 - lZ);
        }
    }

    /* Save normalized params for warm-start on next call */
    st->ws_wpm    = wpm;
    st->ws_A_norm  = A;
    st->ws_nm_norm = nm;
    st->ws_s2_norm = s2;

    /* Denormalize */
    *A_out          = A  * env_scale;
    *noise_mean_out = nm * env_scale;
    *sigma2_out     = s2 * env_scale * env_scale;
    *wpm_out        = wpm;
}

/* -------------------------------------------------------------------------
 * decode_marginal — parallel FB over speed bins
 * ---------------------------------------------------------------------- */
static void decode_marginal(
    itila_state_t *st,
    const double *env, int T,
    double A, double noise_mean, double sigma2_obs)
    /* Results in st->gamma_marg[T*2], st->log_Z_bins[], st->speed_post[] */
{
    /* Pass 1: compute log_Z for each speed bin */
    for (int b = 0; b < N_SPEED_BINS; b++) {
        st->log_Z_bins[b] = forward_backward_fast(
            st, env, T, st->speed_bins[b], A, noise_mean, sigma2_obs,
            st->gamma);
    }

    /* Compute speed posterior (uniform prior) */
    double lmax = st->log_Z_bins[0];
    for (int b = 1; b < N_SPEED_BINS; b++)
        if (st->log_Z_bins[b] > lmax) lmax = st->log_Z_bins[b];
    double lsum = 0.0;
    for (int b = 0; b < N_SPEED_BINS; b++)
        lsum += exp(st->log_Z_bins[b] - lmax);
    double log_norm_sp = lmax + log(lsum);
    for (int b = 0; b < N_SPEED_BINS; b++)
        st->speed_post[b] = exp(st->log_Z_bins[b] - log_norm_sp);

    /* Pass 2: accumulate weighted gamma_marg */
    memset(st->gamma_marg, 0, T * 2 * sizeof(double));
    for (int b = 0; b < N_SPEED_BINS; b++) {
        double w = st->speed_post[b];
        if (w < 1e-10) continue;
        forward_backward_fast(st, env, T, st->speed_bins[b],
                               A, noise_mean, sigma2_obs, st->gamma);
        for (int i = 0; i < T * 2; i++)
            st->gamma_marg[i] += w * st->gamma[i];
    }
}

/* -------------------------------------------------------------------------
 * signal_evidence_ratio
 * ---------------------------------------------------------------------- */
static double signal_evidence_ratio(
    itila_state_t *st,
    const double *env, int T,
    double A, double noise_mean, double sigma2_obs)
{
    /* Noise-only log likelihood */
    double log_lik_noise = 0.0;
    double lc = -0.5 * log(2.0 * M_PI * sigma2_obs);
    for (int i = 0; i < T; i++) {
        double d = env[i] - noise_mean;
        log_lik_noise += lc - 0.5 * d * d / sigma2_obs;
    }

    /* CW model: marginalized over speed (log-mean-exp of log_Z_bins) */
    decode_marginal(st, env, T, A, noise_mean, sigma2_obs);
    double lmax = st->log_Z_bins[0];
    for (int b = 1; b < N_SPEED_BINS; b++)
        if (st->log_Z_bins[b] > lmax) lmax = st->log_Z_bins[b];
    double mean_exp = 0.0;
    for (int b = 0; b < N_SPEED_BINS; b++)
        mean_exp += exp(st->log_Z_bins[b] - lmax);
    double log_lik_cw = lmax + log(mean_exp / N_SPEED_BINS);

    return log_lik_cw - log_lik_noise;
}

/* -------------------------------------------------------------------------
 * posterior_to_marks — threshold gamma[:,1] > 0.5
 * ---------------------------------------------------------------------- */
static void posterior_to_marks(const double *gamma_marg, int T, int8_t *marks) {
    for (int i = 0; i < T; i++)
        marks[i] = gamma_marg[i*2+1] > 0.5 ? 1 : 0;
}

/* -------------------------------------------------------------------------
 * fit_mark_wpm_components — M8 multi-station GMM
 * ---------------------------------------------------------------------- */
static int fit_mark_wpm_components(
    const int8_t *marks, int T,
    double wpm_em,
    double *wpm_out, int max_wpm)  /* fills wpm_out[], returns count */
{
    /* Extract mark-run durations */
    double durs[8192]; int nd = 0;
    int val = marks[0], cnt = 1;
    for (int i = 1; i < T && nd < 8192; i++) {
        if (marks[i] == val) { cnt++; }
        else {
            if (val == 1) durs[nd++] = (double)cnt;
            val = marks[i]; cnt = 1;
        }
    }
    if (val == 1 && nd < 8192) durs[nd++] = (double)cnt;

    if (nd < 20) { wpm_out[0] = wpm_em; return 1; }

    /* Log-space */
    double x[8192];
    for (int i = 0; i < nd; i++) x[i] = log(durs[i] + 0.5);

    /* K=1 fit */
    double mu1 = 0.0;
    for (int i = 0; i < nd; i++) mu1 += x[i];
    mu1 /= nd;
    double var1 = 0.0;
    for (int i = 0; i < nd; i++) { double d = x[i]-mu1; var1 += d*d; }
    var1 = var1/nd + 1e-6;
    double ll1 = -0.5 * nd * (log(2.0*M_PI*var1) + 1.0);
    double bic1 = -2.0*ll1 + 3.0*log((double)nd);

    /* Check range for potential bimodality */
    double q25 = 0.0, q75 = 0.0;
    {
        double sx[8192]; memcpy(sx, x, nd*sizeof(double));
        qsort(sx, nd, sizeof(double), cmp_double);
        int i25 = (int)(0.25*(nd-1)), i75 = (int)(0.75*(nd-1));
        q25 = sx[i25]; q75 = sx[i75];
    }
    if (q75 - q25 < 0.3) { wpm_out[0] = wpm_em; return 1; }

    /* K=2 EM */
    double mu[2] = {q25, q75};
    double vr[2] = {var1*0.5, var1*0.5};
    double pi[2] = {0.5, 0.5};
    double gamma0[8192];

    for (int iter = 0; iter < 40; iter++) {
        /* E-step */
        for (int i = 0; i < nd; i++) {
            double lr0 = log(pi[0]+1e-300) - 0.5*(x[i]-mu[0])*(x[i]-mu[0])/vr[0] - 0.5*log(2*M_PI*vr[0]);
            double lr1 = log(pi[1]+1e-300) - 0.5*(x[i]-mu[1])*(x[i]-mu[1])/vr[1] - 0.5*log(2*M_PI*vr[1]);
            double lZ  = logaddexp(lr0, lr1);
            gamma0[i]  = exp(lr0 - lZ);
        }
        /* M-step */
        double N0=1e-10, N1=1e-10, s0=0, s1=0;
        for (int i=0;i<nd;i++){N0+=gamma0[i];N1+=(1-gamma0[i]);s0+=gamma0[i]*x[i];s1+=(1-gamma0[i])*x[i];}
        pi[0]=N0/nd; pi[1]=N1/nd;
        mu[0]=s0/N0; mu[1]=s1/N1;
        double v0=1e-6,v1=1e-6;
        for(int i=0;i<nd;i++){
            double d0=x[i]-mu[0],d1=x[i]-mu[1];
            v0+=gamma0[i]*d0*d0; v1+=(1-gamma0[i])*d1*d1;
        }
        vr[0]=v0/N0; vr[1]=v1/N1;
    }

    /* K=2 log-likelihood and BIC */
    double ll2 = 0.0;
    for (int i=0;i<nd;i++) {
        double lr0 = log(pi[0]+1e-300) - 0.5*(x[i]-mu[0])*(x[i]-mu[0])/vr[0] - 0.5*log(2*M_PI*vr[0]);
        double lr1 = log(pi[1]+1e-300) - 0.5*(x[i]-mu[1])*(x[i]-mu[1])/vr[1] - 0.5*log(2*M_PI*vr[1]);
        ll2 += logaddexp(lr0, lr1);
    }
    double bic2 = -2.0*ll2 + 5.0*log((double)nd);

    if (bic2 >= bic1 - 6.0) { wpm_out[0] = wpm_em; return 1; }

    /* Sanity: WPM separation */
    double w0 = 480.0 / exp(mu[0]), w1 = 480.0 / exp(mu[1]);
    if (w0 < WPM_MIN) w0 = WPM_MIN; if (w0 > WPM_MAX) w0 = WPM_MAX;
    if (w1 < WPM_MIN) w1 = WPM_MIN; if (w1 > WPM_MAX) w1 = WPM_MAX;
    double wlo = w0 < w1 ? w0 : w1, whi = w0 > w1 ? w0 : w1;
    if (wlo / whi > 0.7) { wpm_out[0] = wpm_em; return 1; }

    /* Reject single-station dit/dah split: mean_ratio ≈ 3 */
    double mean_ratio = exp(mu[0] > mu[1] ? mu[0]-mu[1] : mu[1]-mu[0]);
    if (mean_ratio >= 2.5 && mean_ratio <= 3.5) { wpm_out[0] = wpm_em; return 1; }

    wpm_out[0] = wlo; wpm_out[1] = whi;
    return 2 < max_wpm ? 2 : max_wpm;
}

/* -------------------------------------------------------------------------
 * decode_runs_beam — M7b score-guided beam search
 * (beam_state_t typedef hoisted to top of file — see itila_state_t)
 * ---------------------------------------------------------------------- */
static int beam_score_cmp(const void *a, const void *b) {
    double sa = ((const beam_state_t*)a)->score;
    double sb = ((const beam_state_t*)b)->score;
    return (sa < sb) - (sa > sb);  /* descending */
}

/* Returns number of texts written into out_texts (each MAX_TEXT chars).
 * st provides per-handle scratch buffers (sc_runs, sc_beamA, sc_beamB,
 * sc_dedup) that used to be function-local statics. */
static int decode_runs_beam(
    itila_state_t *st,
    const int8_t *marks, int T,
    double wpm,
    char out_texts[][MAX_TEXT], int max_out)
{
    double boundary = 2.0 * unit_samples(wpm);
    double zone_lo  = boundary * 0.75;
    double zone_hi  = boundary * 1.25;
    double unit     = unit_samples(wpm);

    /* Build run list — uses st->sc_runs (per-handle, was function-local static) */
    run_t *runs = st->sc_runs;
    int n_runs = 0;
    int val = marks[0], cnt = 1;
    for (int i = 1; i < T; i++) {
        int v = marks[i];
        if (v == val) { cnt++; }
        else {
            if (n_runs < MAX_ENV) { runs[n_runs].is_mark=val; runs[n_runs].dur=cnt; n_runs++; }
            val=v; cnt=1;
        }
    }
    if (n_runs < MAX_ENV) { runs[n_runs].is_mark=val; runs[n_runs].dur=cnt; n_runs++; }

    /* Log PMF for geometric distribution */
    #define GEOM_LOG_PMF(d, mean) \
        ((d-1) * log1p(-(1.0/((mean)<1.0?1.0:(mean)))) + log(1.0/((mean)<1.0?1.0:(mean))))

    /* Beam: working array (double buffer) — per-handle scratch. */
    beam_state_t *beam = st->sc_beamA, *next = st->sc_beamB;
    int beam_sz = 1;
    beam[0].score = 0.0; beam[0].sym[0] = '\0'; beam[0].txt[0] = '\0';

    for (int r = 0; r < n_runs; r++) {
        int is_mark = runs[r].is_mark;
        int dur     = runs[r].dur;

        if (is_mark) {
            if ((double)dur >= zone_lo && (double)dur <= zone_hi) {
                /* Boundary zone: expand both dit and dah */
                double log_dit = GEOM_LOG_PMF(dur, unit);
                double log_dah = GEOM_LOG_PMF(dur, 3.0*unit);
                int next_sz = 0;

                for (int i = 0; i < beam_sz && next_sz < MAX_BEAM - 1; i++) {
                    /* dit hypothesis */
                    beam_state_t *s0 = &next[next_sz++];
                    *s0 = beam[i];
                    int sl = strlen(s0->sym);
                    if (sl < MAX_SYM-1) { s0->sym[sl]='.'; s0->sym[sl+1]='\0'; }
                    s0->score += log_dit;
                    /* dah hypothesis */
                    if (next_sz < MAX_BEAM) {
                        beam_state_t *s1 = &next[next_sz++];
                        *s1 = beam[i];
                        sl = strlen(s1->sym);
                        if (sl < MAX_SYM-1) { s1->sym[sl]='-'; s1->sym[sl+1]='\0'; }
                        s1->score += log_dah;
                    }
                }
                /* Sort by score descending, keep top MAX_TEXTS */
                qsort(next, next_sz, sizeof(beam_state_t), beam_score_cmp);
                if (next_sz > MAX_TEXTS) next_sz = MAX_TEXTS;
                /* Deduplicate by (sym,txt) keeping highest score (already sorted) */
                int dedup_sz = 0;
                beam_state_t *dedup_buf = st->sc_dedup;  /* per-handle scratch */
                for (int i = 0; i < next_sz; i++) {
                    int dup = 0;
                    for (int j = 0; j < dedup_sz; j++)
                        if (strcmp(next[i].sym, dedup_buf[j].sym)==0 &&
                            strcmp(next[i].txt, dedup_buf[j].txt)==0) { dup=1; break; }
                    if (!dup && dedup_sz < MAX_BEAM) dedup_buf[dedup_sz++] = next[i];
                }
                beam_sz = dedup_sz;
                beam_state_t *tmp = beam; beam = next; next = tmp;
                memcpy(beam, dedup_buf, beam_sz * sizeof(beam_state_t));
            } else {
                /* Clear dit or dah */
                char elem = (double)dur < boundary ? '.' : '-';
                for (int i = 0; i < beam_sz; i++) {
                    int sl = strlen(beam[i].sym);
                    if (sl < MAX_SYM-1) { beam[i].sym[sl]=elem; beam[i].sym[sl+1]='\0'; }
                }
            }
        } else {
            /* Space */
            if ((double)dur < 2.0*unit) {
                /* element space — no change */
            } else if ((double)dur < 5.0*unit) {
                /* letter space: emit symbol */
                for (int i = 0; i < beam_sz; i++) {
                    if (beam[i].sym[0]) {
                        char ch = morse_lookup(beam[i].sym);
                        int tl = strlen(beam[i].txt);
                        if (tl < MAX_TEXT-1) { beam[i].txt[tl]=ch; beam[i].txt[tl+1]='\0'; }
                        beam[i].sym[0] = '\0';
                    }
                }
            } else {
                /* word space */
                for (int i = 0; i < beam_sz; i++) {
                    int tl = strlen(beam[i].txt);
                    if (beam[i].sym[0]) {
                        char ch = morse_lookup(beam[i].sym);
                        if (tl < MAX_TEXT-1) { beam[i].txt[tl]=ch; tl++; beam[i].txt[tl]='\0'; }
                        beam[i].sym[0] = '\0';
                    }
                    if (tl < MAX_TEXT-1) { beam[i].txt[tl]=' '; beam[i].txt[tl+1]='\0'; }
                }
            }
        }
    }

    /* Flush remaining symbols */
    for (int i = 0; i < beam_sz; i++) {
        if (beam[i].sym[0]) {
            char ch = morse_lookup(beam[i].sym);
            int tl = strlen(beam[i].txt);
            if (tl < MAX_TEXT-1) { beam[i].txt[tl]=ch; beam[i].txt[tl+1]='\0'; }
            beam[i].sym[0] = '\0';
        }
    }

    /* Deduplicate final texts */
    int n_out = 0;
    for (int i = 0; i < beam_sz && n_out < max_out; i++) {
        int dup = 0;
        for (int j = 0; j < n_out; j++)
            if (strcmp(beam[i].txt, out_texts[j])==0) { dup=1; break; }
        if (!dup) strncpy(out_texts[n_out++], beam[i].txt, MAX_TEXT-1);
    }
    return n_out ? n_out : 0;
    #undef GEOM_LOG_PMF
}

/* -------------------------------------------------------------------------
 * Callsign extraction — 3-pass (match Python exactly)
 * ---------------------------------------------------------------------- */
static int is_alnum_upper(char c) {
    return (c>='A'&&c<='Z') || (c>='0'&&c<='9');
}

/* Check if string matches callsign pattern: [A-Z0-9]{1,2}[0-9][A-Z0-9]{1,4} */
static int is_callsign(const char *s) {
    int n = (int)strlen(s);
    if (n < 3 || n > 7) return 0;
    for (int i = 0; i < n; i++) if (!is_alnum_upper(s[i])) return 0;
    /* Digit must be at position 1 or 2 */
    for (int dig = 1; dig <= 2 && dig < n-1; dig++) {
        if (!isdigit((unsigned char)s[dig])) continue;
        /* All chars before digit: alphanumeric (already checked above) */
        /* All chars after digit: alphanumeric (already checked above) */
        return 1;
    }
    return 0;
}

/* callsign_t typedef hoisted to top of file */

static int add_callsign(const char *tok, callsign_t *calls, int *n_calls) {
    if (!is_callsign(tok)) return 0;
    for (int i = 0; i < *n_calls; i++) {
        if (strcmp(calls[i].call, tok)==0) { calls[i].count++; return 1; }
    }
    if (*n_calls >= MAX_CALLS) return 0;
    strncpy(calls[*n_calls].call, tok, MAX_CALL-1);
    calls[*n_calls].call[MAX_CALL-1] = '\0';
    calls[*n_calls].count = 1;
    (*n_calls)++;
    return 1;
}

static int extract_callsigns(
    itila_state_t *st,
    const char *text,
    callsign_t *calls, int *n_calls)
{
    /* Uppercase copy — per-handle scratch (was function-local static). */
    char *up = st->sc_up;
    int tlen = (int)strlen(text);
    if (tlen >= MAX_TEXT) tlen = MAX_TEXT-1;
    for (int i = 0; i < tlen; i++) up[i] = toupper((unsigned char)text[i]);
    up[tlen] = '\0';

    /* Tokenize into alphanumeric runs — per-handle scratch. */
    char (*tokens)[MAX_CALL+4] = st->sc_tokens;
    int tok_lens[256];
    int n_toks = 0;
    int i = 0;
    while (i < tlen && n_toks < 256) {
        if (is_alnum_upper(up[i])) {
            int j = i;
            while (j < tlen && is_alnum_upper(up[j])) j++;
            int tl = j - i;
            if (tl > 10) tl = 10;
            memcpy(tokens[n_toks], up+i, tl);
            tokens[n_toks][tl] = '\0';
            tok_lens[n_toks] = j - i;  /* actual length before truncation */
            n_toks++;
            i = j;
        } else i++;
    }

    /* Pass 1: individual tokens of length 3-7 */
    for (int t = 0; t < n_toks; t++) {
        int tl = (int)strlen(tokens[t]);
        if (tl >= 3 && tl <= 7) add_callsign(tokens[t], calls, n_calls);
    }

    /* Pass 2: adjacent-pair merge */
    for (int t = 0; t < n_toks-1; t++) {
        char merged[MAX_CALL+4];
        int l1 = (int)strlen(tokens[t]), l2 = (int)strlen(tokens[t+1]);
        int ml = l1 + l2;
        if (ml < 3 || ml > 7) continue;
        memcpy(merged, tokens[t], l1);
        memcpy(merged+l1, tokens[t+1], l2+1);
        add_callsign(merged, calls, n_calls);
    }

    /* Pass 3: substring scan of long tokens (>7 chars) with digit */
    for (int t = 0; t < n_toks; t++) {
        int tl = tok_lens[t];  /* use original length */
        if (tl <= 7) continue;
        /* Need the original longer token — re-extract from up[] */
        /* Find the t-th token position in up[] */
        const char *tp = NULL;
        int found = 0; int ti = 0;
        for (int ci = 0; ci < tlen && !found; ) {
            if (is_alnum_upper(up[ci])) {
                if (ti == t) { tp = up+ci; found=1; break; }
                ti++;
                while (ci < tlen && is_alnum_upper(up[ci])) ci++;
            } else ci++;
        }
        if (!found || !tp) continue;
        /* Check for digit */
        int has_digit = 0;
        for (int ci = 0; ci < tl; ci++) if (isdigit((unsigned char)tp[ci])) { has_digit=1; break; }
        if (!has_digit) continue;
        /* Scan substrings */
        for (int start = 0; start < tl-2; start++) {
            for (int len = 3; len <= 7 && start+len <= tl; len++) {
                char sub[8]; memcpy(sub, tp+start, len); sub[len]='\0';
                for (int ci=0;ci<len;ci++) sub[ci]=toupper((unsigned char)sub[ci]);
                add_callsign(sub, calls, n_calls);
            }
        }
    }
    return *n_calls;
}

/* -------------------------------------------------------------------------
 * Public API
 * ---------------------------------------------------------------------- */
itila_t itila_create(int sample_rate, double lpf_hz) {
    itila_state_t *st = (itila_state_t*)calloc(1, sizeof(itila_state_t));
    if (!st) return NULL;

    st->sample_rate = sample_rate;
    st->lpf_hz      = lpf_hz;

    st->log_B      = (double*)malloc(MAX_ENV * 2 * sizeof(double));
    st->log_alpha  = (double*)malloc(MAX_ENV * 2 * sizeof(double));
    st->log_beta   = (double*)malloc(MAX_ENV * 2 * sizeof(double));
    st->gamma      = (double*)malloc(MAX_ENV * 2 * sizeof(double));
    st->gamma_marg = (double*)malloc(MAX_ENV * 2 * sizeof(double));
    st->env_norm   = (double*)malloc(MAX_ENV     * sizeof(double));
    st->marks      = (int8_t*)malloc(MAX_ENV     * sizeof(int8_t));

    /* Per-handle scratch — replaces function-local statics for thread safety. */
    st->sc_runs      = (run_t*)       malloc(MAX_ENV * sizeof(run_t));
    st->sc_beamA     = (beam_state_t*)malloc(MAX_BEAM * sizeof(beam_state_t));
    st->sc_beamB     = (beam_state_t*)malloc(MAX_BEAM * sizeof(beam_state_t));
    st->sc_dedup     = (beam_state_t*)malloc(MAX_BEAM * sizeof(beam_state_t));
    st->sc_up        = (char*)        malloc(MAX_TEXT * sizeof(char));
    st->sc_tokens    = malloc(256 * (MAX_CALL + 4));
    st->sc_calls     = (callsign_t*)  malloc(MAX_CALLS * sizeof(callsign_t));
    st->sc_out_texts = malloc(MAX_TEXTS * MAX_TEXT);
    st->sc_primary   = (char*)        malloc(MAX_TEXT * sizeof(char));

    if (!st->log_B || !st->log_alpha || !st->log_beta ||
        !st->gamma || !st->gamma_marg || !st->env_norm || !st->marks ||
        !st->sc_runs || !st->sc_beamA || !st->sc_beamB || !st->sc_dedup ||
        !st->sc_up || !st->sc_tokens || !st->sc_calls ||
        !st->sc_out_texts || !st->sc_primary) {
        itila_free(st); return NULL;
    }

    /* Speed bins: linspace(WPM_MIN, WPM_MAX, N_SPEED_BINS) */
    for (int i = 0; i < N_SPEED_BINS; i++)
        st->speed_bins[i] = WPM_MIN + (WPM_MAX - WPM_MIN) * i / (N_SPEED_BINS - 1);

    return (itila_t)st;
}

const char* itila_feed(itila_t h, const double* envelope, int n,
                       double freq_khz, double ev_thresh)
{
    itila_state_t *st = (itila_state_t*)h;
    st->result_buf[0] = '\0';

    if (n < st->sample_rate || n > MAX_ENV) return st->result_buf;

    /* EM estimation — warm-start from previous call if available */
    double A, noise_mean, sigma2_obs, wpm_em;
    em_estimate(st, envelope, n, st->ws_wpm, &A, &noise_mean, &sigma2_obs, &wpm_em);

    /* Evidence ratio — calls decode_marginal internally */
    double log_bf = signal_evidence_ratio(st, envelope, n, A, noise_mean, sigma2_obs);
    if (log_bf < ev_thresh) return st->result_buf;

    /* gamma_marg is already filled by signal_evidence_ratio → decode_marginal */
    posterior_to_marks(st->gamma_marg, n, st->marks);

    /* M8: multi-station WPM detection */
    double wpm_cands[2]; int n_cands;
    n_cands = fit_mark_wpm_components(st->marks, n, wpm_em, wpm_cands, 2);

    /* Per-handle scratch — was function-local static; live mode races. */
    callsign_t *calls = st->sc_calls;
    int n_calls = 0;
    char (*out_texts)[MAX_TEXT] = st->sc_out_texts;
    char *primary_text = st->sc_primary;
    primary_text[0] = '\0';

    for (int ci = 0; ci < n_cands; ci++) {
        int n_texts = decode_runs_beam(st, st->marks, n, wpm_cands[ci],
                                       out_texts, MAX_TEXTS);
        if (ci == 0 && n_texts > 0)
            strncpy(primary_text, out_texts[0], MAX_TEXT-1);
        for (int ti = 0; ti < n_texts; ti++)
            extract_callsigns(st, out_texts[ti], calls, &n_calls);
    }

    if (n_calls == 0) return st->result_buf;

    st->last_wpm = wpm_cands[0];

    if (primary_text[0]) {
        strncpy(st->result_buf, primary_text, RESULT_BUF-1);
        st->result_buf[RESULT_BUF-1] = '\0';
        return st->result_buf;
    }

    int best = 0;
    for (int i = 1; i < n_calls; i++)
        if (calls[i].count > calls[best].count) best = i;
    int l = (int)strlen(calls[best].call);
    memcpy(st->result_buf, calls[best].call, l);
    st->result_buf[l]   = '\n';
    st->result_buf[l+1] = '\0';
    return st->result_buf;
}

/* -------------------------------------------------------------------------
 * itila_feed_online — online EM with λ-forgetting sufficient statistics
 * ---------------------------------------------------------------------- */
const char* itila_feed_online(itila_t h, const double *envelope, int n,
                              double lambda, double freq_khz, double ev_thresh)
{
    itila_state_t *st = (itila_state_t*)h;
    st->result_buf[0] = '\0';
    if (n < 10 || n > MAX_ENV) return st->result_buf;

    /* Normalize to [0,1] via p99 — keeps FB numerics stable */
    double env_scale = percentile(envelope, n, 99.0, st->env_norm);
    if (env_scale < 1e-30) return st->result_buf;
    double *env = st->env_norm;
    for (int i = 0; i < n; i++) env[i] = envelope[i] / env_scale;

    /* Current params in normalized space */
    double nm, A, s2, wpm;
    if (!st->oss_init) {
        /* Cold-start: percentile heuristics (same as em_estimate cold path) */
        nm = percentile(env, n, 30.0, st->gamma);
        double p95 = percentile(env, n, 95.0, st->gamma);
        double p5  = percentile(env, n, 5.0,  st->gamma);
        double p40 = percentile(env, n, 40.0, st->gamma);
        double var_lo = 0.0; int nlo = 0;
        for (int i = 0; i < n; i++)
            if (env[i] < p40) { var_lo += (env[i]-nm)*(env[i]-nm); nlo++; }
        if (nlo > 2) var_lo /= nlo; else var_lo = 0.0;
        double env_spread = p95 - p5;
        s2 = var_lo;
        double floor1 = (env_spread * 0.15) * (env_spread * 0.15);
        if (s2 < floor1) s2 = floor1;
        if (s2 < 1e-6) s2 = 1e-6;
        double thresh_init = nm + 3.0 * sqrt(s2);
        A = 0.0; int nhi = 0;
        for (int i = 0; i < n; i++) if (env[i] > thresh_init) { A += env[i]; nhi++; }
        if (nhi > 10) A /= nhi; else A = p95;
        wpm = 25.0;
        /* Store in raw units */
        st->oss_nm  = nm  * env_scale;
        st->oss_A   = A   * env_scale;
        st->oss_s2  = s2  * env_scale * env_scale;
        st->oss_wpm = 25.0;
        st->oss_N0 = st->oss_S0 = st->oss_Q0 = 0.0;
        st->oss_N1 = st->oss_S1 = st->oss_Q1 = 0.0;
        st->oss_init = 1;
    } else {
        /* Convert stored raw params to normalized space */
        nm  = st->oss_nm / env_scale;
        A   = st->oss_A  / env_scale;
        s2  = st->oss_s2 / (env_scale * env_scale);
        wpm = st->oss_wpm > 0.0 ? st->oss_wpm : 25.0;
        if (nm  < 0)         nm  = 0.0;
        if (A   < nm + 1e-10) A  = nm + 1e-10;
        if (s2  < 1e-20)     s2  = 1e-20;
    }

    /* E-step: one forward-backward pass with current params */
    forward_backward_fast(st, env, n, wpm, A, nm, s2, st->gamma);

    /* Sufficient stat contributions from this call (raw units) */
    double dN0=0, dS0=0, dQ0=0, dN1=0, dS1=0, dQ1=0;
    for (int i = 0; i < n; i++) {
        double g0 = st->gamma[i*2+0], g1 = st->gamma[i*2+1];
        double x  = envelope[i];   /* raw */
        dN0 += g0; dS0 += g0 * x; dQ0 += g0 * x * x;
        dN1 += g1; dS1 += g1 * x; dQ1 += g1 * x * x;
    }

    /* Update sufficient stats with λ forgetting */
    st->oss_N0 = lambda * st->oss_N0 + dN0;
    st->oss_S0 = lambda * st->oss_S0 + dS0;
    st->oss_Q0 = lambda * st->oss_Q0 + dQ0;
    st->oss_N1 = lambda * st->oss_N1 + dN1;
    st->oss_S1 = lambda * st->oss_S1 + dS1;
    st->oss_Q1 = lambda * st->oss_Q1 + dQ1;

    /* M-step: update params from accumulated sufficient stats */
    if (st->oss_N0 > 1.0 && st->oss_N1 > 1.0) {
        double nm_new = st->oss_S0 / st->oss_N0;
        double A_new  = st->oss_S1 / st->oss_N1;
        if (A_new < nm_new + 1e-30) A_new = nm_new + 1e-30;
        /* Variance from E[x^2] - mean^2 */
        double var0 = st->oss_Q0 / st->oss_N0 - nm_new * nm_new;
        double var1 = st->oss_Q1 / st->oss_N1 - A_new  * A_new;
        if (var0 < 0.0) var0 = 0.0;
        if (var1 < 0.0) var1 = 0.0;
        double s2_new = (var0 + var1) / 2.0;
        if (s2_new < 1e-30) s2_new = 1e-30;
        st->oss_nm = nm_new;
        st->oss_A  = A_new;
        st->oss_s2 = s2_new;
    }

    /* WPM: estimate from E-step gamma, EMA update (α=0.3: fast adaptation) */
    double wpm_est = estimate_wpm_from_gamma(st->gamma, n);
    if (wpm_est > 0.0) {
        if (st->oss_wpm <= 0.0) st->oss_wpm = wpm_est;
        else                    st->oss_wpm = 0.7 * st->oss_wpm + 0.3 * wpm_est;
    }

    /* Re-normalized params for evidence check and decode */
    double nm2  = st->oss_nm / env_scale;
    double A2   = st->oss_A  / env_scale;
    double s2_2 = st->oss_s2 / (env_scale * env_scale);
    double wpm2 = st->oss_wpm > 0.0 ? st->oss_wpm : 25.0;
    if (nm2 < 0.0)           nm2  = 0.0;
    if (A2  < nm2 + 1e-10)   A2   = nm2 + 1e-10;
    if (s2_2 < 1e-20)        s2_2 = 1e-20;

    /* Quick evidence check: one FB pass vs noise-only log-likelihood.
     * Avoids the 32-FB-pass decode_marginal unless evidence is sufficient. */
    double log_Z_cw = forward_backward_fast(st, env, n, wpm2, A2, nm2, s2_2,
                                             st->gamma);
    double log_lik_noise = 0.0;
    {
        double lc = -0.5 * log(2.0 * M_PI * s2_2);
        for (int i = 0; i < n; i++) {
            double d = env[i] - nm2;
            log_lik_noise += lc - 0.5 * d * d / s2_2;
        }
    }
    double log_bf = log_Z_cw - log_lik_noise;
    if (log_bf < ev_thresh) return st->result_buf;

    /* Evidence sufficient — run full decode_marginal for accurate marks */
    decode_marginal(st, env, n, A2, nm2, s2_2);
    posterior_to_marks(st->gamma_marg, n, st->marks);

    double wpm_cands[2]; int n_cands;
    n_cands = fit_mark_wpm_components(st->marks, n, wpm2, wpm_cands, 2);

    /* Per-handle scratch — was function-local static; live mode races.
     * itila_feed and itila_feed_online both call decode_runs_beam +
     * extract_callsigns; sharing the same per-handle scratch is fine
     * (a single handle is never used concurrently).  */
    callsign_t *calls_ol = st->sc_calls;
    int n_calls = 0;
    char (*out_texts_ol)[MAX_TEXT] = st->sc_out_texts;
    char *primary_ol = st->sc_primary;
    primary_ol[0] = '\0';

    for (int ci = 0; ci < n_cands; ci++) {
        int n_texts = decode_runs_beam(st, st->marks, n, wpm_cands[ci],
                                       out_texts_ol, MAX_TEXTS);
        if (ci == 0 && n_texts > 0)
            strncpy(primary_ol, out_texts_ol[0], MAX_TEXT - 1);
        for (int ti = 0; ti < n_texts; ti++)
            extract_callsigns(st, out_texts_ol[ti], calls_ol, &n_calls);
    }

    if (n_calls == 0) return st->result_buf;

    if (primary_ol[0]) {
        strncpy(st->result_buf, primary_ol, RESULT_BUF - 1);
        st->result_buf[RESULT_BUF - 1] = '\0';
        return st->result_buf;
    }

    int best = 0;
    for (int i = 1; i < n_calls; i++)
        if (calls_ol[i].count > calls_ol[best].count) best = i;
    int l = (int)strlen(calls_ol[best].call);
    memcpy(st->result_buf, calls_ol[best].call, l);
    st->result_buf[l]   = '\n';
    st->result_buf[l+1] = '\0';
    return st->result_buf;
}

void itila_reset_online(itila_t h)
{
    itila_state_t *st = (itila_state_t*)h;
    st->oss_N0 = st->oss_S0 = st->oss_Q0 = 0.0;
    st->oss_N1 = st->oss_S1 = st->oss_Q1 = 0.0;
    st->oss_nm = st->oss_A  = st->oss_s2 = 0.0;
    st->oss_wpm  = 0.0;
    st->oss_init = 0;
}

void itila_free(itila_t h) {
    if (!h) return;
    itila_state_t *st = (itila_state_t*)h;
    free(st->log_B); free(st->log_alpha); free(st->log_beta);
    free(st->gamma); free(st->gamma_marg); free(st->env_norm); free(st->marks);
    free(st->sc_runs);
    free(st->sc_beamA); free(st->sc_beamB); free(st->sc_dedup);
    free(st->sc_up); free(st->sc_tokens);
    free(st->sc_calls); free(st->sc_out_texts); free(st->sc_primary);
    free(st);
}

double itila_get_wpm(itila_t h) {
    if (!h) return 0.0;
    return ((itila_state_t*)h)->last_wpm;
}

/* Debug: expose EM estimate for testing */
void itila_debug_em(itila_t h, const double* envelope, int n,
                    double *A_out, double *nm_out, double *s2_out, double *wpm_out) {
    itila_state_t *st = (itila_state_t*)h;
    em_estimate(st, envelope, n, 0.0, A_out, nm_out, s2_out, wpm_out);
}
