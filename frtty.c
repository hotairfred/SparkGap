/* frtty.c — FRTTY, frame RTTY: a decoder that models the whole character frame (rtty_decoder "frtty").
 *
 * librtty (rtty_core.c) slices bits one by one and hunts for each start bit, so a fade on a start bit
 * loses the next characters. This decoder weighs whole frames instead (Mills, DSP-93):
 * 1. Per tone, a one-bit matched filter read K times a bit; soft value d = (mark - space) / (mark + space),
 *    free of the signal level, so a flat fade leaves it alone.
 * 2. A hidden Markov model of the frame: idle, start, 5 data bits each with its value, stop; small stay and
 *    skip arcs for timing drift. Every state gets one piece of evidence per bit (a character state at the
 *    step its filter matches the bit, idle at 1/K a step).
 * 3. Levels from the best path (Viterbi training): a level that follows fades, and the spread.
 * 4. The sender's cadence: RTTY software sends characters back to back (diddling LTRS when idle), with a
 *    fixed stop of 1, 1.5 (the ham standard) or 2 bits. Each is tried as a locked model, the next start
 *    right after the stop, and the best-fitting path kept.
 * 5. Baudot with LTRS / FIGS (unshift on space).
 * 6. Each over (a stretch of signal between pauses) on its own, with its own cadence (overs()).
 * In place of librtty on the JARTS and PA recordings: real calls 60 -> 65 and 91 -> 97, unconfirmed
 * 1 -> 3 and 5 -> 6 (hotairfred/SparkGap#155).
 *
 *   int frtty_decode(const double *audio, int n, int rate_hz, double center, char *out, int out_len)
 *   int frtty_decode_fsk(..., int *fsk_seconds)   also the seconds of clear FSK, as rtty_fsk_seconds()
 * audio: rate_hz, mark at center + 85 Hz, space at center - 85 Hz.  Returns the text length.
 */
#include <math.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#define K        8
#define BAUD     45.45
#define SHIFT    170.0
#define WIN      (12 * K)
#define NEG      (-1e300)
#define MAXP     16
#define MAXS     128

typedef struct {
    int ns, start0, data0, stop0, stoplen;
    int np[MAXS], pidx[MAXS][MAXP]; double plp[MAXS][MAXP];
    int kind[MAXS], sample[MAXS]; double weight[MAXS];
} Model;

static void arc(Model *m, int i, int j, double p)
{
    for (int q = 0; q < m->np[j]; q++)
        if (m->pidx[j][q] == i) { double a = m->plp[j][q], b = log(p); m->plp[j][q] = fmax(a, b) + log1p(exp(-fabs(a - b))); return; }
    if (m->np[j] >= MAXP) abort();
    m->pidx[j][m->np[j]] = i; m->plp[j][m->np[j]] = log(p); m->np[j]++;
}

static void chain(Model *m, int first, int len, const int *nx, const double *np, int nn)
{
    for (int k = 0; k < len - 1; k++) {
        int s = first + k;
        arc(m, s, s + 1, 0.98); arc(m, s, s, 0.01); arc(m, s, first + (k + 2 < len ? k + 2 : len - 1), 0.01);
    }
    int last = first + len - 1;
    for (int q = 0; q < nn; q++) arc(m, last, nx[q], np[q] * 0.99);
    arc(m, last, last, 0.01);
}

