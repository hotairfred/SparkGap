/*
 * rtty_core.c — RTTY decoder for SparkGap
 *
 * Goertzel dual-tone detection → AM envelope → bit recovery → Baudot
 * Based on Grayline's design doc (rtty_decoder_design.md)
 *
 * API matches libitila.so pattern:
 *   rtty_create(sample_rate, center_freq) → handle
 *   rtty_feed(handle, audio, n, confidence) → decoded text
 *   rtty_free(handle)
 *
 * Compile:
 *   gcc -shared -O2 -fPIC -o librtty.so rtty_core.c -lm
 */

#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* ---- RTTY parameters ---- */
#define RTTY_SHIFT    170.0    /* Hz between mark and space */
#define RTTY_BAUD     45.45   /* Baudot baud rate */
#define RTTY_OVERSAMP 4       /* bit clock oversampling factor */
#define RTTY_BITS_PER_CHAR 7  /* start(1) + data(5) + stop(1.5) ≈ 7 */
#define RESULT_BUF    512
#define GOERTZEL_N    512     /* Sliding Goertzel window — ~23 Hz resolution at 12 kHz */

/* Baudot/ITA2 tables */
static const char LTRS[] = "\0E\nA SIU\rDRJNFCKTZLWHYPQOBG\0MXV\0";
/*                            0 1  2 3 4 5 6 7  8  9 A B  C D E  F 10 11 12 13 14 15 16 17 18 19 1A 1B 1C 1D 1E 1F */
static const char FIGS[] = {  0,'3','\n','-',' ','\a','8','7','\r','$','4','\'',',','!',':','(','5','"',')','2','#','6','0','1','9','?','&', 0,'.','/',';', 0 };
#define BAUDOT_LTRS 0x1F
#define BAUDOT_FIGS 0x1B
#define BAUDOT_SPACE 0x04

typedef struct {
    int    sample_rate;
    double mark_freq;      /* mark tone frequency (Hz) */
    double space_freq;     /* space tone frequency (Hz) */

    /* Envelope state */
    double mark_env;       /* filtered mark envelope */
    double space_env;      /* filtered space envelope */
    /* Automatic threshold correction (W7AY): each tone's own peak and floor */
    double mark_max, mark_min, space_max, space_min;
    double atc_attack, atc_decay;   /* fast follow toward a new peak/floor, slow release */

    /* Bit recovery */
    double data_lpf;       /* bipolar data signal after LPF */
    double data_alpha;     /* data LPF coefficient */
    int    samples_per_bit;/* samples per bit period */

    /* Baudot state */
    int    shift;          /* 0=LTRS, 1=FIGS */
    int    bit_buf;        /* accumulating bits for current character */
    int    bit_count;      /* bits received in current character */
    int    in_char;        /* currently receiving a character */
    double soft_bits[5];   /* soft decision per data bit */

    /* RTTY detection */
    double corr_sum;       /* running mark/space correlation */
    double mark_sum, space_sum;
    double mark_sq, space_sq;
    int    corr_n;
    int    active;         /* RTTY signal detected */

    /* Output */
    char   result_buf[RESULT_BUF];
    int    result_len;
    double confidence;     /* average bit confidence */
    double prev_data;      /* previous LPF value for transition detection */
    /* Matched filters: each tone mixed to 0 Hz and summed over one bit (ring of nb) */
    int    nb, mf_pos;
    double *mf_mr, *mf_mi, *mf_sr, *mf_si;      /* ring of the mixed samples */
    double mf_msr, mf_msi, mf_ssr, mf_ssi;      /* running sums */
    double mf_pm_r, mf_pm_i, mf_ps_r, mf_ps_i;  /* tone phasors, rotated each sample */
    double mf_rm_r, mf_rm_i, mf_rs_r, mf_rs_i;  /* per-sample rotation */
    double spb;            /* samples per bit, fractional */
    double tc, next_t;     /* samples since the start edge; when the next bit is sampled */
    int    framing_errors; /* characters dropped for a space stop bit */
} rtty_state_t;

typedef void *rtty_handle_t;

/* ---- Goertzel coefficient for target frequency ---- */
static double goertzel_coeff(double freq, int sample_rate, int n) {
    double k = 0.5 + (double)n * freq / sample_rate;
    return 2.0 * cos(2.0 * M_PI * k / n);
}

/* ---- API ---- */

