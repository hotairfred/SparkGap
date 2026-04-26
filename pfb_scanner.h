/*
 * pfb_scanner.h — Polyphase channelizer-backed band scanner.
 *
 * Mirrors itila_scanner's API exactly so the Python wrapper can swap
 * between the two without any other code change.  The difference is
 * internal: instead of N per-bin NCO+FIR chains running at the full
 * 192 kHz IQ rate, this version runs ONE polyphase channelizer that
 * produces all N narrowband channels at once, then per active bin only
 * does a tiny fine-tune mix + envelope at the (low) channelizer output
 * rate.
 *
 * Build (depends on cw_pfb.cpp + FFTW3f):
 *   g++ -O3 -march=native -ffast-math -shared -fPIC \
 *       -o libpfb_scanner.so pfb_scanner.c cw_pfb.cpp -lfftw3f -lm
 */

#ifndef PFB_SCANNER_H
#define PFB_SCANNER_H

#ifdef __cplusplus
extern "C" {
#endif

typedef struct PfbSc PfbSc;

/*
 * Create scanner.  Same signature as itila_sc_create so the Python
 * loader can drop this in with no parameter changes.
 *   sos*_flat / n_sos are accepted for compat but ignored — the PFB
 *   already provides the channel selectivity (nothing to filter after).
 */
PfbSc *pfb_sc_create(int sample_rate, double center_hz,
                     int max_bins, double min_snr,
                     int window_samples, int energy_win,
                     double grid_hz,
                     double band_min_hz, double band_max_hz,
                     const double *sos100_flat, int n_sos,
                     const double *sos200_flat);

void pfb_sc_free(PfbSc *sc);

void pfb_sc_feed_iq(PfbSc *sc,
                    const double *i_arr, const double *q_arr, int n);

int  pfb_sc_ready_bins (PfbSc *sc, double *f_hz_out, int max_out);
int  pfb_sc_drain_env  (PfbSc *sc, double f_hz,
                        double *env100_out, double *env200_out, int max_n);
int  pfb_sc_peek_env   (PfbSc *sc, double f_hz,
                        double *env100_out, double *env200_out, int max_n);
void pfb_sc_mark_evidence(PfbSc *sc, double f_hz);
int  pfb_sc_bin_count  (PfbSc *sc);
int  pfb_sc_env_n      (PfbSc *sc, double f_hz);
int  pfb_sc_list_bins  (PfbSc *sc, double *f_hz_out, int max_out);
double pfb_sc_get_snr  (PfbSc *sc, double f_hz);

unsigned long long pfb_sc_env_drops(PfbSc *sc);
int                pfb_sc_bins_peak(PfbSc *sc);

/* Decoder hookup — same shape as itila_sc_set_decoder. */
void pfb_sc_set_decoder(PfbSc *sc,
                        void *(*dec_create)(int sample_rate, double lpf_hz),
                        const char *(*dec_feed)(void *handle,
                                                const double *env, int n,
                                                double f_khz, double ev_thresh),
                        void  (*dec_free)(void *handle),
                        double (*dec_get_wpm)(void *handle),
                        double ev_thresh);

typedef struct {
    double f_hz;
    double snr;
    int    wpm;
    char   text[256];
} PfbScDecodeResult;

int pfb_sc_decode_ready(PfbSc *sc, int window_samples,
                        PfbScDecodeResult *results, int max_results);

/* Diagnostic — actual PFB params chosen at create time. */
int    pfb_sc_n_chan      (PfbSc *sc);
double pfb_sc_bin_spacing (PfbSc *sc);
int    pfb_sc_output_rate (PfbSc *sc);

#ifdef __cplusplus
}
#endif

#endif /* PFB_SCANNER_H */