/* stop_lo == 0: loose (stop K..2K steps, then start / idle); else the stop is exactly stop_lo steps */
static void build(Model *m, int stop_lo)
{
    memset(m, 0, sizeof *m);
    enum { NOISE = 0, IDLE = 1 };
    m->start0 = 2; m->data0 = m->start0 + K; m->stop0 = m->data0 + 5 * 2 * K;
    m->stoplen = stop_lo ? stop_lo : 2 * K;
    m->ns = m->stop0 + m->stoplen;
    m->kind[NOISE] = 0; m->kind[IDLE] = 1;
    for (int k = 0; k < K; k++) m->kind[m->start0 + k] = -1;
    for (int b = 0; b < 5; b++) for (int v = 0; v < 2; v++) for (int k = 0; k < K; k++) m->kind[m->data0 + (b * 2 + v) * K + k] = v ? 1 : -1;
    for (int k = 0; k < m->stoplen; k++) m->kind[m->stop0 + k] = 1;
    for (int s = 0; s < m->ns; s++) {
        int ch = s >= m->start0;
        if (!ch) m->sample[s] = 1;
        else if (s >= m->stop0) m->sample[s] = (s - m->stop0) == K - 1;
        else m->sample[s] = ((s - m->start0) % K) == K - 1;
        m->weight[s] = ch ? (m->sample[s] ? 1.0 : 0.0) : 1.0 / K;
    }
    arc(m, NOISE, NOISE, 0.98); arc(m, NOISE, IDLE, 0.01); arc(m, NOISE, m->start0, 0.01);
    arc(m, IDLE, IDLE, 0.97); arc(m, IDLE, m->start0, 0.025); arc(m, IDLE, NOISE, 0.005);
    { int nx[2] = { m->data0, m->data0 + K }; double p[2] = { 0.5, 0.5 }; chain(m, m->start0, K, nx, p, 2); }
    for (int b = 0; b < 5; b++) for (int v = 0; v < 2; v++) {
        int first = m->data0 + (b * 2 + v) * K;
        if (b < 4) { int nx[2] = { m->data0 + ((b + 1) * 2) * K, m->data0 + ((b + 1) * 2 + 1) * K }; double p[2] = { 0.5, 0.5 }; chain(m, first, K, nx, p, 2); }
        else { int nx[1] = { m->stop0 }; double p[1] = { 1.0 }; chain(m, first, K, nx, p, 1); }
    }
    if (!stop_lo) {
        for (int k = 0; k < 2 * K - 1; k++) {
            int s = m->stop0 + k;
            if (k >= K - 1) { arc(m, s, m->start0, 0.25); arc(m, s, IDLE, 0.1); arc(m, s, s + 1, 0.6); arc(m, s, NOISE, 0.05); }
            else { arc(m, s, s + 1, 0.99); arc(m, s, s, 0.01); }
        }
        int last = m->stop0 + 2 * K - 1;
        arc(m, last, m->start0, 0.6); arc(m, last, IDLE, 0.35); arc(m, last, NOISE, 0.05);
    } else {                                       /* locked cadence: next start right after the stop */
        int nx[3] = { m->start0, IDLE, NOISE }; double p[3] = { 0.9, 0.08, 0.02 };
        chain(m, m->stop0, stop_lo, nx, p, 3);
    }
}

static int cmpd(const void *a, const void *b) { double x = *(const double *)a, y = *(const double *)b; return (x > y) - (x < y); }
static double percentile(const double *x, int n, double q)
{
    double *t = malloc(n * sizeof *t); memcpy(t, x, n * sizeof *t); qsort(t, n, sizeof *t, cmpd);
    double pos = (n - 1) * q; int i = (int)pos; double f = pos - i;
    double r = i + 1 < n ? t[i] * (1 - f) + t[i + 1] * f : t[n - 1]; free(t); return r;
}
static void ufilter(const double *x, double *y, int n, int w)     /* moving mean, reflect at the ends; O(n) */
{
    int left = w / 2, right = w - 1 - left;
    double s = 0.0;
    #define AT(j) x[(j) < 0 ? -(j) - 1 : ((j) >= n ? 2 * n - (j) - 1 : (j))]
    for (int j = -left; j <= right; j++) s += AT(j);
    for (int i = 0; i < n; i++) { y[i] = s / w; s += AT(i + right + 1) - AT(i - left); }
    #undef AT
}

/* Viterbi with emission per step built on the fly: kind * mu_t, spread sigma, weighted per bit */
static void viterbi(const Model *m, const double *d, const double *mu, double sigma, int T, int *path, short *back, double *sc, double *nsc)
{
    double ls = log(sigma), inv = 1.0 / sigma;
    for (int s = 0; s < m->ns; s++) {
        double z = (d[0] - m->kind[s] * mu[0]) * inv;
        sc[s] = (s == 1 ? 0.0 : NEG) + (s == 0 ? -1e9 : (-0.5 * z * z - ls) * m->weight[s]);
    }
    for (int t = 1; t < T; t++) {
        for (int j = 0; j < m->ns; j++) {
            double best = NEG; int bi = 0;
            for (int q = 0; q < m->np[j]; q++) { double v = sc[m->pidx[j][q]] + m->plp[j][q]; if (v > best) { best = v; bi = m->pidx[j][q]; } }
            double e;
            if (j == 0) e = -1e9;
            else if (m->weight[j] == 0.0) e = 0.0;
            else { double z = (d[t] - m->kind[j] * mu[t]) * inv; e = (-0.5 * z * z - ls) * m->weight[j]; }
            nsc[j] = best + e; back[(size_t)t * MAXS + j] = (short)bi;
        }
        memcpy(sc, nsc, m->ns * sizeof *sc);
    }
    int bs = 1; for (int s = 1; s < m->ns; s++) if (sc[s] > sc[bs]) bs = s;
    path[T - 1] = bs;
    for (int t = T - 1; t > 0; t--) path[t - 1] = back[(size_t)t * MAXS + path[t]];
}

