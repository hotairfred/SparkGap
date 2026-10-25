/*
 * itila2_scanner.c: itila_scanner.c plus WX7V's scanner changes (selected with cw_decoder: "itila2").
 * Same API and symbols as itila_scanner.c; built as libitila2_scanner.so. Changes:
 *   - 40 ms Hann window on the 100 Hz path I/Q at 2 kHz, before the magnitude
 *   - a new bin is fed the last 1 s of IQ (pre-roll) so the first characters are not lost
 *   - one bin per signal: spawn spacing 75 Hz on interpolated frequencies, bin at the mean of its candidate's peaks
 *   - per-bin DSP split over itila_decode_threads workers (itila_sc_set_threads)
 *   - fir_dot without the per-tap modulo
 *   - stage 1 (mix + FIR to 12 kHz) as one shared FFT per block (overlap-save), a small IFFT per bin;
 *     ITILA_SC_FIR1=1 in the environment restores the per-bin mix + FIR1 (for A/B)
 * Bin keep-alive is the same as itila_scanner.c: spot evidence only (word keep-alive kept noise bins alive, removed 2026-10-06).
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
 *       -o libitila2_scanner.so itila2_scanner.c -lm
 */

#include "itila_scanner.h"
#include <stdio.h>
#include <math.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

/* ---- compile-time limits ---- */
#define SC_MAX_BINS   512
#define SC_DEC1       16      /* max stage-1 decimation: 192 kHz → 12 kHz (8 at 96k, 4 at 48k) */
#define SC_DEC2       6       /* 12 kHz → 2 kHz    (FIR stage 2) */
#define SC_DEC3       10      /* 2 kHz → 200 Hz    (FIR stage 3) */
#define SC_ENV_CAP    15000   /* 75s at 200 Hz - 1.25 decode windows */
/* itila2_snr_decoder: noise bandwidths of the two envelope paths, from their taps (FIR_S2_200 at
 * 12 kHz then WIDE_TAPS at 2 kHz; FIR_S2_100 then the 40 ms Hann), recomputed in tests/test_itila2_snr.py */
#define SC_ENBW_200_HZ   197.1
#define SC_ENBW_100_HZ    36.5
#define SC_SNR_REF_HZ    500.0   /* SNR is quoted in 500 Hz, as CW Skimmer does */
#define SC_NOISE_PCT     25      /* noise density from this percentile of the band's FFT bins */
#define SC_NOISE_TAU_S   20.0    /* ...smoothed over this long */

/* Lazy bin spawn: a peak must be detected by ≥SC_CAND_HITS_REQUIRED scans
 * before a bin is allocated for it. Filters single-scan noise crossings
 * that otherwise burn per-RX worker decode cycles on garbage. Candidates
 * within SC_CAND_CLUSTER_HZ of an existing one are merged. Stale ones
 * (last_seen older than SC_CAND_EXPIRY_SCANS scans) are recycled. At
 * 4096-sample scans / 192k SPS = ~21 ms cadence, 3 hits ≈ 63 ms latency
 * to spawn - negligible vs CW callsign duration. */
#define SC_MAX_CANDIDATES      512
#define SC_CAND_HITS_REQUIRED  3
#define SC_CAND_EXPIRY_SCANS   10
#define SC_CAND_CLUSTER_HZ     150.0
#define SC_CAND_AVG_HZ         25.0    /* hits averaged into the new bin's frequency */

#include "itila_fir_coeffs.h"
/* Hot loops built twice, AVX2/FMA and baseline, chosen at load time: portable builds keep
 * running on older CPUs and get the native speed on newer ones (cdub89/SparkGap#19 A4). */
#if defined(__GNUC__) && !defined(__clang__) && defined(__x86_64__)
#define HOT_CLONES __attribute__((target_clones("arch=x86-64-v3", "default")))
#else
#define HOT_CLONES
#endif

#define SC_HANN_MS     40.0  /* Hann window on the 100 Hz path, at 2 kHz */
#define SC_HANN_LEN    80    /* SC_HANN_MS x 2 taps */
/* Channel filter on the 200 Hz path at 2 kHz: firwin(93, 110, window=('kaiser', 5.65326), fs=2000).
 * Flat to +-75 Hz, -3 dB at +-100 Hz, -60 dB by +-150 Hz, so neighbours the 200 Hz stage-2 FIR passes are cut. */
#define SC_WIDE_LEN 93
static const double WIDE_TAPS[SC_WIDE_LEN] = {
    -2.642351154647633e-05, 3.098960963239555e-05, 1.281421043742315e-04, 2.594227398493466e-04,
    4.080655646582045e-04, 5.461286314294562e-04, 6.369604587657394e-04, 6.402932382826449e-04,
    5.195909279798357e-04, 2.507355662392715e-04, -1.693426246577815e-04, -7.157910271436799e-04,
    -1.332458233331290e-03, -1.933480514483820e-03, -2.410694161648160e-03, -2.646862085936297e-03,
    -2.533666092035861e-03, -1.992373188482555e-03, -9.942162026149192e-04, 4.229867858911401e-04,
    2.145470597928983e-03, 3.984362533841939e-03, 5.688547732248054e-03, 6.969799342689818e-03,
    7.538551418113782e-03, 7.146813461549591e-03, 5.633120258669281e-03, 2.963360758564164e-03,
    -7.389385147209261e-04, -5.178657316523528e-03, -9.895677551561193e-03, -1.429391917436351e-02,
    -1.769185150052840e-02, -1.939051011648550e-02, -1.875230757381975e-02, -1.528185696095847e-02,
    -8.698973619144608e-03, 1.005798167885363e-03, 1.354131147702408e-02, 2.831712631179540e-02,
    4.447800478857074e-02, 6.096961561725402e-02, 7.662955317128239e-02, 9.029444149769780e-02,
    1.009116036435528e-01, 1.076428647172937e-01, 1.099486776938451e-01, 1.076428647172937e-01,
    1.009116036435528e-01, 9.029444149769780e-02, 7.662955317128239e-02, 6.096961561725402e-02,
    4.447800478857074e-02, 2.831712631179540e-02, 1.354131147702408e-02, 1.005798167885363e-03,
    -8.698973619144608e-03, -1.528185696095847e-02, -1.875230757381975e-02, -1.939051011648550e-02,
    -1.769185150052840e-02, -1.429391917436351e-02, -9.895677551561193e-03, -5.178657316523528e-03,
    -7.389385147209261e-04, 2.963360758564164e-03, 5.633120258669281e-03, 7.146813461549591e-03,
    7.538551418113782e-03, 6.969799342689818e-03, 5.688547732248054e-03, 3.984362533841939e-03,
    2.145470597928983e-03, 4.229867858911401e-04, -9.942162026149192e-04, -1.992373188482555e-03,
    -2.533666092035861e-03, -2.646862085936297e-03, -2.410694161648160e-03, -1.933480514483820e-03,
    -1.332458233331290e-03, -7.157910271436799e-04, -1.693426246577815e-04, 2.507355662392715e-04,
    5.195909279798357e-04, 6.402932382826449e-04, 6.369604587657394e-04, 5.461286314294562e-04,
    4.080655646582045e-04, 2.594227398493466e-04, 1.281421043742315e-04, 3.098960963239555e-05,
    -2.642351154647633e-05
};
#define SC_PREROLL_SEC 1.0   /* IQ replayed into a new bin */
#define SC_MAX_EXCL    64    /* exclusion zones (RTTY tones) at once */
#define SC_MAX_THREADS 16    /* itila_sc_set_threads cap */

/* Stage 1 to 12 kHz: firwin(N, fc, window=('kaiser', beta), fs=rate), as in hotairfred/SparkGap#8.
 * 192k: 32 taps, fc 5000, beta 3.0; 96k: 17 taps, fc 5000, beta 3.0; 48k: 9 taps, fc 4000, beta 2.5.
 * 40 dB or more down at every fold band (k x 12 kHz +-300 Hz), < 0.03 dB droop at 300 Hz. */
#define FIR1W_192K_LEN 32
static const double FIR1W_192K[32] = {
    2.722306640689562e-03, 4.627612510154580e-03, 7.153278408679127e-03, 1.031823517842259e-02,
    1.410480446587604e-02, 1.845556456377926e-02, 2.327290075508940e-02, 2.842146224904661e-02,
    3.373351832399613e-02, 3.901696734361195e-02, 4.406552492746963e-02, 4.867042232044303e-02,
    5.263280110495639e-02, 5.577590986242678e-02, 5.795620068993096e-02, 5.907249065542795e-02,
    5.907249065542795e-02, 5.795620068993096e-02, 5.577590986242678e-02, 5.263280110495639e-02,
    4.867042232044303e-02, 4.406552492746963e-02, 3.901696734361195e-02, 3.373351832399613e-02,
    2.842146224904661e-02, 2.327290075508940e-02, 1.845556456377926e-02, 1.410480446587604e-02,
    1.031823517842259e-02, 7.153278408679127e-03, 4.627612510154580e-03, 2.722306640689562e-03
};
#define FIR1W_96K_LEN 17
static const double FIR1W_96K[17] = {
    4.548930590877632e-03, 1.251609591700054e-02, 2.526995175929027e-02, 4.240186942651408e-02,
    6.235816889092164e-02, 8.261251447677263e-02, 1.001325448708302e-01, 1.120342116062826e-01,
    1.162514249230209e-01, 1.120342116062826e-01, 1.001325448708302e-01, 8.261251447677263e-02,
    6.235816889092164e-02, 4.240186942651408e-02, 2.526995175929027e-02, 1.251609591700054e-02,
    4.548930590877632e-03
};
#define FIR1W_48K_LEN 9
static const double FIR1W_48K[9] = {
    2.494596132197499e-02, 6.950412953992999e-02, 1.278896623047664e-01, 1.784234141254429e-01,
    1.984736654157714e-01, 1.784234141254429e-01, 1.278896623047664e-01, 6.950412953992999e-02,
    2.494596132197499e-02
};
#define SC_FIR1_MAX FIR1W_192K_LEN

/* ---- FFT plan: precomputed bit reversal and twiddles, power of 2, forward ---- */
typedef struct { int n; int *rev; double *wr, *wi; } FftPlan;

