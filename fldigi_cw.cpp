/*
 * fldigi_cw.cpp — Standalone CW decoder extracted from fldigi
 *
 * Based on fldigi's cw.cxx by Dave Freese W1HKJ, Mauri Niininen AG1LE,
 * Tomi Manninen OH2BNS, Lawrence Glaister VE7IT.
 *
 * GPL-3.0 — same license as fldigi.
 *
 * Reads 8kHz 16-bit signed mono audio from stdin.
 * Outputs decoded characters to stdout.
 *
 * Usage: cat audio.raw | ./fldigi_cw [-f freq] [-s speed]
 *        Pipe wideband I-channel audio, specify tone frequency with -f.
 *
 * Build: g++ -O2 -o fldigi_cw fldigi_cw.cpp -lm
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <string>
#include <complex>

static int SAMPLE_RATE = 8000;      // Configurable via -r flag
#define DEC_RATIO       16          // Decimation ratio
// These are computed at runtime since SAMPLE_RATE is configurable
static int DEC_RATE() { return SAMPLE_RATE / DEC_RATIO; }
static int KWPM() { return 12 * DEC_RATE() / 10; }
#define MAX_MORSE       6
#define TWOPI           (2.0 * M_PI)
#define INITIAL_SPEED   20

typedef std::complex<double> cmplx;
bool debug_timing = false;
double preset_snr = -1;  // -1 = not set, use normal AGC warmup

// --- Morse table ---
static const struct { const char *p; char c; } morse[] = {
    {".-",'A'},    {"-...",'B'},  {"-.-.",'C'},  {"-..",'D'},
    {".",'E'},     {"..-.",'F'},  {"--.",'G'},   {"....",'H'},
    {"..",'I'},    {".---",'J'},  {"-.-",'K'},   {".-..",'L'},
    {"--",'M'},    {"-.",'N'},    {"---",'O'},    {".--.",'P'},
    {"--.-",'Q'},  {".-.",'R'},   {"...",'S'},    {"-",'T'},
    {"..-",'U'},   {"...-",'V'},  {".--",'W'},    {"-..-",'X'},
    {"-.--",'Y'},  {"--..",'Z'},
    {"-----",'0'}, {".----",'1'}, {"..---",'2'}, {"...--",'3'},
    {"....-",'4'}, {".....",'5'}, {"-....",'6'}, {"--...",'7'},
    {"---..",'8'}, {"----.",'9'},
    {".-.-.-",'.'}, {"--..--",','}, {"..--.."},
    {NULL,0}
};

static char lookup(const std::string &rep) {
    for (int i = 0; morse[i].p; i++)
        if (rep == morse[i].p) return morse[i].c;
    return '*';
}

// --- Moving average ---
class Avg {
    double *b; int n, p; double s;
public:
    Avg(int len) : n(len), p(0), s(0) { b = new double[n](); }
    ~Avg() { delete[] b; }
    double run(double v) {
        s -= b[p]; b[p] = v; s += v;
        if (++p >= n) p = 0;
        return s / n;
    }
};

// --- Overlap-save bandpass filter (simplified fftfilt) ---
// Uses brute-force FIR instead of FFT for simplicity and no FFTW dependency
class BandpassFilter {
    double *coeffs_i, *coeffs_q;  // Complex FIR taps
    double *delay_i, *delay_q;
    int ntaps, ptr;
    double carrier_freq;
    double phase;
    // Output buffer for decimation
    double *outbuf;
    int outptr, outlen;
    int dec_count;

public:
    BandpassFilter(double freq, double bandwidth, int taps = 0) {
        if (taps == 0) taps = SAMPLE_RATE / (int)bandwidth * 4 + 1; // auto-size
        if (taps > 2047) taps = 2047;
        if (taps < 31) taps = 31;
        carrier_freq = freq;
        ntaps = taps | 1;  // make odd
        phase = 0;
        dec_count = 0;

        coeffs_i = new double[ntaps]();
        coeffs_q = new double[ntaps]();
        delay_i = new double[ntaps]();
        delay_q = new double[ntaps]();
        ptr = 0;

        // Design complex bandpass centered at carrier_freq with given bandwidth
        // This is a lowpass FIR at bandwidth/2, shifted to carrier_freq
        double bw_norm = bandwidth / SAMPLE_RATE;
        int center = ntaps / 2;
        for (int i = 0; i < ntaps; i++) {
            // Sinc lowpass
            double h;
            if (i == center)
                h = bw_norm;
            else
                h = sin(M_PI * bw_norm * (i - center)) / (M_PI * (i - center));
            // Blackman window
            h *= 0.42 - 0.5 * cos(2*M_PI*i/(ntaps-1)) + 0.08 * cos(4*M_PI*i/(ntaps-1));
            coeffs_i[i] = h;
            coeffs_q[i] = h;
        }

        outbuf = new double[SAMPLE_RATE]();  // 1 second max
        outptr = 0;
        outlen = 0;
    }

    ~BandpassFilter() {
        delete[] coeffs_i; delete[] coeffs_q;
        delete[] delay_i; delete[] delay_q;
        delete[] outbuf;
    }

    // Process one sample (real mono or complex IQ). Returns envelope or -1.
    double process(double sample_i, double sample_q = 0.0) {
        // Complex mix: shift signal at carrier_freq to baseband
        // For complex IQ input: z_in = (I + jQ), multiply by e^(-j*2pi*f*t)
        // For real input: sample_q=0, this creates analytic signal from real
        double cos_p = cos(phase);
        double sin_p = sin(phase);
        double si = sample_i * cos_p + sample_q * sin_p;   // Real part of mixed
        double sq = -sample_i * sin_p + sample_q * cos_p;  // Imag part of mixed
        phase += TWOPI * carrier_freq / SAMPLE_RATE;
        if (phase > TWOPI) phase -= TWOPI;

        // FIR filter (complex)
        delay_i[ptr] = si;
        delay_q[ptr] = sq;

        double sum_i = 0, sum_q = 0;
        int idx = ptr;
        for (int k = 0; k < ntaps; k++) {
            sum_i += coeffs_i[k] * delay_i[idx];
            sum_q += coeffs_q[k] * delay_q[idx];
            if (--idx < 0) idx = ntaps - 1;
        }

        if (++ptr >= ntaps) ptr = 0;

        // Decimation
        if (++dec_count < DEC_RATIO)
            return -1.0;
        dec_count = 0;

        // Envelope = magnitude of complex output
        return sqrt(sum_i * sum_i + sum_q * sum_q);
    }
};

// --- CW Decoder ---
class CWDecoder {
    enum { IDLE, IN_TONE, AFTER_TONE } state;
    unsigned int smpl_ctr;
    unsigned int tone_start, tone_end;

    double agc_peak, noise_floor, sig_avg;
    double upper_thresh, lower_thresh;

    long two_dots;
    long dot_len;
    long noise_thresh;
    int last_element;
    bool space_sent;

    Avg *trackfilter;
    Avg *bitfilter;
    BandpassFilter *bpf;

    std::string rep;

    double decay(double avg, double val, double w) {
        return w <= 1 ? val : avg * (1.0 - 1.0/w) + val / w;
    }

    void sync_params() {
        dot_len = two_dots / 2;
        noise_thresh = dot_len / 2;
    }

    void update_tracking(int dot, int dash) {
        two_dots = (long)trackfilter->run((dot + dash) / 2.0);
        sync_params();
    }

    int handle_event(int ev, char &ch) {
        int dur;
        switch (ev) {
        case 0: // RESET
            state = IDLE; smpl_ctr = 0; rep.clear();
            space_sent = true; last_element = 0;
            break;
        case 1: // KEYDOWN
            if (state == IN_TONE) return -1;
            if (state == IDLE) { smpl_ctr = 0; rep.clear(); }
            tone_start = smpl_ctr;
            state = IN_TONE;
            return -1;
        case 2: // KEYUP
            if (state != IN_TONE) return -1;
            tone_end = smpl_ctr;
            dur = (tone_start < tone_end) ? (tone_end - tone_start) : 0;
            sync_params();
            if (noise_thresh > 0 && dur < noise_thresh) {
                state = IDLE;
                return -1;
            }
            if (last_element > 0) {
                // Accept wider ratio range (1.5x-5x) for speed tracking
                // Original fldigi used 2x-4x but that's too strict for noisy signals
                if (dur > 1.5*last_element && dur < 5*last_element)
                    update_tracking(last_element, dur);
                if (last_element > 1.5*dur && last_element < 5*dur)
                    update_tracking(dur, last_element);
            }
            last_element = dur;
            rep += (dur <= two_dots) ? '.' : '-';
            if (debug_timing)
                fprintf(stderr, "%c dur=%d 2dot=%ld dot=%ld\n",
                        (dur <= two_dots) ? '.' : '-', dur, two_dots, dot_len);
            if (rep.length() > MAX_MORSE) {
                state = IDLE; rep.clear();
                return -1;
            }
            state = AFTER_TONE;
            return -1;
        case 3: // QUERY
            if (state == IN_TONE) return -1;
            sync_params();
            dur = (tone_end < smpl_ctr) ? (smpl_ctr - tone_end) : 0;
            // Standard Morse timing:
            //   inter-element gap = 1 dit (within character)
            //   inter-character gap = 3 dits
            //   inter-word gap = 7 dits
            // Use 2.5 dits as character break (was 2 — too aggressive,
            // split 5-element digit patterns into multiple characters)
            if (dur < (long)(2.5 * dot_len)) return -1;
            if (dur >= (long)(2.5*dot_len) && dur <= (long)(5*dot_len) && state == AFTER_TONE) {
                ch = lookup(rep);
                if (debug_timing)
                    fprintf(stderr, "CHAR: '%s' → '%c' (gap=%d, dot=%ld)\n",
                            rep.c_str(), ch, dur, dot_len);
                rep.clear();
                state = IDLE;
                space_sent = false;
                return 0;
            }
            if (dur > (long)(5*dot_len) && !space_sent) {
                ch = ' ';
                space_sent = true;
                return 0;
            }
            return -1;
        }
        return -1;
    }

public:
    // Speed estimation from keying envelope FFT
    double *speed_buf;
    int speed_buf_ptr, speed_buf_len;
    bool speed_measured;

    void measure_speed() {
        if (speed_buf_ptr < speed_buf_len) return;
        int N = speed_buf_len;
        // DFT to find dit repetition frequency
        int dec_rate = DEC_RATE();
        int lo = (int)(2.0 * N / dec_rate);   // 5 WPM
        int hi = (int)(25.0 * N / dec_rate);   // 60 WPM
        if (lo < 1) lo = 1;
        if (hi > N/2-1) hi = N/2-1;

        double best_mag = 0;
        int best_k = lo;
        for (int k = lo; k <= hi; k++) {
            double re = 0, im = 0;
            for (int n = 0; n < N; n++) {
                double angle = TWOPI * k * n / N;
                re += speed_buf[n] * cos(angle);
                im -= speed_buf[n] * sin(angle);
            }
            double mag = re*re + im*im;
            if (mag > best_mag) { best_mag = mag; best_k = k; }
        }

        double peak_freq = (double)best_k * dec_rate / N;

        // Check harmonics — the peak might be a subharmonic of the real dit rate
        // CW has strong energy at half the dit rate (dit-space periodicity)
        // If 2× the peak freq also has significant energy, use the harmonic
        int harm_k = best_k * 2;
        if (harm_k < hi) {
            double harm_re = 0, harm_im = 0;
            for (int n = 0; n < N; n++) {
                double angle = TWOPI * harm_k * n / N;
                harm_re += speed_buf[n] * cos(angle);
                harm_im -= speed_buf[n] * sin(angle);
            }
            double harm_mag = harm_re*harm_re + harm_im*harm_im;
            // If harmonic is at least 40% as strong, it's likely the real dit rate
            if (harm_mag > best_mag * 0.4) {
                peak_freq = (double)harm_k * dec_rate / N;
                fprintf(stderr, "Harmonic detected: using %.1f Hz (2x)\n", peak_freq);
            }
        }

        int est_wpm = (int)(peak_freq * 2.4);
        if (est_wpm < 10) est_wpm = 10;
        if (est_wpm > 50) est_wpm = 50;

        two_dots = 2 * KWPM() / est_wpm;
        for (int i = 0; i < 8; i++) trackfilter->run(two_dots);
        sync_params();

        // Also report the top 5 peaks for debugging
        fprintf(stderr, "Speed: %d WPM (peak %.1f Hz)\n", est_wpm, peak_freq);
        if (debug_timing) {
            // Quick sort of top 5 by magnitude
            struct { int k; double mag; } top[5] = {};
            for (int k2 = lo; k2 <= hi; k2++) {
                double re2 = 0, im2 = 0;
                for (int n = 0; n < N; n++) {
                    double angle = TWOPI * k2 * n / N;
                    re2 += speed_buf[n] * cos(angle);
                    im2 -= speed_buf[n] * sin(angle);
                }
                double m2 = re2*re2 + im2*im2;
                for (int t = 0; t < 5; t++) {
                    if (m2 > top[t].mag) {
                        for (int u = 4; u > t; u--) top[u] = top[u-1];
                        top[t] = {k2, m2};
                        break;
                    }
                }
            }
            for (int t = 0; t < 5; t++) {
                double f2 = (double)top[t].k * dec_rate / N;
                fprintf(stderr, "  peak %d: %.1f Hz (%.0f WPM) mag=%.0f\n",
                        t+1, f2, f2*2.4, sqrt(top[t].mag));
            }
        }
        speed_measured = true;
    }

    CWDecoder(double freq, int speed, double bandwidth = 60.0) {
        two_dots = 2 * KWPM() / speed;
        state = IDLE; smpl_ctr = 0;
        tone_start = tone_end = 0;
        last_element = 0; space_sent = true;
        agc_peak = 0.001; noise_floor = 0; sig_avg = 0;

        trackfilter = new Avg(8);
        int bf_len = DEC_RATE() * 8 / 1000;
        if (bf_len < 2) bf_len = 2;
        if (bf_len > 32) bf_len = 32;
        bitfilter = new Avg(bf_len);
        bpf = new BandpassFilter(freq, bandwidth);

        // Speed estimation buffer — 2 seconds
        speed_buf_len = DEC_RATE() * 2;
        speed_buf = new double[speed_buf_len]();
        speed_buf_ptr = 0;
        speed_measured = false;

        for (int i = 0; i < 8; i++)
            trackfilter->run(two_dots);
        sync_params();
    }

    void preseed_agc(double snr_db) {
        // Pre-seed AGC from known SNR so decoder starts locked
        // Signal at snr_db above noise → set peak/noise ratio
        double ratio = pow(10, snr_db / 20.0);
        noise_floor = 0.01;
        agc_peak = noise_floor * ratio;
        sig_avg = (agc_peak + noise_floor) / 2;
        fprintf(stderr, "AGC pre-seeded: SNR=%.0f dB, peak=%.4f, noise=%.4f\n",
                snr_db, agc_peak, noise_floor);
    }

    ~CWDecoder() {
        delete trackfilter;
        delete bitfilter;
        delete bpf;
        delete[] speed_buf;
    }

    char process(double sample_i, double sample_q = 0.0) {
        // Bandpass filter + decimate + envelope
        double value = bpf->process(sample_i, sample_q);
        if (value < 0) return 0;  // decimation — no output yet

        smpl_ctr++;

        // Smooth envelope
        value = bitfilter->run(value);

        // Speed estimation from first 2 seconds of envelope
        // DISABLED — the estimate picks up subharmonics and interference
        // patterns instead of the actual dit rate. Better to let the
        // adaptive tracker converge from the initial speed guess.
        // TODO: improve speed estimation to handle crowded bands
        //if (!speed_measured) {
        //    if (speed_buf_ptr < speed_buf_len)
        //        speed_buf[speed_buf_ptr++] = value;
        //    else
        //        measure_speed();
        //}

        // AGC — scale constants to decimated rate
        // fldigi calibrated at 500 Hz decimated rate, scale proportionally
        double rate_scale = DEC_RATE() / 500.0;
        double agc_attack = 200 * rate_scale;   // ~400ms at any rate
        double agc_decay = 1000 * rate_scale;    // ~2000ms at any rate
        sig_avg = decay(sig_avg, value, agc_decay);
        if (value < sig_avg) {
            noise_floor = decay(noise_floor, value,
                value < noise_floor ? agc_attack : agc_decay);
        }
        if (value > sig_avg) {
            agc_peak = decay(agc_peak, value,
                value > agc_peak ? agc_attack : agc_decay);
        }

        // Normalize
        if (agc_peak > 1e-6)
            value /= agc_peak;
        else
            value = 0;

        // Dynamic thresholds
        double nn = noise_floor / (agc_peak > 1e-6 ? agc_peak : 1.0);
        double ns = sig_avg / (agc_peak > 1e-6 ? agc_peak : 1.0);
        double diff = ns - nn;
        upper_thresh = ns - 0.2 * diff;
        lower_thresh = nn + 0.7 * diff;

        // SNR gate
        double metric = 0;
        if (noise_floor > 1e-6 && noise_floor < sig_avg)
            metric = 20 * log10(sig_avg / noise_floor);
        if (metric < 3.0) return 0;  // too weak

        // Hysteresis keying
        char ch = 0;
        if (value > upper_thresh && state != IN_TONE)
            handle_event(1, ch);
        if (value < lower_thresh && state == IN_TONE)
            handle_event(2, ch);

        // Check for character
        if (handle_event(3, ch) == 0)
            return ch;

        return 0;
    }
};

int main(int argc, char *argv[]) {
    double freq = 600;
    int speed = 25;
    double bandwidth = 60;
    bool iq_mode = false;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-f") && i+1 < argc) freq = atof(argv[++i]);
        else if (!strcmp(argv[i], "-s") && i+1 < argc) speed = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-b") && i+1 < argc) bandwidth = atof(argv[++i]);
        else if (!strcmp(argv[i], "-r") && i+1 < argc) SAMPLE_RATE = atoi(argv[++i]);
        else if (!strcmp(argv[i], "-q")) iq_mode = true;
        else if (!strcmp(argv[i], "-d")) {  // debug — show timing to stderr
            extern bool debug_timing;
            debug_timing = true;
        }
        else if (!strcmp(argv[i], "--snr") && i+1 < argc) {
            // Pre-seed AGC from known SNR (dB above noise)
            // This eliminates the 60-second warmup period
            double snr_db = atof(argv[++i]);
            // Will be applied after decoder construction
            extern double preset_snr;
            preset_snr = snr_db;
        }
        else if (!strcmp(argv[i], "-h")) {
            fprintf(stderr,
                "fldigi_cw — Standalone CW decoder (fldigi-derived, GPL-3)\n"
                "Reads 16-bit signed audio from stdin\n"
                "  -r rate      Sample rate in Hz (default 8000)\n"
                "  -f freq      Signal frequency offset in Hz (default 600)\n"
                "  -s speed     Initial WPM (default 25, adapts)\n"
                "  -b bandwidth Filter bandwidth in Hz (default 60)\n"
                "  -q           IQ input mode (interleaved I,Q int16 pairs)\n");
            return 0;
        }
    }

    fprintf(stderr, "fldigi_cw: f=%.0f Hz, %d WPM, bw=%.0f Hz, %s\n",
            freq, speed, bandwidth, iq_mode ? "IQ" : "mono");

    CWDecoder dec(freq, speed, bandwidth);
    if (preset_snr >= 0) {
        dec.preseed_agc(preset_snr);
    }
    int count = 0;

    if (iq_mode) {
        short iq[2];
        while (fread(iq, sizeof(short), 2, stdin) == 2) {
            char ch = dec.process(iq[0] / 32768.0, iq[1] / 32768.0);
            if (ch) { putchar(ch); fflush(stdout); }
            count++;
        }
    } else {
        short sample;
        while (fread(&sample, sizeof(short), 1, stdin) == 1) {
            char ch = dec.process(sample / 32768.0);
            if (ch) { putchar(ch); fflush(stdout); }
            count++;
        }
    }

    fprintf(stderr, "Processed %d samples (%.1f s)\n",
            count, (double)count / SAMPLE_RATE);
    return 0;
}