/* Viterbi training: levels from the sampled steps of the best path */
static void relearn(const Model *m, const double *d, const int *path, int T, double *mu, double *sigma, double *num, double *den, double *tmp)
{
    for (int t = 0; t < T; t++) {
        int s = path[t]; double w = m->weight[s]; int k = m->kind[s];
        num[t] = w * k * d[t]; den[t] = w * k * k;
    }
    ufilter(num, tmp, T, WIN); memcpy(num, tmp, T * sizeof *tmp);
    ufilter(den, tmp, T, WIN); memcpy(den, tmp, T * sizeof *tmp);
    for (int t = 0; t < T; t++) { double v = num[t] / fmax(den[t], 1e-9); mu[t] = den[t] > 1e-6 ? fmin(1.0, fmax(0.15, v)) : mu[t]; }
    double ws = 0, wr = 0;
    for (int t = 0; t < T; t++) { int s = path[t]; if (m->kind[s] && m->weight[s] > 0) { double r = d[t] - m->kind[s] * mu[t]; ws += m->weight[s]; wr += m->weight[s] * r * r; } }
    if (ws > 4) *sigma = fmin(0.6, fmax(0.08, sqrt(wr / ws)));
}

static const char LTRS[] = "\0E\nA SIU\rDRJNFCKTZLWHYPQOBG\0MXV\0";
static const char FIGS[] = "\0" "3\n- \a87\r$4',!:(5\")2#6019?&\0./;\0";

static int text_out(const Model *m, const int *path, int T, char *out, int out_len)
{
    int bits[5], prev = -1, figs = 0, o = 0;
    for (int b = 0; b < 5; b++) bits[b] = -1;
    for (int t = 0; t < T; t++) {
        int s = path[t];
        if (s >= m->data0 && s < m->stop0) { int r = (s - m->data0) / K; bits[r / 2] = r % 2; }
        if (s == m->stop0 && s != prev) {
            int ok = 1, c = 0;
            for (int b = 0; b < 5; b++) { if (bits[b] < 0) ok = 0; else c |= bits[b] << b; }
            if (ok) {
                if (c == 0x1F) figs = 0;
                else if (c == 0x1B) figs = 1;
                else { if (c == 0x04) figs = 0; char ch = figs ? FIGS[c] : LTRS[c];
                       if (ch && o < out_len - 1) out[o++] = (ch == '\r' || ch == '\n') ? ' ' : ch; }
            }
            for (int b = 0; b < 5; b++) bits[b] = -1;
        }
        prev = s;
    }
    int a0 = 0; while (a0 < o && (out[a0] == ' ' || out[a0] == '\a')) a0++;
    while (o > a0 && (out[o - 1] == ' ' || out[o - 1] == '\a')) o--;
    memmove(out, out + a0, o - a0); o -= a0; out[o] = 0;
    return o;
}

static Model loose, locked[2 * K + 1];                  /* read-only once built: every thread shares them */
static pthread_once_t models_once = PTHREAD_ONCE_INIT;
static void build_models(void)
{
    build(&loose, 0);
    for (int L = K; L <= 2 * K; L++) build(&locked[L], L);
}