rtty_handle_t rtty_create(int sample_rate, double center_freq) {
    rtty_state_t *st = (rtty_state_t *)calloc(1, sizeof(rtty_state_t));
    if (!st) return NULL;

    st->sample_rate = sample_rate;
    st->mark_freq = center_freq + RTTY_SHIFT / 2.0;  /* mark is higher */
    st->space_freq = center_freq - RTTY_SHIFT / 2.0;


    double bit_period = 1.0 / RTTY_BAUD;

    /* Data LPF: cutoff at baud rate (wider for better bit tracking) */
    double data_tc = 1.0 / (RTTY_BAUD * 2.0 * M_PI);
    st->data_alpha = 1.0 - exp(-1.0 / (data_tc * sample_rate));

    st->samples_per_bit = (int)(sample_rate / RTTY_BAUD + 0.5);
    st->spb = sample_rate / RTTY_BAUD;
    st->nb = (int)(st->spb + 0.5);
    st->mf_mr = calloc(st->nb, sizeof(double)); st->mf_mi = calloc(st->nb, sizeof(double));
    st->mf_sr = calloc(st->nb, sizeof(double)); st->mf_si = calloc(st->nb, sizeof(double));
    if (!st->mf_mr || !st->mf_mi || !st->mf_sr || !st->mf_si) {
        free(st->mf_mr); free(st->mf_mi); free(st->mf_sr); free(st->mf_si); free(st);
        return NULL;
    }
    st->mf_pm_r = st->mf_ps_r = 1.0;
    st->mf_rm_r = cos(2.0 * M_PI * st->mark_freq / sample_rate);
    st->mf_rm_i = -sin(2.0 * M_PI * st->mark_freq / sample_rate);
    st->mf_rs_r = cos(2.0 * M_PI * st->space_freq / sample_rate);
    st->mf_rs_i = -sin(2.0 * M_PI * st->space_freq / sample_rate);
    /* ATC: peaks and floors follow a new level in half a bit and release over 64 bits
     * (1.4 s). Tuned on an RTTY contest recording: 0.5/64, 1/256 and 2/256 bits gave
     * 117, 117 and 112 RBN-confirmed calls; 0.5/64 kept the fewest unconfirmed. */
    st->atc_attack = 1.0 - exp(-1.0 / (0.5 * bit_period * sample_rate));
    st->atc_decay  = 1.0 - exp(-1.0 / (64.0 * bit_period * sample_rate));

    return (rtty_handle_t)st;
}