static int fft_plan_init(FftPlan *p, int n)
{
    p->n = n;
    p->rev = (int *)malloc(n * sizeof(int));
    p->wr = (double *)malloc(n / 2 * sizeof(double));
    p->wi = (double *)malloc(n / 2 * sizeof(double));
    if (!p->rev || !p->wr || !p->wi) return 0;
    int bits = 0;
    while ((1 << bits) < n) bits++;
    for (int i = 0; i < n; i++) {
        int r = 0;
        for (int b = 0; b < bits; b++) if (i & (1 << b)) r |= 1 << (bits - 1 - b);
        p->rev[i] = r;
    }
    for (int k = 0; k < n / 2; k++) { p->wr[k] = cos(-2.0 * M_PI * k / n); p->wi[k] = sin(-2.0 * M_PI * k / n); }
    return 1;
}

static void fft_plan_free(FftPlan *p)
{
    free(p->rev); free(p->wr); free(p->wi);
    p->rev = NULL; p->wr = p->wi = NULL;
}

HOT_CLONES static void fft_exec(const FftPlan *p, double *re, double *im)
{
    int n = p->n;
    for (int i = 0; i < n; i++) {
        int j = p->rev[i];
        if (i < j) {
            double t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (int len = 2; len <= n; len <<= 1) {
        int half = len >> 1, step = n / len;
        for (int i = 0; i < n; i += len) {
            for (int k = 0; k < half; k++) {
                double cr = p->wr[k * step], ci = p->wi[k * step];
                double vr = re[i+k+half], vi = im[i+k+half];
                double tr = vr*cr - vi*ci, ti = vr*ci + vi*cr;
                double ur = re[i+k], ui = im[i+k];
                re[i+k]      = ur + tr; im[i+k]      = ui + ti;
                re[i+k+half] = ur - tr; im[i+k+half] = ui - ti;
            }
        }
    }
}

/* ---- per-bin state ---- */
typedef struct {
    double f_hz;
    int    active;
    double c_phase, s_phase;           /* oscillator state */

    /* FIR stage 1: to 12k (up to SC_FIR1_MAX taps, complex I/Q) */
    double dl1_i[2*SC_FIR1_MAX];
    double dl1_q[2*SC_FIR1_MAX];
    int    dl1_count;                  /* samples fed since last output */

    /* FIR stage 2: 12k→2k (96 taps, complex I/Q, two paths) */
    double dl2_100i[2*FIR_S2_100_LEN];
    double dl2_100q[2*FIR_S2_100_LEN];
    double dl2_200i[2*FIR_S2_200_LEN];
    double dl2_200q[2*FIR_S2_200_LEN];
    int    dl2_count;

    /* Hann window on the 100 Hz path (2 kHz, complex I/Q) */
    double dlh_i[2*SC_HANN_LEN];
    double dlh_q[2*SC_HANN_LEN];
    double dlw_i[2*SC_WIDE_LEN];         /* channel filter on the 200 Hz path */
    double dlw_q[2*SC_WIDE_LEN];
    int    dlw_count;
    int    dlh_count;
    int    need_preroll;               /* feed the pre-roll IQ on the next process_bins */
    int64_t start_in;                  /* FFT front end: first input sample this bin is fed (pre-roll start) */

    /* FIR stage 3: 2k→200Hz (60 taps, real envelope, two paths) */
    double dl3_100[2*FIR_STAGE3_LEN];
    double dl3_200[2*FIR_STAGE3_LEN];
    int    dl3_count;

    double env100[SC_ENV_CAP];
    double env200[SC_ENV_CAP];
    int    env_n;
    int64_t created_sample;           /* 12 kHz units; int64 so long runs never wrap */
    int64_t last_evidence;
    double snr_db;

    /* ITILA decoder handles - created lazily, one per LPF path */
    void  *h100;
    void  *h200;

    /* Set non-zero while itila_sc_decode_ready is inside dec_feed for this
     * bin with the scanner lock released.  Eviction sites (in feed_iq path)
     * check this and skip the bin if it's mid-decode -- otherwise
     * bin_free_decoders would free h100/h200 while the decode thread is
     * using them.  Cleared when decode_ready re-acquires the lock and is
     * done with the bin. */
    int    in_decode;
} ScBin;

/* ---- scanner ---- */
struct ItilaSc {
    int    sample_rate;
    int    dec1;              /* stage-1 decimation and filter for this rate */
    const double *fir1;
    int    fir1_len;
    double center_hz;
    int    max_bins;
    double min_snr;
    int    window_samples;
    int    energy_win;
    double grid_hz;
    double band_min_hz;
    double band_max_hz;
    double hann[SC_HANN_LEN];
    double *pre_i;             /* ring: last SC_PREROLL_SEC of IQ fed to the bins */
    double *pre_q;
    int     pre_len;
    int     pre_n;             /* samples in the ring (<= pre_len) */
    int     pre_w;             /* next write position */
    double *lin_i;             /* the ring in time order, built when a bin needs it */
    double *lin_q;
    int     lin_n;
    int     preroll_pending;   /* a bin was spawned since lin_* was built */
    /* IIR SOS removed - replaced by per-bin FIR delay lines */

    /* Shared FFT front end (stage 1).  Overlap-save: each block is fN input samples, the last
     * fP of the previous block plus fL new ones; one forward FFT per block, then per bin the
     * fNd = fN/dec1 FFT bins around it, times the channel mask fH, one inverse FFT of fNd
     * gives fL/dec1 samples at 12 kHz.  The last ring_k block spectra are kept for pre-roll. */
    int     fft_front;
    int     fN, fNd, fP, fL;
    FftPlan plan_N, plan_Nd;
    double *fH;                /* [fNd] mask, FFT order (index m is offset m or m - fNd bins) */
    double *blk_i, *blk_q;     /* [fN] block being filled */
    int     blk_n;
    int64_t blk_start;         /* input sample index of blk[0] */
    int64_t in_fed;            /* input samples fed before the current itila_sc_feed_iq call */
    int     ring_k, ring_w, ring_cnt;
    double **ring_re, **ring_im;
    int64_t *ring_a;           /* blk_start of each stored spectrum */
    double *scr_re[SC_MAX_THREADS], *scr_im[SC_MAX_THREADS];   /* per-stripe IFFT scratch */
    int     job_slot;

    double iq_res_i[SC_DEC1];
    double iq_res_q[SC_DEC1];
    int    iq_res_n;
    int64_t total_samples;     /* running sample count for eviction timing */

    double *scan_i;
    double *scan_q;
    int     scan_n;
    /* run_scan work buffers and Blackman window, made once instead of per scan */
    FftPlan plan_scan;
    double *scan_win, *scan_re, *scan_im, *scan_psd;
    void   *scan_peaks;

    int    n_bins;
    ScBin  bins[SC_MAX_BINS];

    pthread_mutex_t lock;  /* protects bins/envelope during concurrent feed+decode */

    /* itila2_snr_decoder (itila_sc_set_snr_marks): per bin, the linear power of each path at 200 Hz,
     * in step with env100/env200, and the band's noise density per Hz. NULL / 0 while off. */
    struct ScPow {
        double dl3[2][2*FIR_STAGE3_LEN];
        int    n;              /* samples held in p, from the start of env100/env200 */
        float *p[2];           /* pow_cap each, in pow_buf */
    } *pow;
    float  *pow_buf;       /* SC_MAX_BINS x 2 x pow_cap; a slot takes memory once a bin uses it */
    int     pow_cap;       /* 1.5 decode windows: power past it is not kept (NAN), the envelope is */
    double *scan_pow;      /* energy_win linear bin powers for the noise percentile */
    double  sum_w2;        /* sum of scan_win^2 */
    double  n0;            /* noise density, input power per Hz; 0 until the first scan */

    /* Worker pool for process_bins (itila_sc_set_threads).  Bin k goes to
     * stripe k % n_threads; stripe 0 runs on the calling thread. */
    int      n_threads;
    pthread_t workers[SC_MAX_THREADS];
    struct ScWorker { struct ItilaSc *sc; int stripe; uint64_t seen; } worker_arg[SC_MAX_THREADS];
    pthread_mutex_t pool_lock;
    pthread_cond_t  pool_go;
    pthread_cond_t  pool_done;
    uint64_t pool_gen;
    int      pool_pending;
    int      pool_stop;
    const double *job_i, *job_q;
    int      job_n;
    uint64_t job_drops;

    /* Diagnostic counters */
    uint64_t env_drops;          /* per-bin envelope cap hits */
    uint64_t bins_at_max;        /* peak active bin count */
    uint64_t spawn_gated;        /* candidates rejected (insufficient hits) */
    uint64_t spawn_promoted;     /* candidates that became bins */

    /* Exclusion zones (itila_sc_exclude): frequencies where no bin may run, e.g. the mark and
     * space tones of an RTTY signal. A zone lapses at `until` (total_samples units). */
    struct { double lo_hz, hi_hz; int64_t until; } excl[SC_MAX_EXCL];
    uint64_t excl_released;      /* bins released because a zone covered them */
    uint64_t excl_blocked;       /* scan peaks not spawned because a zone covered them */

    /* Lazy spawn candidate ring - see SC_MAX_CANDIDATES comment block */
    int64_t  scan_counter;
    struct {
        double f_hz;
        double f_first, f_sum;   /* interpolated peak: first hit, sum of hits near it */
        int    n_avg;
        int    hits;
        int64_t last_seen_scan;
        int    in_use;
    } cand[SC_MAX_CANDIDATES];

    /* ITILA decoder function pointers - set via itila_sc_set_decoder */
    void *(*dec_create)(int sample_rate, double lpf_hz);
    const char *(*dec_feed)(void *h, const double *env, int n,
                             double freq_khz, double ev_thresh);
    void (*dec_free)(void *h);
    double (*dec_get_wpm)(void *h);
    /* Optional: per-handle timing-cost confidence score from the most
     * recent dec_feed call.  NULL if older libitila.so without the API;
     * decode loop falls back to cost=999.0 (sentinel) in that case. */
    double (*dec_get_last_cost)(void *h);
    /* itila2_snr_decoder on the C decode path (itila_sc_set_decoder_pow); NULL while off */
    const char *(*dec_feed_pow)(void *h, const double *env, const double *pow, int n, double f_khz, double ev);
    double (*dec_get_mark_pow)(void *h);
    double ev_thresh;
};

static void bin_free_decoders(ItilaSc *sc, ScBin *b);

/* ---- qsort comparator (file scope, not nested) ---- */
typedef struct { double power; double f_hz; double snr; double f_exact; } ScPeak;
static int cmp_peak_desc(const void *a, const void *b)
{
    double da = ((const ScPeak*)a)->power, db = ((const ScPeak*)b)->power;
    return da > db ? -1 : da < db ? 1 : 0;
}

/* ---- energy scan ---- */
/* Whether f_hz lies in a live exclusion zone. Caller holds sc->lock. */
static int sc_excluded(const ItilaSc *sc, double f_hz)
{
    for (int z = 0; z < SC_MAX_EXCL; z++)
        if (sc->excl[z].until > sc->total_samples &&
            f_hz >= sc->excl[z].lo_hz && f_hz <= sc->excl[z].hi_hz)
            return 1;
    return 0;
}

/* The k-th smallest of a[0..n) (quickselect, reorders a). */
static double kth_smallest(double *a, int n, int k)
{
    int lo = 0, hi = n - 1;
    while (lo < hi) {
        double piv = a[(lo + hi) / 2];
        int i = lo, j = hi;
        while (i <= j) {
            while (a[i] < piv) i++;
            while (a[j] > piv) j--;
            if (i <= j) { double t = a[i]; a[i] = a[j]; a[j] = t; i++; j--; }
        }
        if (k <= j) hi = j; else if (k >= i) lo = i; else break;
    }
    return a[k];
}

/* itila2_snr_decoder: the band's noise density from one scan. FFT bin power of white noise is
 * exponential with mean sigma^2 * sum(w^2), so the SC_NOISE_PCT percentile over the decode span
 * (busy bins sit above it) divided by -ln(1 - pct) is that mean; density = sigma^2 / fs. Caller holds sc->lock. */
static void noise_update(ItilaSc *sc, const double *re, const double *im, int N, double bin_hz)
{
    int m = 0;
    for (int k = 0; k < N; k++) {
        int kk = k < N / 2 ? k : k - N;
        double f_abs = sc->center_hz + kk * bin_hz;
        if (abs(kk) <= 2 || f_abs < sc->band_min_hz || f_abs > sc->band_max_hz) continue;   /* DC, out of span */
        sc->scan_pow[m++] = re[k]*re[k] + im[k]*im[k];
    }
    if (m < 64 || sc->sum_w2 <= 0.0) return;
    double q = kth_smallest(sc->scan_pow, m, m * SC_NOISE_PCT / 100);
    double n0 = q / (-log(1.0 - SC_NOISE_PCT / 100.0)) / sc->sum_w2 / sc->sample_rate;
    if (!(n0 > 0.0) || !isfinite(n0)) return;
    double a = 1.0 - exp(-(double)N / (sc->sample_rate * SC_NOISE_TAU_S));
    sc->n0 = sc->n0 > 0.0 ? sc->n0 + a * (n0 - sc->n0) : n0;
}

HOT_CLONES static void run_scan(ItilaSc *sc, const double *seg_i, const double *seg_q)
{
    int N = sc->energy_win;
    double *re = sc->scan_re, *im = sc->scan_im, *psd = sc->scan_psd;

    /* Blackman window + load */
    for (int k = 0; k < N; k++) {
        re[k] = seg_i[k] * sc->scan_win[k];
        im[k] = seg_q[k] * sc->scan_win[k];
    }
    fft_exec(&sc->plan_scan, re, im);

    /* PSD in dB */
    for (int k = 0; k < N; k++)
        psd[k] = 10.0 * log10(re[k]*re[k] + im[k]*im[k] + 1e-20);

    double bin_hz  = (double)sc->sample_rate / N;
    if (sc->scan_pow) noise_update(sc, re, im, N, bin_hz);

    /* Collect LOCAL MAXIMA using CFAR detection - local noise estimate per bin.
     * For each candidate peak, compute median of nearby bins (excluding the peak
     * and its immediate neighbors). Self-calibrates to local QRM conditions. */
    ScPeak *peaks = (ScPeak *)sc->scan_peaks;
    int np = 0;
    int guard = 3;   /* bins to exclude around candidate */
    int window = 20; /* bins each side for local noise estimate */
    for (int k = 1; k < N - 1; k++) {
        if (psd[k] <= psd[k-1] || psd[k] <= psd[k+1]) continue;  /* not a peak */
        /* Local noise: median of bins within ±window, excluding ±guard */
        double local[64]; int nl = 0;
        for (int j = k - window; j <= k + window && nl < 64; j++) {
            int jj = j < 0 ? j + N : j >= N ? j - N : j;  /* wrap around */
            if (abs(j - k) <= guard) continue;
            local[nl++] = psd[jj];
        }
        if (nl < 5) continue;
        /* Median: the nl/2-th smallest (quickselect; same element a full sort would put there) */
        int lo = 0, hi = nl - 1, kth = nl / 2;
        while (lo < hi) {
            double piv = local[(lo + hi) / 2];
            int a = lo, b = hi;
            while (a <= b) {
                while (local[a] < piv) a++;
                while (local[b] > piv) b--;
                if (a <= b) { double t = local[a]; local[a] = local[b]; local[b] = t; a++; b--; }
            }
            if (kth <= b) hi = b; else if (kth >= a) lo = a; else break;
        }
        double local_noise = local[kth];
        if (psd[k] <= local_noise + sc->min_snr) continue;
        /* Parabolic interpolation for sub-bin accuracy */
        double delta = 0.5 * (psd[k-1] - psd[k+1]) /
                       (psd[k-1] - 2.0*psd[k] + psd[k+1]);
        double exact = (double)k + delta;
        if (exact >= N/2) exact -= N;
        double f_hz_interp = exact * bin_hz;
        double f_abs  = sc->center_hz + f_hz_interp;
        if (f_abs < sc->band_min_hz || f_abs > sc->band_max_hz) continue;
        if (sc_excluded(sc, f_abs)) { sc->excl_blocked++; continue; }
        double f_grid = round(f_abs / sc->grid_hz) * sc->grid_hz;
        peaks[np].power = psd[k];
        peaks[np].f_hz  = f_grid;
        peaks[np].snr   = psd[k] - local_noise;
        peaks[np].f_exact = f_abs;
        np++;
    }
    qsort(peaks, np, sizeof(ScPeak), cmp_peak_desc);

    /* Bump scan counter and expire stale candidates (haven't been seen in
     * SC_CAND_EXPIRY_SCANS scans). Recycled slots fall to the allocator. */
    sc->scan_counter++;
    int64_t expiry_cutoff = sc->scan_counter - SC_CAND_EXPIRY_SCANS;
    for (int c = 0; c < SC_MAX_CANDIDATES; c++) {
        if (sc->cand[c].in_use &&
            sc->cand[c].last_seen_scan < expiry_cutoff) {
            sc->cand[c].in_use = 0;
        }
    }

    /* Sweep: evict stale bins regardless of capacity. The capacity-only
     * eviction below was a leak - under typical operation each band's
     * scanner stays well below max_bins, so the eviction path never ran
     * and bins accumulated forever. After ~1-2 hours bin count drifts
     * up to a few hundred, FFT cost climbs, decoder starves, ring
     * drops climb past 50%. Sweeping unconditionally per scan keeps
     * the active set bounded by what the band actually produces. */
    {
        int rate_12k = sc->sample_rate / sc->dec1;
        for (int b = 0; b < SC_MAX_BINS; b++) {
            if (!sc->bins[b].active) continue;
            /* Skip mid-decode bins - see ScBin.in_decode comment. */
            if (sc->bins[b].in_decode) continue;
            int64_t age      = sc->total_samples - sc->bins[b].created_sample;
            int64_t since_ev = sc->total_samples - sc->bins[b].last_evidence;
            int stale    = 0;
            if (sc->bins[b].last_evidence == 0 && age > 300 * rate_12k) stale = 1;
            else if (sc->bins[b].last_evidence > 0 && since_ev > 300 * rate_12k) stale = 1;
            if (!stale && sc_excluded(sc, sc->bins[b].f_hz)) { stale = 1; sc->excl_released++; }
            if (stale) {
                bin_free_decoders(sc, &sc->bins[b]);
                sc->bins[b].active = 0;
                sc->n_bins--;
            }
        }
    }

    /* Cluster (50 Hz), keep strongest per cluster, spawn new bins.
     * History: started at 300 Hz, tightened to 150 Hz on 2026-04-22
     * (commit 45467f1 alongside FIR decimation work).  Tightened again
     * to 50 Hz on 2026-05-24 after RBN cross-reference triage showed
     * K1GU @ 7041.3 and KC7V @ 7039.3 were co-channel-blocked by
     * stronger neighbors within 150 Hz (WJ9B at 7041.2, N8KH at 7039.2).
     * File-mode A/B recovered HA9RE the same way.  Held until decode-
     * thread split (1cb74fc) addressed the bin-pressure ceiling that
     * tighter clustering would otherwise hit. */
    double cluster_hz = 75.0;  /* one signal one bin; co-channel stations 100 Hz apart stay separate */
    for (int i = 0; i < np; i++) {
        double f_hz = peaks[i].f_hz;

        /* Skip if within cluster_hz of any active bin - but update its SNR.
         * Bin and peak both at the interpolated frequency, so one signal is one bin. */
        int found = 0;
        for (int b = 0; b < SC_MAX_BINS; b++) {
            if (sc->bins[b].active && fabs(sc->bins[b].f_hz - peaks[i].f_exact) <= cluster_hz) {
                sc->bins[b].snr_db = peaks[i].snr;
                found = 1; break;
            }
        }
        if (found) continue;

        /* Lazy spawn gate: require SC_CAND_HITS_REQUIRED scans at this
         * frequency before allocating a real bin. Find existing candidate
         * within SC_CAND_CLUSTER_HZ; otherwise allocate a new candidate
         * slot (preferring expired/unused slots). */
        int cand_idx = -1;
        for (int c = 0; c < SC_MAX_CANDIDATES; c++) {
            if (sc->cand[c].in_use &&
                fabs(sc->cand[c].f_hz - f_hz) < SC_CAND_CLUSTER_HZ) {
                cand_idx = c;
                break;
            }
        }
        if (cand_idx < 0) {
            /* Allocate a slot - first try unused, else oldest in_use */
            int64_t oldest_seen = sc->scan_counter + 1;
            int oldest_idx  = -1;
            for (int c = 0; c < SC_MAX_CANDIDATES; c++) {
                if (!sc->cand[c].in_use) { cand_idx = c; break; }
                if (sc->cand[c].last_seen_scan < oldest_seen) {
                    oldest_seen = sc->cand[c].last_seen_scan;
                    oldest_idx  = c;
                }
            }
            if (cand_idx < 0) cand_idx = oldest_idx;
            sc->cand[cand_idx].f_hz   = f_hz;
            sc->cand[cand_idx].hits   = 0;
            sc->cand[cand_idx].f_first = peaks[i].f_exact;
            sc->cand[cand_idx].f_sum  = 0.0;
            sc->cand[cand_idx].n_avg  = 0;
            sc->cand[cand_idx].in_use = 1;
        }
        sc->cand[cand_idx].hits++;
        if (fabs(peaks[i].f_exact - sc->cand[cand_idx].f_first) <= SC_CAND_AVG_HZ) {
            sc->cand[cand_idx].f_sum += peaks[i].f_exact;
            sc->cand[cand_idx].n_avg++;
        }
        sc->cand[cand_idx].last_seen_scan = sc->scan_counter;
        if (sc->cand[cand_idx].hits < SC_CAND_HITS_REQUIRED) {
            sc->spawn_gated++;
            continue;  /* not enough hits yet - defer spawn */
        }
        /* Threshold met: spawn the bin and recycle the candidate slot */
        sc->cand[cand_idx].in_use = 0;
        sc->spawn_promoted++;

        /* Evict stale bins if at capacity: bins that never produced evidence
         * after 120s, or produced evidence but went silent for 300s */
        if (sc->n_bins >= sc->max_bins) {
            int evicted = -1;
            int64_t oldest_age = 0;
            for (int b = 0; b < SC_MAX_BINS; b++) {
                if (!sc->bins[b].active) continue;
                /* Skip bins that decode_ready is currently using under
                 * released lock - see ScBin.in_decode comment. */
                if (sc->bins[b].in_decode) continue;
                int64_t age = sc->total_samples - sc->bins[b].created_sample;
                int64_t since_ev = sc->total_samples - sc->bins[b].last_evidence;
                /* Never produced evidence and >120s old
                 * total_samples counts in 12kHz units (n/SC_DEC1) */
                int rate_12k = sc->sample_rate / sc->dec1;
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
                bin_free_decoders(sc, &sc->bins[evicted]);
                sc->bins[evicted].active = 0;
                sc->n_bins--;
            } else {
                continue;  /* all bins active and producing - can't evict */
            }
        }

        int slot = -1;
        for (int b = 0; b < SC_MAX_BINS; b++) {
            if (!sc->bins[b].active) { slot = b; break; }
        }
        if (slot < 0) continue;

        ScBin *bin = &sc->bins[slot];
        memset(bin, 0, sizeof(ScBin));
        if (sc->pow) { memset(sc->pow[slot].dl3, 0, sizeof(sc->pow[slot].dl3)); sc->pow[slot].n = 0; }
        bin->f_hz           = sc->cand[cand_idx].f_sum / sc->cand[cand_idx].n_avg;
        bin->created_sample = sc->total_samples;
        bin->active  = 1;
        bin->c_phase = 1.0;
        bin->s_phase = 0.0;
        bin->snr_db  = peaks[i].snr;
        bin->need_preroll = 1;
        bin->start_in = sc->in_fed - sc->pre_len;     /* as the per-bin path: the 1 s before this feed */
        sc->preroll_pending = 1;
        sc->n_bins++;
    }
}

/* ---- FIR convolution helper: dot product of delay line with filter ---- */
/* Delay lines are written twice (pos and pos + len), so the newest-first window is one
 * contiguous run: same taps in the same order as (pos - k + len) % len (cdub89/SparkGap#19 A3). */
static double fir_dot(const double *dl, int pos, const double *h, int len)
{
    const double *x = dl + pos + len;
    double acc = 0.0;
    for (int k = 0; k < len; k++)
        acc += x[-k] * h[k];
    return acc;
}

/* Stages 2 and 3 for one 12 kHz sample of a bin: FIR2 (100/200 Hz paths) to 2 kHz, Hann and
 * channel filter, envelope, FIR3 to 200 Hz.  Returns 1 when the envelope buffer was full. */
static int bin_push_12k(const ItilaSc *sc, ScBin *b, double s1_i, double s1_q)
{
    /* Stage 2: push into FIR2 delay lines (100 Hz + 200 Hz paths), decimate 6:1 */
    int pos2_100 = b->dl2_count % FIR_S2_100_LEN;
    int pos2_200 = b->dl2_count % FIR_S2_200_LEN;
    b->dl2_100i[pos2_100] = b->dl2_100i[pos2_100 + FIR_S2_100_LEN] = s1_i;
    b->dl2_100q[pos2_100] = b->dl2_100q[pos2_100 + FIR_S2_100_LEN] = s1_q;
    b->dl2_200i[pos2_200] = b->dl2_200i[pos2_200 + FIR_S2_200_LEN] = s1_i;
    b->dl2_200q[pos2_200] = b->dl2_200q[pos2_200 + FIR_S2_200_LEN] = s1_q;
    if (++b->dl2_count == FIR_S2_100_LEN * FIR_S2_200_LEN * SC_DEC2) b->dl2_count = 0;

    if (b->dl2_count % SC_DEC2 != 0) return 0;
    /* Output one 2k sample per path */
    double s2_100i = fir_dot(b->dl2_100i, pos2_100, FIR_S2_100, FIR_S2_100_LEN);
    double s2_100q = fir_dot(b->dl2_100q, pos2_100, FIR_S2_100, FIR_S2_100_LEN);
    double s2_200i = fir_dot(b->dl2_200i, pos2_200, FIR_S2_200, FIR_S2_200_LEN);
    double s2_200q = fir_dot(b->dl2_200q, pos2_200, FIR_S2_200, FIR_S2_200_LEN);

    /* Hann window on the 100 Hz path before the magnitude */
    int ph = b->dlh_count;
    b->dlh_i[ph] = b->dlh_i[ph + SC_HANN_LEN] = s2_100i;
    b->dlh_q[ph] = b->dlh_q[ph + SC_HANN_LEN] = s2_100q;
    if (++b->dlh_count == SC_HANN_LEN) b->dlh_count = 0;
    s2_100i = fir_dot(b->dlh_i, ph, sc->hann, SC_HANN_LEN);
    s2_100q = fir_dot(b->dlh_q, ph, sc->hann, SC_HANN_LEN);

    int pw = b->dlw_count;
    b->dlw_i[pw] = b->dlw_i[pw + SC_WIDE_LEN] = s2_200i;
    b->dlw_q[pw] = b->dlw_q[pw + SC_WIDE_LEN] = s2_200q;
    if (++b->dlw_count == SC_WIDE_LEN) b->dlw_count = 0;
    s2_200i = fir_dot(b->dlw_i, pw, WIDE_TAPS, SC_WIDE_LEN);
    s2_200q = fir_dot(b->dlw_q, pw, WIDE_TAPS, SC_WIDE_LEN);

    /* Envelope at 2 kHz */
    double env2_100 = sqrt(s2_100i*s2_100i + s2_100q*s2_100q);
    double env2_200 = sqrt(s2_200i*s2_200i + s2_200q*s2_200q);

    /* Stage 3: push envelope into FIR3 delay lines, decimate 10:1 */
    int pos3 = b->dl3_count % FIR_STAGE3_LEN;
    b->dl3_100[pos3] = b->dl3_100[pos3 + FIR_STAGE3_LEN] = env2_100;
    b->dl3_200[pos3] = b->dl3_200[pos3 + FIR_STAGE3_LEN] = env2_200;
    struct ScPow *spw = sc->pow ? &sc->pow[b - sc->bins] : NULL;
    if (spw) {      /* itila2_snr_decoder: the same smoother on linear power, before the magnitude */
        spw->dl3[0][pos3] = spw->dl3[0][pos3 + FIR_STAGE3_LEN] = s2_100i*s2_100i + s2_100q*s2_100q;
        spw->dl3[1][pos3] = spw->dl3[1][pos3 + FIR_STAGE3_LEN] = s2_200i*s2_200i + s2_200q*s2_200q;
    }
    if (++b->dl3_count == FIR_STAGE3_LEN * SC_DEC3) b->dl3_count = 0;

    if (b->dl3_count % SC_DEC3 != 0) return 0;
    /* Output one 200 Hz envelope sample */
    double env_100 = fir_dot(b->dl3_100, pos3, FIR_STAGE3, FIR_STAGE3_LEN);
    double env_200 = fir_dot(b->dl3_200, pos3, FIR_STAGE3, FIR_STAGE3_LEN);

    if (b->env_n < SC_ENV_CAP) {
        if (spw && b->env_n < sc->pow_cap) {        /* samples in step with the envelope; gaps are NAN */
            for (int k = spw->n; k < b->env_n; k++) spw->p[0][k] = spw->p[1][k] = NAN;
            spw->p[0][b->env_n] = (float)fir_dot(spw->dl3[0], pos3, FIR_STAGE3, FIR_STAGE3_LEN);
            spw->p[1][b->env_n] = (float)fir_dot(spw->dl3[1], pos3, FIR_STAGE3, FIR_STAGE3_LEN);
            spw->n = b->env_n + 1;
        }
        b->env100[b->env_n] = env_100;
        b->env200[b->env_n] = env_200;
        b->env_n++;
    } else {
        return 1;
    }
    return 0;
}

/* ---- per-bin DSP: all-FIR linear-phase chain ---- */
HOT_CLONES static int process_bin(ItilaSc *sc, ScBin *b, const double *i_full, const double *q_full, int n)
{
    int drops = 0;
    {

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
            int pos1 = b->dl1_count % sc->fir1_len;
            b->dl1_i[pos1] = b->dl1_i[pos1 + sc->fir1_len] = mixed_i;
            b->dl1_q[pos1] = b->dl1_q[pos1 + sc->fir1_len] = mixed_q;
            if (++b->dl1_count == sc->fir1_len * sc->dec1) b->dl1_count = 0;  /* wrap: keeps both moduli */

            if (b->dl1_count % sc->dec1 != 0) continue;
            /* Output one 12k sample */
            double s1_i = fir_dot(b->dl1_i, pos1, sc->fir1, sc->fir1_len);
            double s1_q = fir_dot(b->dl1_q, pos1, sc->fir1, sc->fir1_len);

            drops += bin_push_12k(sc, b, s1_i, s1_q);
        }

        /* Renormalize oscillator */
        double norm = 1.0 / sqrt(cp*cp + sp*sp);
        b->c_phase = cp * norm;
        b->s_phase = sp * norm;
    }
    return drops;
}

/* Stage 1 from the shared FFT: one block of a bin.  The fNd FFT bins around the bin, times the
 * mask, inverse FFT.  The mask is zero-phase, so an output needs input on both sides of it: half
 * the overlap (fP/2) is dropped at each end of the block, else the last outputs wrap around to
 * the start of the block (a strong carrier nearby then leaks a burst every block).  The block FFT
 * mixes by k0 FFT bins relative to blk_start, so a phase term makes the mix continuous from
 * block to block; the residual (under half an FFT bin) is mixed out at 12 kHz by the bin's
 * oscillator, after the mask, which is flat far beyond it. */
HOT_CLONES static int process_bin_fft(ItilaSc *sc, ScBin *b, int slot, double *yr, double *yi)
{
    const double *xr = sc->ring_re[slot], *xi = sc->ring_im[slot];
    int N = sc->fN, Nd = sc->fNd;
    double df  = (double)sc->sample_rate / N;
    double off = b->f_hz - sc->center_hz;
    long k0 = lround(off / df);
    double r = off - k0 * df;
    for (int m = 0; m < Nd; m++) {
        long mm = m < Nd / 2 ? m : m - Nd;
        long src = ((k0 + mm) % N + N) % N;
        /* conjugate in, conjugate out: the forward FFT computes the inverse */
        yr[m] =  xr[src] * sc->fH[m];
        yi[m] = -xi[src] * sc->fH[m];
    }
    fft_exec(&sc->plan_Nd, yr, yi);
    long ph = (long)(((int64_t)k0 % N * (sc->ring_a[slot] % N)) % N);
    double th = -2.0 * M_PI * (double)ph / N;
    double pc = cos(th) / N, ps = sin(th) / N;          /* 1/N: unnormalised forward + inverse */
    double step = -2.0 * M_PI * r * sc->dec1 / sc->sample_rate;
    double c_step = cos(step), s_step = sin(step);
    double cp = b->c_phase, sp = b->s_phase;
    int drops = 0;
    int t0 = sc->fP / sc->dec1 / 2, t1 = Nd - t0;                  /* half the overlap off each end */
    int64_t first = sc->ring_a[slot] + (int64_t)t0 * sc->dec1;     /* input sample of output t0 */
    if (first < b->start_in) {                                     /* pre-roll starts mid-block */
        int64_t skip = (b->start_in - first + sc->dec1 - 1) / sc->dec1;
        t0 = skip >= t1 - t0 ? t1 : t0 + (int)skip;
    }
    for (int t = t0; t < t1; t++) {
        double zr = yr[t], zi = -yi[t];
        double ar = zr*pc - zi*ps, ai = zr*ps + zi*pc;
        double s1_i = ar*cp - ai*sp, s1_q = ar*sp + ai*cp;
        double cn = cp*c_step - sp*s_step;
        sp = sp*c_step + cp*s_step; cp = cn;
        drops += bin_push_12k(sc, b, s1_i, s1_q);
    }
    double norm = 1.0 / sqrt(cp*cp + sp*sp);
    b->c_phase = cp * norm;
    b->s_phase = sp * norm;
    return drops;
}

/* A new bin first gets the stored block spectra older than `slot`, oldest first (pre-roll). */
static int preroll_fft(ItilaSc *sc, ScBin *b, int slot, double *yr, double *yi)
{
    int drops = 0;
    for (int age = sc->ring_cnt - 1; age >= 1; age--) {
        int k = ((slot - age) % sc->ring_k + sc->ring_k) % sc->ring_k;
        drops += process_bin_fft(sc, b, k, yr, yi);
    }
    return drops;
}

/* One stripe of the bins: every n_threads-th bin from `stripe`.  A bin's
 * DSP touches only that bin, and the scanner fields read here are constant
 * while process_bins runs, so stripes need no locking. */
static int process_stripe(ItilaSc *sc, int stripe, const double *i_full, const double *q_full, int n)
{
    int drops = 0;
    int step = sc->n_threads > 1 ? sc->n_threads : 1;
    for (int bi = stripe; bi < SC_MAX_BINS; bi += step) {
        ScBin *b = &sc->bins[bi];
        if (!b->active) continue;
        if (sc->fft_front) {
            if (b->need_preroll) {
                b->need_preroll = 0;
                drops += preroll_fft(sc, b, sc->job_slot, sc->scr_re[stripe], sc->scr_im[stripe]);
            }
            drops += process_bin_fft(sc, b, sc->job_slot, sc->scr_re[stripe], sc->scr_im[stripe]);
            continue;
        }
        if (b->need_preroll) {
            b->need_preroll = 0;
            if (sc->lin_n > 0) drops += process_bin(sc, b, sc->lin_i, sc->lin_q, sc->lin_n);
        }
        drops += process_bin(sc, b, i_full, q_full, n);
    }
    return drops;
}

static void *scan_worker(void *arg)
{
    struct ScWorker *w = (struct ScWorker *)arg;
    ItilaSc *sc = w->sc;
    uint64_t seen = w->seen;    /* jobs before this worker started are not its own */
    for (;;) {
        pthread_mutex_lock(&sc->pool_lock);
        while (!sc->pool_stop && sc->pool_gen == seen)
            pthread_cond_wait(&sc->pool_go, &sc->pool_lock);
        if (sc->pool_stop) { pthread_mutex_unlock(&sc->pool_lock); return NULL; }
        seen = sc->pool_gen;
        const double *ji = sc->job_i, *jq = sc->job_q;
        int jn = sc->job_n;
        pthread_mutex_unlock(&sc->pool_lock);

        int drops = process_stripe(sc, w->stripe, ji, jq, jn);

        pthread_mutex_lock(&sc->pool_lock);
        sc->job_drops += (uint64_t)drops;
        if (--sc->pool_pending == 0) pthread_cond_signal(&sc->pool_done);
        pthread_mutex_unlock(&sc->pool_lock);
    }
}

/* Lay the pre-roll ring out in time order for the bins spawned since the last call. */
static void preroll_linearize(ItilaSc *sc)
{
    /* Until the ring first fills, pre_w == pre_n and the data is already in order. */
    int head  = sc->pre_n < sc->pre_len ? 0 : sc->pre_w;     /* oldest sample */
    int first = sc->pre_n - head;                            /* head .. end of data */
    memcpy(sc->lin_i, sc->pre_i + head, first * sizeof(double));
    memcpy(sc->lin_q, sc->pre_q + head, first * sizeof(double));
    memcpy(sc->lin_i + first, sc->pre_i, head * sizeof(double));
    memcpy(sc->lin_q + first, sc->pre_q, head * sizeof(double));
    sc->lin_n = sc->pre_n;
}

static void process_bins(ItilaSc *sc, const double *i_full, const double *q_full, int n)
{
    if (sc->preroll_pending && !sc->fft_front) {
        sc->preroll_pending = 0;
        preroll_linearize(sc);
    }
    if (sc->n_threads <= 1) {
        sc->env_drops += (uint64_t)process_stripe(sc, 0, i_full, q_full, n);
        return;
    }
    pthread_mutex_lock(&sc->pool_lock);
    sc->job_i = i_full; sc->job_q = q_full; sc->job_n = n;
    sc->job_drops = 0;
    sc->pool_pending = sc->n_threads - 1;
    sc->pool_gen++;
    pthread_cond_broadcast(&sc->pool_go);
    pthread_mutex_unlock(&sc->pool_lock);

    uint64_t drops = (uint64_t)process_stripe(sc, 0, i_full, q_full, n);

    pthread_mutex_lock(&sc->pool_lock);
    while (sc->pool_pending > 0)
        pthread_cond_wait(&sc->pool_done, &sc->pool_lock);
    drops += sc->job_drops;
    pthread_mutex_unlock(&sc->pool_lock);
    sc->env_drops += drops;
}

static void stop_workers(ItilaSc *sc)
{
    if (sc->n_threads <= 1) return;
    pthread_mutex_lock(&sc->pool_lock);
    sc->pool_stop = 1;
    pthread_cond_broadcast(&sc->pool_go);
    pthread_mutex_unlock(&sc->pool_lock);
    for (int k = 1; k < sc->n_threads; k++)
        pthread_join(sc->workers[k], NULL);
    sc->n_threads = 1;
    sc->pool_stop = 0;
}

/* Split the per-bin DSP over n threads (1 = the calling thread only).
 * Call before feeding, from the thread that feeds.  Returns the count in use. */
int itila_sc_set_threads(ItilaSc *sc, int n)
{
    if (!sc) return 0;
    if (n < 1) n = 1;
    if (n > SC_MAX_THREADS) n = SC_MAX_THREADS;
    pthread_mutex_lock(&sc->lock);
    stop_workers(sc);
    int started = 1;
    for (int k = 1; k < n; k++) {
        sc->worker_arg[k].sc = sc;
        sc->worker_arg[k].stripe = k;
        sc->worker_arg[k].seen = sc->pool_gen;
        if (pthread_create(&sc->workers[k], NULL, scan_worker, &sc->worker_arg[k]) != 0)
            break;
        started++;
    }
    sc->n_threads = started;
    pthread_mutex_unlock(&sc->lock);
    return started;
}

/* One path's first n power samples of a bin, NAN where none was kept. Caller holds sc->lock. */
static void pow_take(const struct ScPow *pw, int path, int n, double *out)
{
    for (int j = 0; j < n; j++) out[j] = j < pw->n ? (double)pw->p[path][j] : NAN;
}

/* Drop a bin's first n power samples, as the envelope's. Caller holds sc->lock. */
static void pow_shift(struct ScPow *pw, int n)
{
    int keep = pw->n - n;
    if (keep > 0) {
        memmove(pw->p[0], pw->p[0] + n, (size_t)keep * sizeof(float));
        memmove(pw->p[1], pw->p[1] + n, (size_t)keep * sizeof(float));
    }
    pw->n = keep > 0 ? keep : 0;
}

/* Config itila2_snr_decoder: keep each bin's linear power and the band's noise density (see
 * struct ScPow). Call before the first feed. Returns 1 when on, 0 when off or out of memory. */
int itila_sc_set_snr_marks(ItilaSc *sc, int on)
{
    if (!sc) return 0;
    int ok = 0;
    pthread_mutex_lock(&sc->lock);
    if (on && !sc->pow) {
        int cap = sc->window_samples * 3 / 2;
        sc->pow_cap = cap > 0 && cap < SC_ENV_CAP ? cap : SC_ENV_CAP;
        sc->pow = calloc(SC_MAX_BINS, sizeof *sc->pow);
        sc->pow_buf = calloc((size_t)SC_MAX_BINS * 2 * sc->pow_cap, sizeof(float));
        sc->scan_pow = malloc((size_t)sc->energy_win * sizeof(double));
        if (!sc->pow || !sc->pow_buf || !sc->scan_pow) {
            free(sc->pow); free(sc->pow_buf); free(sc->scan_pow);
            sc->pow = NULL; sc->pow_buf = NULL; sc->scan_pow = NULL;
        } else {
            for (int i = 0; i < SC_MAX_BINS; i++)
                for (int p = 0; p < 2; p++)
                    sc->pow[i].p[p] = sc->pow_buf + ((size_t)i * 2 + p) * sc->pow_cap;
        }
        double s = 0.0;
        for (int k = 0; k < sc->energy_win; k++) s += sc->scan_win[k] * sc->scan_win[k];
        sc->sum_w2 = s;
        sc->n0 = 0.0;
    }
    ok = sc->pow != NULL;
    pthread_mutex_unlock(&sc->lock);
    return ok;
}

/* drain_env plus each path's power (p100_out, p200_out) and the band's noise density at this
 * moment (*n0_out, 0 before the first scan), all under one lock. -1 when itila2_snr_decoder is off. */
int itila_sc_drain_env_pow(ItilaSc *sc, double f_hz, double *env100_out, double *env200_out,
                           double *p100_out, double *p200_out, int max_n, double *n0_out)
{
    int n = 0;
    pthread_mutex_lock(&sc->lock);
    if (!sc->pow) { pthread_mutex_unlock(&sc->lock); return -1; }
    *n0_out = sc->n0;
    for (int i = 0; i < SC_MAX_BINS; i++) {
        ScBin *b = &sc->bins[i];
        if (!b->active || fabs(b->f_hz - f_hz) >= 1.0) continue;
        struct ScPow *pw = &sc->pow[i];
        n = b->env_n < max_n ? b->env_n : max_n;
        memcpy(env100_out, b->env100, n * sizeof(double));
        memcpy(env200_out, b->env200, n * sizeof(double));
        pow_take(pw, 0, n, p100_out);
        pow_take(pw, 1, n, p200_out);
        int rem = b->env_n - n;
        memmove(b->env100, b->env100 + n, rem * sizeof(double));
        memmove(b->env200, b->env200 + n, rem * sizeof(double));
        pow_shift(pw, n);
        b->env_n = rem;
        break;
    }
    pthread_mutex_unlock(&sc->lock);
    return n;
}

/* itila2_snr_decoder: SNR in 500 Hz from a window's key-down power on one path (itila_get_mark_power)
 * and the noise density drained with it: signal = mark power less the noise in the path's bandwidth. */
double itila_sc_snr_from_power(double mark_pow, double n0, int wide)
{
    if (!isfinite(mark_pow) || !isfinite(n0) || n0 <= 0.0) return NAN;
    double s = mark_pow - n0 * (wide ? SC_ENBW_200_HZ : SC_ENBW_100_HZ);
    return s > 0.0 ? 10.0 * log10(s / (SC_SNR_REF_HZ * n0)) : NAN;
}

/* Keep the last pre_len samples fed to the bins in a ring.  Copies only the new
 * samples: the live C path feeds ~5 ms at a time, and shifting the whole 1 s buffer
 * on every feed cost a large share of each band's drain thread. */
static void preroll_push(ItilaSc *sc, const double *i_full, const double *q_full, int n)
{
    if (n > sc->pre_len) {                 /* only the last pre_len samples matter */
        i_full += n - sc->pre_len;
        q_full += n - sc->pre_len;
        n = sc->pre_len;
    }
    int a = sc->pre_len - sc->pre_w;       /* room before the ring wraps */
    if (a > n) a = n;
    memcpy(sc->pre_i + sc->pre_w, i_full, a * sizeof(double));
    memcpy(sc->pre_q + sc->pre_w, q_full, a * sizeof(double));
    memcpy(sc->pre_i, i_full + a, (n - a) * sizeof(double));
    memcpy(sc->pre_q, q_full + a, (n - a) * sizeof(double));
    sc->pre_w = (sc->pre_w + n) % sc->pre_len;
    sc->pre_n = sc->pre_n + n > sc->pre_len ? sc->pre_len : sc->pre_n + n;
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
    /* SOS coefficients ignored - FIR chain replaces IIR.
     * Signature kept for backward compatibility with Python wrapper. */
    (void)sos100_flat; (void)n_sos; (void)sos200_flat;
    if (max_bins > SC_MAX_BINS) max_bins = SC_MAX_BINS;
    if (energy_win < 4) energy_win = 4096;

    ItilaSc *sc = (ItilaSc *)calloc(1, sizeof(ItilaSc));
    if (!sc) return NULL;
    pthread_mutex_init(&sc->lock, NULL);       /* before anything that can fail into itila_sc_free */
    pthread_mutex_init(&sc->pool_lock, NULL);
    pthread_cond_init(&sc->pool_go, NULL);
    pthread_cond_init(&sc->pool_done, NULL);
    sc->n_threads = 1;
    switch (sample_rate) {
    case 192000: sc->dec1 = 16; sc->fir1 = FIR1W_192K; sc->fir1_len = FIR1W_192K_LEN; break;
    case 96000:  sc->dec1 = 8;  sc->fir1 = FIR1W_96K;  sc->fir1_len = FIR1W_96K_LEN;  break;
    case 48000:  sc->dec1 = 4;  sc->fir1 = FIR1W_48K;  sc->fir1_len = FIR1W_48K_LEN;  break;
    default:     itila_sc_free(sc); return NULL;
    }
    /* Scale the scan FFT with the rate: same bin width and scan period as 192 kHz */
    energy_win = energy_win * sc->dec1 / SC_DEC1;

    sc->sample_rate    = sample_rate;
    sc->center_hz      = center_hz;
    sc->max_bins       = max_bins;
    sc->min_snr        = min_snr;
    sc->window_samples = window_samples;
    sc->energy_win     = energy_win;
    sc->grid_hz        = grid_hz;
    sc->band_min_hz    = band_min_hz;
    sc->band_max_hz    = band_max_hz;

    double sum = 0.0;
    for (int k = 0; k < SC_HANN_LEN; k++) { sc->hann[k] = 0.5 - 0.5 * cos(2.0 * M_PI * (k + 0.5) / SC_HANN_LEN); sum += sc->hann[k]; }
    for (int k = 0; k < SC_HANN_LEN; k++) sc->hann[k] /= sum;

    sc->scan_i = (double *)calloc(energy_win, sizeof(double));
    sc->scan_q = (double *)calloc(energy_win, sizeof(double));
    sc->scan_win = (double *)malloc(energy_win * sizeof(double));
    sc->scan_re  = (double *)malloc(energy_win * sizeof(double));
    sc->scan_im  = (double *)malloc(energy_win * sizeof(double));
    sc->scan_psd = (double *)malloc(energy_win * sizeof(double));
    sc->scan_peaks = malloc(energy_win * sizeof(ScPeak));
    if (!sc->scan_win || !sc->scan_re || !sc->scan_im || !sc->scan_psd || !sc->scan_peaks ||
        !fft_plan_init(&sc->plan_scan, energy_win)) {
        itila_sc_free(sc); return NULL;
    }
    for (int k = 0; k < energy_win; k++)
        sc->scan_win[k] = 0.42 - 0.5*cos(2.0*M_PI*k/(energy_win-1)) + 0.08*cos(4.0*M_PI*k/(energy_win-1));
    sc->pre_len = (int)(SC_PREROLL_SEC * sample_rate) / sc->dec1 * sc->dec1;
    sc->pre_i = (double *)calloc(sc->pre_len, sizeof(double));
    sc->pre_q = (double *)calloc(sc->pre_len, sizeof(double));
    sc->lin_i = (double *)calloc(sc->pre_len, sizeof(double));
    sc->lin_q = (double *)calloc(sc->pre_len, sizeof(double));
    if (!sc->scan_i || !sc->scan_q || !sc->pre_i || !sc->pre_q || !sc->lin_i || !sc->lin_q) {
        itila_sc_free(sc); return NULL;
    }

    const char *fir1_env = getenv("ITILA_SC_FIR1");
    sc->fft_front = !(fir1_env && fir1_env[0] == '1');
    if (sc->fft_front) {
        /* 11.7 Hz FFT bins at every rate; 1024 bins = 12 kHz per bin; 64 output samples of overlap
         * (5.3 ms), 32 dropped at each end, hold the zero-phase mask's impulse response on both
         * sides; 960 new 12 kHz samples per block, 2.7 ms behind the input. */
        sc->fNd = 1024;
        sc->fN  = sc->fNd * sc->dec1;
        sc->fP  = 64 * sc->dec1;
        sc->fL  = sc->fN - sc->fP;
        sc->ring_k = (sc->pre_len + sc->fL - 1) / sc->fL + 1;
        int ok = fft_plan_init(&sc->plan_N, sc->fN) && fft_plan_init(&sc->plan_Nd, sc->fNd);
        sc->fH    = (double *)calloc(sc->fNd, sizeof(double));
        sc->blk_i = (double *)calloc(sc->fN, sizeof(double));
        sc->blk_q = (double *)calloc(sc->fN, sizeof(double));
        sc->ring_re = (double **)calloc(sc->ring_k, sizeof(double *));
        sc->ring_im = (double **)calloc(sc->ring_k, sizeof(double *));
        sc->ring_a  = (int64_t *)calloc(sc->ring_k, sizeof(int64_t));
        ok = ok && sc->fH && sc->blk_i && sc->blk_q && sc->ring_re && sc->ring_im && sc->ring_a;
        for (int k = 0; ok && k < sc->ring_k; k++) {
            sc->ring_re[k] = (double *)malloc(sc->fN * sizeof(double));
            sc->ring_im[k] = (double *)malloc(sc->fN * sizeof(double));
            ok = sc->ring_re[k] && sc->ring_im[k];
        }
        for (int k = 0; ok && k < SC_MAX_THREADS; k++) {
            sc->scr_re[k] = (double *)malloc(sc->fNd * sizeof(double));
            sc->scr_im[k] = (double *)malloc(sc->fNd * sizeof(double));
            ok = sc->scr_re[k] && sc->scr_im[k];
        }
        if (!ok) { itila_sc_free(sc); return NULL; }
        /* Channel mask: flat to +-4.8 kHz, raised-cosine to zero at +-6 kHz (the 12 kHz edges).
         * The old FIR1 droops < 0.03 dB at 300 Hz; downstream FIR2 only passes +-200 Hz. */
        double df = (double)sample_rate / sc->fN;
        for (int m = 0; m < sc->fNd; m++) {
            double f = fabs((m < sc->fNd / 2 ? m : m - sc->fNd) * df);
            sc->fH[m] = f <= 4800.0 ? 1.0 : f >= 6000.0 ? 0.0 : 0.5 * (1.0 + cos(M_PI * (f - 4800.0) / 1200.0));
        }
        sc->blk_n = sc->fP / 2;          /* silence before input sample 0: the first output is sample 0 */
        sc->blk_start = -sc->fP / 2;
    }

    return sc;
}

void itila_sc_free(ItilaSc *sc)
{
    if (!sc) return;
    pthread_mutex_lock(&sc->lock);             /* wait out an in-flight feed */
    stop_workers(sc);
    for (int i = 0; i < SC_MAX_BINS; i++)
        if (sc->bins[i].active) bin_free_decoders(sc, &sc->bins[i]);
    pthread_mutex_unlock(&sc->lock);
    pthread_mutex_destroy(&sc->lock);
    pthread_mutex_destroy(&sc->pool_lock);
    pthread_cond_destroy(&sc->pool_go);
    pthread_cond_destroy(&sc->pool_done);
    free(sc->pow);
    free(sc->pow_buf);
    free(sc->scan_pow);
    free(sc->scan_i);
    free(sc->scan_q);
    free(sc->scan_win); free(sc->scan_re); free(sc->scan_im); free(sc->scan_psd); free(sc->scan_peaks);
    fft_plan_free(&sc->plan_scan);
    free(sc->pre_i);
    free(sc->pre_q);
    free(sc->lin_i);
    free(sc->lin_q);
    if (sc->fft_front) {
        fft_plan_free(&sc->plan_N);
        fft_plan_free(&sc->plan_Nd);
        for (int k = 0; k < sc->ring_k; k++) {
            if (sc->ring_re) free(sc->ring_re[k]);
            if (sc->ring_im) free(sc->ring_im[k]);
        }
        free(sc->ring_re); free(sc->ring_im); free(sc->ring_a);
        for (int k = 0; k < SC_MAX_THREADS; k++) { free(sc->scr_re[k]); free(sc->scr_im[k]); }
        free(sc->fH); free(sc->blk_i); free(sc->blk_q);
    }
    free(sc);
}

double itila_sc_get_snr(ItilaSc *sc, double f_hz)
{
    double snr = 0.0;
    pthread_mutex_lock(&sc->lock);
    for (int i = 0; i < SC_MAX_BINS; i++) {
        if (sc->bins[i].active && fabs(sc->bins[i].f_hz - f_hz) < 1.0) {
            snr = sc->bins[i].snr_db;
            break;
        }
    }
    pthread_mutex_unlock(&sc->lock);
    return snr;
}

void itila_sc_mark_evidence(ItilaSc *sc, double f_hz)
{
    pthread_mutex_lock(&sc->lock);
    for (int i = 0; i < SC_MAX_BINS; i++) {
        if (sc->bins[i].active && fabs(sc->bins[i].f_hz - f_hz) < 1.0) {
            sc->bins[i].last_evidence = sc->total_samples;
            break;
        }
    }
    pthread_mutex_unlock(&sc->lock);
}

/* Keep bins off [lo_hz, hi_hz] (absolute RF) for hold_sec: no new bin is spawned there and a
 * bin already there is released at the next scan. Used for the mark and space tones of an
 * RTTY signal, which a CW bin can only decode as junk. A zone that matches one already held
 * (within 10 Hz) is refreshed. */
void itila_sc_exclude(ItilaSc *sc, double lo_hz, double hi_hz, double hold_sec)
{
    if (!sc || hi_hz < lo_hz || hold_sec <= 0) return;
    pthread_mutex_lock(&sc->lock);
    int64_t until = sc->total_samples + (int64_t)(hold_sec * (sc->sample_rate / sc->dec1));
    int slot = -1;
    for (int z = 0; z < SC_MAX_EXCL; z++)
        if (sc->excl[z].until > sc->total_samples &&
            fabs(sc->excl[z].lo_hz - lo_hz) <= 10.0 && fabs(sc->excl[z].hi_hz - hi_hz) <= 10.0) {
            slot = z; break;
        }
    if (slot < 0) {                       /* a lapsed slot, else the one lapsing soonest */
        int64_t soonest = INT64_MAX;
        for (int z = 0; z < SC_MAX_EXCL; z++) {
            if (sc->excl[z].until <= sc->total_samples) { slot = z; break; }
            if (sc->excl[z].until < soonest) { soonest = sc->excl[z].until; slot = z; }
        }
    }
    sc->excl[slot].lo_hz = lo_hz;
    sc->excl[slot].hi_hz = hi_hz;
    sc->excl[slot].until = until;
    pthread_mutex_unlock(&sc->lock);
}

/* Counter reads under the lock: feed_iq increments them (64-bit, could tear). */
static unsigned long long locked_u64(ItilaSc *sc, const uint64_t *v)
{
    pthread_mutex_lock(&sc->lock);
    unsigned long long r = (unsigned long long)*v;
    pthread_mutex_unlock(&sc->lock);
    return r;
}

unsigned long long itila_sc_excl_released(ItilaSc *sc) { return locked_u64(sc, &sc->excl_released); }
unsigned long long itila_sc_excl_blocked(ItilaSc *sc) { return locked_u64(sc, &sc->excl_blocked); }

void itila_sc_feed_iq(ItilaSc *sc,
                       const double *i_arr, const double *q_arr, int n)
{
    pthread_mutex_lock(&sc->lock);
    sc->total_samples += n / sc->dec1;  /* count in 12kHz samples */
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

    if (sc->fft_front) {
        /* --- stage 1 by blocks: fill, FFT, store the spectrum, run the bins --- */
        int pos = 0;
        while (pos < n) {
            int copy = sc->fN - sc->blk_n;
            if (copy > n - pos) copy = n - pos;
            memcpy(sc->blk_i + sc->blk_n, i_arr + pos, copy * sizeof(double));
            memcpy(sc->blk_q + sc->blk_n, q_arr + pos, copy * sizeof(double));
            sc->blk_n += copy;
            pos += copy;
            if (sc->blk_n < sc->fN) break;
            int slot = sc->ring_w;
            memcpy(sc->ring_re[slot], sc->blk_i, sc->fN * sizeof(double));
            memcpy(sc->ring_im[slot], sc->blk_q, sc->fN * sizeof(double));
            fft_exec(&sc->plan_N, sc->ring_re[slot], sc->ring_im[slot]);
            sc->ring_a[slot] = sc->blk_start;
            sc->ring_w = (slot + 1) % sc->ring_k;
            if (sc->ring_cnt < sc->ring_k) sc->ring_cnt++;
            if (sc->n_bins > 0) {
                sc->job_slot = slot;
                process_bins(sc, NULL, NULL, 0);
            }
            memmove(sc->blk_i, sc->blk_i + sc->fL, sc->fP * sizeof(double));
            memmove(sc->blk_q, sc->blk_q + sc->fL, sc->fP * sizeof(double));
            sc->blk_n = sc->fP;
            sc->blk_start += sc->fL;
        }
        sc->in_fed += n;
        pthread_mutex_unlock(&sc->lock);
        return;
    }

    /* --- IQ routing: prepend residual, process bins --- */
    int total   = sc->iq_res_n + n;
    int n_dec1  = (total / sc->dec1) * sc->dec1;
    int new_res = total - n_dec1;

    double *i_full = (double *)malloc(total * sizeof(double));
    double *q_full = (double *)malloc(total * sizeof(double));
    if (!i_full || !q_full) { free(i_full); free(q_full); pthread_mutex_unlock(&sc->lock); return; }

    memcpy(i_full,                  sc->iq_res_i, sc->iq_res_n * sizeof(double));
    memcpy(q_full,                  sc->iq_res_q, sc->iq_res_n * sizeof(double));
    memcpy(i_full + sc->iq_res_n,  i_arr,         n * sizeof(double));
    memcpy(q_full + sc->iq_res_n,  q_arr,         n * sizeof(double));

    memcpy(sc->iq_res_i, i_full + n_dec1, new_res * sizeof(double));
    memcpy(sc->iq_res_q, q_full + n_dec1, new_res * sizeof(double));
    sc->iq_res_n = new_res;

    if (n_dec1 > 0 && sc->n_bins > 0)
        process_bins(sc, i_full, q_full, n_dec1);
    if (n_dec1 > 0)
        preroll_push(sc, i_full, q_full, n_dec1);

    free(i_full);
    free(q_full);
    pthread_mutex_unlock(&sc->lock);
}

int itila_sc_ready_bins(ItilaSc *sc, double *f_hz_out, int max_out)
{
    int count = 0;
    pthread_mutex_lock(&sc->lock);
    for (int i = 0; i < SC_MAX_BINS && count < max_out; i++) {
        if (sc->bins[i].active && sc->bins[i].env_n >= sc->window_samples)
            f_hz_out[count++] = sc->bins[i].f_hz;
    }
    pthread_mutex_unlock(&sc->lock);
    return count;
}

int itila_sc_drain_env(ItilaSc *sc, double f_hz,
                        double *env100_out, double *env200_out, int max_n)
{
    int n = 0;
    pthread_mutex_lock(&sc->lock);
    for (int i = 0; i < SC_MAX_BINS; i++) {
        ScBin *b = &sc->bins[i];
        if (!b->active || fabs(b->f_hz - f_hz) >= 1.0) continue;
        n = b->env_n < max_n ? b->env_n : max_n;
        memcpy(env100_out, b->env100, n * sizeof(double));
        memcpy(env200_out, b->env200, n * sizeof(double));
        int rem = b->env_n - n;
        memmove(b->env100, b->env100 + n, rem * sizeof(double));
        memmove(b->env200, b->env200 + n, rem * sizeof(double));
        b->env_n = rem;
        break;
    }
    pthread_mutex_unlock(&sc->lock);
    return n;
}

int itila_sc_peek_env(ItilaSc *sc, double f_hz,
                       double *env100_out, double *env200_out, int max_n)
{
    int n = 0;
    pthread_mutex_lock(&sc->lock);
    for (int i = 0; i < SC_MAX_BINS; i++) {
        ScBin *b = &sc->bins[i];
        if (!b->active || fabs(b->f_hz - f_hz) >= 1.0) continue;
        n = b->env_n < max_n ? b->env_n : max_n;
        memcpy(env100_out, b->env100, n * sizeof(double));
        memcpy(env200_out, b->env200, n * sizeof(double));
        break;
    }
    pthread_mutex_unlock(&sc->lock);
    return n;
}

int itila_sc_bin_count(ItilaSc *sc)
{
    pthread_mutex_lock(&sc->lock);
    if (sc->n_bins > (int)sc->bins_at_max) sc->bins_at_max = sc->n_bins;
    int n = sc->n_bins;
    pthread_mutex_unlock(&sc->lock);
    return n;
}

unsigned long long itila_sc_env_drops(ItilaSc *sc) { return locked_u64(sc, &sc->env_drops); }
int      itila_sc_bins_peak(ItilaSc *sc)
{
    pthread_mutex_lock(&sc->lock);
    int n = (int)sc->bins_at_max;
    pthread_mutex_unlock(&sc->lock);
    return n;
}

int itila_sc_env_n(ItilaSc *sc, double f_hz)
{
    int n = 0;
    pthread_mutex_lock(&sc->lock);
    for (int i = 0; i < SC_MAX_BINS; i++) {
        if (sc->bins[i].active && fabs(sc->bins[i].f_hz - f_hz) < 1.0) {
            n = sc->bins[i].env_n;
            break;
        }
    }
    pthread_mutex_unlock(&sc->lock);
    return n;
}

int itila_sc_list_bins(ItilaSc *sc, double *f_hz_out, int max_out)
{
    int count = 0;
    pthread_mutex_lock(&sc->lock);
    for (int i = 0; i < SC_MAX_BINS && count < max_out; i++) {
        if (sc->bins[i].active)
            f_hz_out[count++] = sc->bins[i].f_hz;
    }
    pthread_mutex_unlock(&sc->lock);
    return count;
}

int itila_sc_list_bin_ages(ItilaSc *sc, int *ages_out, int max_out)
{
    if (!sc) return 0;
    pthread_mutex_lock(&sc->lock);
    int count = 0;
    for (int i = 0; i < SC_MAX_BINS && count < max_out; i++) {
        if (sc->bins[i].active)
            ages_out[count++] = (sc->total_samples - sc->bins[i].created_sample) > INT32_MAX
                                ? INT32_MAX : (int)(sc->total_samples - sc->bins[i].created_sample);
    }
    pthread_mutex_unlock(&sc->lock);
    return count;
}

/* ---- integrated decode: ready check + drain + itila_feed in C ---- */

void itila_sc_set_decoder(ItilaSc *sc,
                           void *(*create_fn)(int, double),
                           const char *(*feed_fn)(void*, const double*, int, double, double),
                           void (*free_fn)(void*),
                           double (*get_wpm_fn)(void*),
                           double ev_thresh) {
    pthread_mutex_lock(&sc->lock);
    sc->dec_create  = create_fn;
    sc->dec_feed    = feed_fn;
    sc->dec_free    = free_fn;
    sc->dec_get_wpm = get_wpm_fn;
    sc->dec_get_last_cost = NULL;  /* optional - set via itila_sc_set_cost_fn */
    sc->ev_thresh   = ev_thresh;
    pthread_mutex_unlock(&sc->lock);
}

/* Optional setter for the timing-cost API (added 2026-05-14).  Decoupled
 * from set_decoder so callers without the API can keep the old 5-arg
 * signature working.  When the cost fn is set, decode_ready populates
 * ScDecodeResult.cost; otherwise cost is sentinel 999.0. */
void itila_sc_set_cost_fn(ItilaSc *sc, double (*get_last_cost_fn)(void*)) {
    pthread_mutex_lock(&sc->lock);
    sc->dec_get_last_cost = get_last_cost_fn;
    pthread_mutex_unlock(&sc->lock);
}

/* itila2_snr_decoder on the C decode path: decode_ready feeds each path's power (itila_feed_pow) and
 * reports the SNR in 500 Hz in ScDecodeResult.snr500. Needs itila_sc_set_snr_marks first. */
void itila_sc_set_decoder_pow(ItilaSc *sc,
                              const char *(*feed_pow_fn)(void*, const double*, const double*, int, double, double),
                              double (*get_mark_pow_fn)(void*))
{
    pthread_mutex_lock(&sc->lock);
    sc->dec_feed_pow = feed_pow_fn;
    sc->dec_get_mark_pow = get_mark_pow_fn;
    pthread_mutex_unlock(&sc->lock);
}

typedef struct {
    double f_hz;
    double snr;      /* the scanner's reading: ranks copies */
    int    wpm;
    float  snr500;   /* itila2_snr_decoder: SNR in 500 Hz the spot reports, NAN if none (was padding) */
    char   text[256];
    double cost;     /* timing-cost confidence; 999.0 if no API available */
} ScDecodeResult;

int itila_sc_decode_ready(ItilaSc *sc, int window_samples,
                           ScDecodeResult *results, int max_results) {
    if (!sc->dec_create || !sc->dec_feed) return 0;
    if (window_samples <= 0 || window_samples > SC_ENV_CAP) return 0;

    /* Heap-allocated env-copy buffer.  VLA on stack would also work
     * (window_samples is typically 12000 -> 96 KB), but with up to 8
     * per-RX decode threads each in this function, we'd hit pthread
     * stack limits at unusual window sizes.  One heap allocation per
     * call is negligible relative to the dec_feed cost. */
    double *env_local = (double *)malloc((size_t)window_samples * sizeof(double));
    if (!env_local) return 0;
    pthread_mutex_lock(&sc->lock);
    double *pow_local = NULL;     /* itila2_snr_decoder: this window's power on one path; none: plain decode */
    if (sc->pow && sc->dec_feed_pow && sc->dec_get_mark_pow)
        pow_local = (double *)malloc((size_t)window_samples * sizeof(double));
    int n_results = 0;

    for (int bi = 0; bi < SC_MAX_BINS && n_results < max_results; bi++) {
        ScBin *b = &sc->bins[bi];
        if (!b->active || b->env_n < window_samples) continue;

        /* Lazily create decoder handles */
        if (!b->h100) b->h100 = sc->dec_create(200, 100.0);
        if (!b->h200) b->h200 = sc->dec_create(200, 200.0);

        while (b->env_n >= window_samples && n_results < max_results) {
            /* Snapshot bin state for result emission (b->snr_db, b->f_hz)
             * before we release the lock; the drain thread may update
             * these during the unlock window.  f_khz derived from the
             * SAME per-iteration snapshot so dec_feed and the emitted
             * result agree on the frequency even if a future code path
             * mutates b->f_hz mid-iteration (e.g. fine-tuning toward
             * carrier offset).  Squelch caught this latent inconsistency
             * in pre-deploy review 2026-05-24. */
            double snr_local = b->snr_db;
            double f_hz_local = b->f_hz;
            double f_khz = f_hz_local / 1000.0;
            void *handles[2] = { b->h100, b->h200 };

            /* Mark bin in-decode so the eviction sites in feed_iq won't
             * free our captured handles during the unlock window. */
            b->in_decode = 1;

            int bin_evicted = 0;
            int r_first = n_results;          /* this window's results, for the shared SNR below */
            double n0 = sc->n0;
            for (int p = 0; p < 2; p++) {
                if (!handles[p]) continue;
                if (n_results >= max_results) break;

                /* Copy env data to local buffer while under lock - drain
                 * thread will be writing to b->env100/env200 during the
                 * unlock window and we can't have dec_feed see torn data. */
                double *src = (p == 0) ? b->env100 : b->env200;
                memcpy(env_local, src, (size_t)window_samples * sizeof(double));
                if (pow_local)
                    pow_take(&sc->pow[bi], p, window_samples, pow_local);

                /* Release scanner lock for the expensive dec_feed call.
                 * Pre-fix this was held across 400 bins x 2 paths x ~30ms
                 * each = ~24 s lockout, causing the IQ ring to overflow
                 * (feedback_bin_saturation_ceiling.md, 2026-04-26).  With
                 * the lock released drain can run scanner_feed concurrently. */
                pthread_mutex_unlock(&sc->lock);
                const char *raw = pow_local
                    ? sc->dec_feed_pow(handles[p], env_local, pow_local, window_samples, f_khz, sc->ev_thresh)
                    : sc->dec_feed(handles[p], env_local, window_samples, f_khz, sc->ev_thresh);
                double snr500 = pow_local
                    ? itila_sc_snr_from_power(sc->dec_get_mark_pow(handles[p]), n0, p) : NAN;
                double cost = sc->dec_get_last_cost
                              ? sc->dec_get_last_cost(handles[p])
                              : 999.0;
                int wpm = (int)(sc->dec_get_wpm(handles[p]) + 0.5);
                pthread_mutex_lock(&sc->lock);

                /* After re-locking: bin must still be active (in_decode
                 * guard above prevents eviction, but defensive check
                 * doesn't hurt). */
                if (!b->active) { bin_evicted = 1; break; }

                if (raw && raw[0]) {
                    ScDecodeResult *r = &results[n_results];
                    r->f_hz = f_hz_local;
                    r->snr  = snr_local;
                    r->wpm  = wpm;
                    r->snr500 = (float)snr500;
                    r->cost = cost;
                    int len = strlen(raw);
                    if (len > 255) len = 255;
                    memcpy(r->text, raw, len);
                    r->text[len] = '\0';
                    /* Trim trailing whitespace */
                    while (len > 0 && (r->text[len-1] == ' ' || r->text[len-1] == '\n'))
                        r->text[--len] = '\0';
                    if (len > 0) n_results++;
                }
            }

            /* itila2_snr_decoder: a path without a reading takes the other path's (the 200 Hz one first) */
            float shared = NAN;
            for (int k = n_results - 1; k >= r_first; k--)
                if (isfinite(results[k].snr500)) { shared = results[k].snr500; break; }
            for (int k = r_first; k < n_results; k++)
                if (!isfinite(results[k].snr500)) results[k].snr500 = shared;

            /* Clear in-decode under lock before shifting envelope or
             * exiting this bin.  Eviction can resume on this bin
             * afterward if it's stale. */
            b->in_decode = 0;
            if (bin_evicted) break;

            /* Shift envelope buffer - drain window_samples.  Drain may
             * have appended more samples during the unlock window, so
             * we use the CURRENT b->env_n, not a pre-unlock cache. */
            int rem = b->env_n - window_samples;
            if (rem < 0) rem = 0;  /* defensive - shouldn't happen */
            memmove(b->env100, b->env100 + window_samples, rem * sizeof(double));
            if (sc->pow) pow_shift(&sc->pow[b - sc->bins], window_samples);
            memmove(b->env200, b->env200 + window_samples, rem * sizeof(double));
            b->env_n = rem;
        }
    }
    pthread_mutex_unlock(&sc->lock);
    free(env_local);
    free(pow_local);
    return n_results;
}

/* Clean up decoder handles when bins are evicted */
static void bin_free_decoders(ItilaSc *sc, ScBin *b) {
    if (sc->dec_free) {
        if (b->h100) { sc->dec_free(b->h100); b->h100 = NULL; }
        if (b->h200) { sc->dec_free(b->h200); b->h200 = NULL; }
    }
}