/* One over (or the whole minute): the loose model, then the three locked cadences; the best path's text. */
static int decode_d(const double *d, int T, char *out, int out_len)
{
    pthread_once(&models_once, build_models);            /* once, even when two threads start together */
    if (T < 4 * K || out_len < 1) { if (out_len) out[0] = 0; return 0; }
    double *a = malloc(T * sizeof *a); for (int t = 0; t < T; t++) a[t] = fabs(d[t]);
    double mu0 = fmax(0.3, percentile(a, T, 0.80)), sigma = 0.3;
    double *mu = malloc(T * sizeof *mu); for (int t = 0; t < T; t++) mu[t] = mu0;
    double *num = malloc(T * sizeof *num), *den = malloc(T * sizeof *den), *tmp = malloc(T * sizeof *tmp);
    int *path = malloc(T * sizeof *path); short *back = malloc((size_t)T * MAXS * sizeof *back);
    double sc[MAXS], nsc[MAXS];
    viterbi(&loose, d, mu, sigma, T, path, back, sc, nsc);
    relearn(&loose, d, path, T, mu, &sigma, num, den, tmp);
    viterbi(&loose, d, mu, sigma, T, path, back, sc, nsc);
    const Model *m = &loose;
    {
        /* try the three stop lengths a sender uses (1, 1.5, 2 bits); keep the one the audio fits best */
        static const int Ls[3] = { K, K + K / 2, 2 * K };
        double best = NEG; int bl = -1;
        int *pbest = malloc(T * sizeof *pbest);
        double *mu_save = malloc(T * sizeof *mu_save); memcpy(mu_save, mu, T * sizeof *mu);
        double sig_save = sigma;
        for (int q = 0; q < 3; q++) {
            const Model *mq = &locked[Ls[q]];
            memcpy(mu, mu_save, T * sizeof *mu); double sg = sig_save;
            viterbi(mq, d, mu, sg, T, path, back, sc, nsc);
            relearn(mq, d, path, T, mu, &sg, num, den, tmp);
            viterbi(mq, d, mu, sg, T, path, back, sc, nsc);
            double fs = NEG; for (int s = 1; s < mq->ns; s++) if (sc[s] > fs) fs = sc[s];
            if (fs > best) { best = fs; bl = q; memcpy(pbest, path, T * sizeof *path); }
        }
        if (bl >= 0) { m = &locked[Ls[bl]]; memcpy(path, pbest, T * sizeof *path); }
        free(pbest); free(mu_save);
    }
    int o = text_out(m, path, T, out, out_len);
    free(a); free(mu); free(num); free(den); free(tmp); free(path); free(back);
    return o;
}

/* Overs: the steps where the two tones together stand clear of the pauses between transmissions.
 * In a pause the soft value is noise, and a frame model has to read it as letters (CQK, AB1EXPCU), glued
 * to the call next to it; and a runner and a caller in one minute may send different stops (1.5 and 2
 * bits). So each over is decoded on its own, with its own cadence, and the overs joined by a space.
 * A minute with no clear pauses (one long over, or nothing but noise) stays one piece. */
#define SEG_MIN  (8 * K)          /* an over: at least 8 bits */
#define GAP_MIN  (3 * K)          /* a pause: at least 3 bits (a stop and a start are 2.5) */
static int overs(const double *lvl, int T, int *lo, int *hi, int maxn)
{
    double *g = malloc(T * sizeof *g), *sm = malloc(T * sizeof *sm);
    for (int t = 0; t < T; t++) g[t] = log(fmax(lvl[t], 1e-12));
    ufilter(g, sm, T, K);                                  /* one bit: a space bit keeps the level up */
    double p10 = percentile(sm, T, 0.10), p90 = percentile(sm, T, 0.90);
    int n = 0;
    if (p90 - p10 < log(4.0)) { free(g); free(sm); return 0; }   /* under 12 dB: no pauses to find */
    double thr = p10 + 0.35 * (p90 - p10);
    int t = 0;
    while (t < T && n < maxn) {
        while (t < T && sm[t] < thr) t++;
        int a = t, quiet = 0, b = t;
        while (t < T) {
            if (sm[t] < thr) { if (++quiet >= GAP_MIN) break; } else { quiet = 0; b = t + 1; }
            t++;
        }
        if (b - a >= SEG_MIN) { lo[n] = a > K ? a - K : 0; hi[n] = b + K < T ? b + K : T; n++; }
    }
    free(g); free(sm);
    return n;
}

/* frtty_decode, and in *fsk_seconds (when not NULL) the seconds in which mark and space clearly alternated
 * (correlation below -0.3 over one second), as librtty's rtty_fsk_seconds: RTTY keys one tone or the other,
 * two CW signals 170 Hz apart key independently (rtty_excludes_cw). */