const char *rtty_feed(rtty_handle_t h, const double *audio, int n,
                       double *confidence_out) {
    rtty_state_t *st = (rtty_state_t *)h;
    st->result_len = 0;
    st->result_buf[0] = '\0';

    for (int i = 0; i < n; i++) {
        double x = audio[i];

        /* Matched filters (the optimum non-coherent FSK detector): each tone mixed
         * to 0 Hz and summed over exactly one bit; the magnitude is the tone's energy
         * in the last bit. Rejects a neighbour 200-300 Hz away far better than the
         * 60 Hz resonators did, and integrates noise over the bit. */
        double mr = x * st->mf_pm_r, mi = x * st->mf_pm_i;
        double sr = x * st->mf_ps_r, si = x * st->mf_ps_i;
        double t;
        t = st->mf_pm_r * st->mf_rm_r - st->mf_pm_i * st->mf_rm_i;
        st->mf_pm_i = st->mf_pm_r * st->mf_rm_i + st->mf_pm_i * st->mf_rm_r; st->mf_pm_r = t;
        t = st->mf_ps_r * st->mf_rs_r - st->mf_ps_i * st->mf_rs_i;
        st->mf_ps_i = st->mf_ps_r * st->mf_rs_i + st->mf_ps_i * st->mf_rs_r; st->mf_ps_r = t;
        if (st->mf_pos == 0) {            /* keep the phasors on the unit circle */
            double gm = 1.0 / sqrt(st->mf_pm_r * st->mf_pm_r + st->mf_pm_i * st->mf_pm_i);
            double gs = 1.0 / sqrt(st->mf_ps_r * st->mf_ps_r + st->mf_ps_i * st->mf_ps_i);
            st->mf_pm_r *= gm; st->mf_pm_i *= gm; st->mf_ps_r *= gs; st->mf_ps_i *= gs;
        }
        int k = st->mf_pos;
        st->mf_msr += mr - st->mf_mr[k]; st->mf_msi += mi - st->mf_mi[k];
        st->mf_ssr += sr - st->mf_sr[k]; st->mf_ssi += si - st->mf_si[k];
        st->mf_mr[k] = mr; st->mf_mi[k] = mi; st->mf_sr[k] = sr; st->mf_si[k] = si;
        st->mf_pos = (k + 1) % st->nb;
        double mark_amp = sqrt(st->mf_msr * st->mf_msr + st->mf_msi * st->mf_msi) / st->nb;
        double space_amp = sqrt(st->mf_ssr * st->mf_ssr + st->mf_ssi * st->mf_ssi) / st->nb;
        st->mark_env = mark_amp;
        st->space_env = space_amp;

        /* RTTY detection */
        st->mark_sum += mark_amp;
        st->space_sum += space_amp;
        st->mark_sq += mark_amp * mark_amp;
        st->space_sq += space_amp * space_amp;
        st->corr_sum += mark_amp * space_amp;
        st->corr_n++;
        if (st->corr_n >= st->sample_rate) {  /* check every ~1 second */
            double mm = st->mark_sum / st->corr_n;
            double sm = st->space_sum / st->corr_n;
            double mv = st->mark_sq / st->corr_n - mm * mm;
            double sv = st->space_sq / st->corr_n - sm * sm;
            double cov = st->corr_sum / st->corr_n - mm * sm;
            double denom = sqrt((mv > 0 ? mv : 1e-20) * (sv > 0 ? sv : 1e-20));
            double corr = cov / denom;
            st->active = (corr < -0.2) || st->active;  /* latch on */
            st->mark_sum = st->space_sum = 0;
            st->mark_sq = st->space_sq = 0;
            st->corr_sum = 0;
            st->corr_n = 0;
        }

        /* Always try to decode (don't gate on active for now) */

        /* ATC (W7AY optimal ATC): track each tone's peak and floor, clip each envelope
         * to its own range and decide on the two relative to their own spans, so one
         * tone fading (selective fading) does not move the decision threshold. */
        double me = st->mark_env, se = st->space_env;
        #define ATC_TRACK(v, mx, mn) do { \
            mx += ((v) > mx ? st->atc_attack : st->atc_decay) * ((v) - mx); \
            mn += ((v) < mn ? st->atc_attack : st->atc_decay) * ((v) - mn); } while (0)
        ATC_TRACK(me, st->mark_max, st->mark_min);
        ATC_TRACK(se, st->space_max, st->space_min);
        #undef ATC_TRACK
        double mc = fmin(fmax(me, st->mark_min), st->mark_max);
        double sc = fmin(fmax(se, st->space_min), st->space_max);
        double bipolar = (mc - st->mark_min) - (sc - st->space_min)
                       - 0.5 * ((st->mark_max - st->mark_min) - (st->space_max - st->space_min));
        st->data_lpf += st->data_alpha * (bipolar - st->data_lpf);

        /* Character timing as a UART: a mark-to-space edge while idle starts a
         * character; the start bit is checked at its centre (half a bit later), the
         * five data bits are sampled at their centres, and the character is kept only
         * when the stop bit is mark. Each character re-times from its own start edge,
         * so clock error cannot build up, and a misread start or a space stop bit
         * drops the character instead of printing a wrong letter. */
        double v = st->data_lpf;
        if (!st->in_char) {
            if (st->prev_data >= 0 && v < 0) {
                st->in_char = 1;
                st->bit_count = -1;            /* -1: start bit, 0..4: data, 5: stop */
                st->bit_buf = 0;
                st->tc = 0.0;
                st->next_t = 0.5 * st->spb;
            }
        } else {
            st->tc += 1.0;
            if (st->tc >= st->next_t) {
                int bit = (v >= 0) ? 1 : 0;
                st->next_t += st->spb;
                if (st->bit_count == -1) {
                    if (bit) st->in_char = 0;  /* a glitch, not a start bit */
                    else st->bit_count = 0;
                } else if (st->bit_count < 5) {
                    st->bit_buf |= (bit << st->bit_count);
                    st->soft_bits[st->bit_count] = v;
                    st->bit_count++;
                } else {
                    st->in_char = 0;
                    if (!bit) {
                        st->framing_errors++;  /* space stop bit: drop it */
                    } else {
                        int code = st->bit_buf & 0x1F;
                        if (code == BAUDOT_SPACE) {
                            st->shift = 0;  /* UOS */
                            if (st->result_len < RESULT_BUF - 1)
                                st->result_buf[st->result_len++] = ' ';
                        }
                        else if (code == BAUDOT_LTRS) st->shift = 0;
                        else if (code == BAUDOT_FIGS) st->shift = 1;
                        else {
                            char ch = st->shift ? FIGS[code] : LTRS[code];
                            if (ch > 0 && ch != '\r' && ch != '\a' &&
                                st->result_len < RESULT_BUF - 1) {
                                st->result_buf[st->result_len++] = ch;
                            }
                        }
                        double conf = 0;
                        for (int k = 0; k < 5; k++)
                            conf += fabs(st->soft_bits[k]);
                        st->confidence = conf / 5.0;
                    }
                }
            }
        }
        st->prev_data = v;
    }

    st->result_buf[st->result_len] = '\0';
    if (confidence_out) *confidence_out = st->confidence;
    return st->result_buf;
}

void rtty_free(rtty_handle_t h) {
    rtty_state_t *st0 = (rtty_state_t *)h;
    if (st0) { free(st0->mf_mr); free(st0->mf_mi); free(st0->mf_sr); free(st0->mf_si); }
    free(h);
}

/* For testing */
int rtty_is_active(rtty_handle_t h) {
    return ((rtty_state_t *)h)->active;
}
