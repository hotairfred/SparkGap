/*
 * rtty_core.c — RTTY decoder for OpenSkimmer
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
static const char FIGS[] = {  0,'3','\n','-',' ','\a','8','7','\r', 0,'$','4','\'',',','!',':','(','5','"',')','2','#','6','0','1','9','?','&', 0,'.','/','=', 0 };
#define BAUDOT_LTRS 0x1F
#define BAUDOT_FIGS 0x1B
#define BAUDOT_SPACE 0x04

typedef struct {
    int    sample_rate;
    double mark_freq;      /* mark tone frequency (Hz) */
    double space_freq;     /* space tone frequency (Hz) */

    /* Resonator bandpass for mark and space tones (2nd order IIR) */
    double mark_bp_s1, mark_bp_s2;
    double space_bp_s1, space_bp_s2;
    double mark_bp_a1, mark_bp_b0;  /* resonator coefficients */
    double space_bp_a1, space_bp_b0;
    double mark_bp_r;               /* pole radius */
    double space_bp_r;

    /* Envelope state */
    double mark_env;       /* filtered mark envelope */
    double space_env;      /* filtered space envelope */
    double env_alpha;      /* envelope LPF coefficient */

    /* Bit recovery */
    double data_lpf;       /* bipolar data signal after LPF */
    double data_alpha;     /* data LPF coefficient */
    int    bit_counter;    /* sample counter within current bit */
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

    /* Resonator bandpass filters for mark and space tones.
     * 2nd-order IIR resonator: y[n] = x[n] - r²·x[n-2] + 2r·cos(w)·y[n-1] - r²·y[n-2]
     * Bandwidth ≈ (1-r) × sample_rate / π */
    double bw = 60.0;  /* ~60 Hz bandwidth per tone (1.3 × baud) */
    double r = 1.0 - M_PI * bw / sample_rate;
    if (r > 0.999) r = 0.999;
    if (r < 0.9) r = 0.9;

    st->mark_bp_r = r;
    st->mark_bp_a1 = 2.0 * r * cos(2.0 * M_PI * st->mark_freq / sample_rate);
    st->mark_bp_b0 = 1.0 - r;

    st->space_bp_r = r;
    st->space_bp_a1 = 2.0 * r * cos(2.0 * M_PI * st->space_freq / sample_rate);
    st->space_bp_b0 = 1.0 - r;

    /* Envelope LPF: ~150 Hz cutoff (tc ≈ 1 ms). Must be FAST enough to
     * track bit transitions cleanly — old value (tc = half bit period =
     * 132 samples at 12 kHz) needed 5×tc ≈ 2.5 bit periods to settle,
     * which smeared every transition across neighboring bits → massive
     * ISI → garbled output. 150 Hz is well above the bit rate (45 baud)
     * but well below the rectified-carrier ripple from the bandpass
     * output, so it removes ripple without smearing bits. */
    double env_tc = 1.0 / (2.0 * M_PI * 150.0);  /* ~1.06 ms */
    st->env_alpha = 1.0 - exp(-1.0 / (env_tc * sample_rate));

    /* Data LPF: cutoff at baud rate (wider for better bit tracking) */
    double data_tc = 1.0 / (RTTY_BAUD * 2.0 * M_PI);
    st->data_alpha = 1.0 - exp(-1.0 / (data_tc * sample_rate));

    st->samples_per_bit = (int)(sample_rate / RTTY_BAUD + 0.5);

    return (rtty_handle_t)st;
}

const char *rtty_feed(rtty_handle_t h, const double *audio, int n,
                       double *confidence_out) {
    rtty_state_t *st = (rtty_state_t *)h;
    st->result_len = 0;
    st->result_buf[0] = '\0';

    for (int i = 0; i < n; i++) {
        double x = audio[i];

        /* Resonator bandpass — per-sample output */
        double m_out = st->mark_bp_b0 * x + st->mark_bp_a1 * st->mark_bp_s1
                     - st->mark_bp_r * st->mark_bp_r * st->mark_bp_s2;
        st->mark_bp_s2 = st->mark_bp_s1;
        st->mark_bp_s1 = m_out;

        double s_out = st->space_bp_b0 * x + st->space_bp_a1 * st->space_bp_s1
                     - st->space_bp_r * st->space_bp_r * st->space_bp_s2;
        st->space_bp_s2 = st->space_bp_s1;
        st->space_bp_s1 = s_out;

        /* AM envelope: rectify + LPF */
        double mark_amp = fabs(m_out);
        double space_amp = fabs(s_out);
        st->mark_env += st->env_alpha * (mark_amp - st->mark_env);
        st->space_env += st->env_alpha * (space_amp - st->space_env);

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

        /* Bipolar data signal + LPF */
        double bipolar = st->mark_env - st->space_env;
        st->data_lpf += st->data_alpha * (bipolar - st->data_lpf);

        /* Bit clock: count samples, detect transitions */
        st->bit_counter++;

        /* Hard reset to 0 on every zero-crossing. The fast envelope LPF
         * (commit f97fb5b) detects transitions ~12 samples after the
         * actual bit boundary — well within mid-bit sampling tolerance
         * (we sample at counter == 132). The previous soft-pull DPLL
         * (commit 5d270e9) reacted to noise-induced zero-crossings and
         * destabilized the clock instead of locking it. The hard reset
         * trusts that the data_lpf (smoothed at tc=42) suppresses
         * within-bit glitches enough that real transitions dominate. */
        if ((st->prev_data >= 0 && st->data_lpf < 0) ||
            (st->prev_data < 0 && st->data_lpf >= 0)) {
            st->bit_counter = 0;
        }
        st->prev_data = st->data_lpf;

        /* Sample at mid-bit */
        if (st->bit_counter == st->samples_per_bit / 2) {
            int bit = (st->data_lpf >= 0) ? 1 : 0;
            double soft = st->data_lpf;

            if (!st->in_char) {
                if (bit == 0) {  /* start bit */
                    st->in_char = 1;
                    st->bit_count = 0;
                    st->bit_buf = 0;
                }
            } else {
                st->bit_count++;
                if (st->bit_count <= 5) {
                    st->bit_buf |= (bit << (st->bit_count - 1));
                    st->soft_bits[st->bit_count - 1] = soft;
                } else if (st->bit_count >= 7) {
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
                    for (int b = 0; b < 5; b++)
                        conf += fabs(st->soft_bits[b]);
                    st->confidence = conf / 5.0;
                    st->in_char = 0;
                }
            }
        }

        if (st->bit_counter >= st->samples_per_bit)
            st->bit_counter = 0;
    }

    st->result_buf[st->result_len] = '\0';
    if (confidence_out) *confidence_out = st->confidence;
    return st->result_buf;
}

void rtty_free(rtty_handle_t h) {
    free(h);
}

/* For testing */
int rtty_is_active(rtty_handle_t h) {
    return ((rtty_state_t *)h)->active;
}