int frtty_decode_fsk(const double *audio, int n, int rate_hz, double center, char *out, int out_len, int *fsk_seconds)
{
    double rate = rate_hz;
    double spb = rate / BAUD, step = spb / K; int L = (int)lround(spb), T = (int)(n / step);
    if (fsk_seconds) *fsk_seconds = 0;
    if (T < 4 * K || out_len < 1) { if (out_len) out[0] = 0; return 0; }
    double *d = malloc(T * sizeof *d), *lvl = malloc(T * sizeof *lvl);
    double cs1 = 0, cm1 = 0, cs2 = 0, cm2 = 0, cms = 0; int cn = 0, per_s = (int)lround(rate / step);
    {
        double fm = center + SHIFT / 2, fs = center - SHIFT / 2;
        double cm = cos(2 * M_PI * fm / rate), sm = sin(2 * M_PI * fm / rate), cs = cos(2 * M_PI * fs / rate), ss = sin(2 * M_PI * fs / rate);
        double ar = 1, ai = 0, br = 1, bi = 0;                     /* oscillators exp(-i w t) */
        double *mr = malloc(n * sizeof *mr), *mi = malloc(n * sizeof *mi), *sr = malloc(n * sizeof *sr), *si = malloc(n * sizeof *si);
        for (int i = 0; i < n; i++) {
            mr[i] = audio[i] * ar; mi[i] = audio[i] * ai; sr[i] = audio[i] * br; si[i] = audio[i] * bi;
            double t1 = ar * cm + ai * sm; ai = ai * cm - ar * sm; ar = t1;
            double t2 = br * cs + bi * ss; bi = bi * cs - br * ss; br = t2;
            if ((i & 1023) == 1023) { double g = 1 / hypot(ar, ai), h = 1 / hypot(br, bi); ar *= g; ai *= g; br *= h; bi *= h; }
        }
        double cmr = 0, cmi = 0, csr = 0, csi = 0; int ti = 0;
        for (int i = 0; i < n && ti < T; i++) {
            cmr += mr[i]; cmi += mi[i]; csr += sr[i]; csi += si[i];
            if (i >= L) { cmr -= mr[i - L]; cmi -= mi[i - L]; csr -= sr[i - L]; csi -= si[i - L]; }
            while (ti < T && (long)(ti * step) == i) {
                double m = i >= L - 1 ? hypot(cmr, cmi) / L : 0.0, s = i >= L - 1 ? hypot(csr, csi) / L : 0.0;
                d[ti] = (m - s) / fmax(m + s, 1e-12); lvl[ti] = m + s; ti++;
                cm1 += m; cs1 += s; cm2 += m * m; cs2 += s * s; cms += m * s;
                if (++cn >= per_s) {
                    double mm = cm1 / cn, sm_ = cs1 / cn, mv = cm2 / cn - mm * mm, sv = cs2 / cn - sm_ * sm_;
                    double den = sqrt(fmax(mv, 0) * fmax(sv, 0));
                    if (fsk_seconds && den > 1e-12 && (cms / cn - mm * sm_) / den < -0.3) (*fsk_seconds)++;
                    cm1 = cs1 = cm2 = cs2 = cms = 0; cn = 0;
                }
            }
        }
        for (; ti < T; ti++) { d[ti] = 0.0; lvl[ti] = 0.0; }
        free(mr); free(mi); free(sr); free(si);
    }
    int maxn = T / SEG_MIN + 1, *lo = malloc(maxn * sizeof *lo), *hi = malloc(maxn * sizeof *hi);
    int ns = overs(lvl, T, lo, hi, maxn), o = 0;
    if (ns == 0) o = decode_d(d, T, out, out_len);
    else
        for (int k = 0; k < ns && o < out_len - 2; k++) {
            if (o) out[o++] = ' ';
            o += decode_d(d + lo[k], hi[k] - lo[k], out + o, out_len - o);
        }
    out[o] = 0;
    free(lo); free(hi); free(d); free(lvl);
    return o;
}

int frtty_decode(const double *audio, int n, int rate_hz, double center, char *out, int out_len)
{
    return frtty_decode_fsk(audio, n, rate_hz, center, out, out_len, NULL);
}
