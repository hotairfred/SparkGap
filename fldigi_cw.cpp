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
 *        sox input.wav -t raw -r 8000 -e signed -b 16 -c 1 - | ./fldigi_cw
 *
 * Build: g++ -O2 -o fldigi_cw fldigi_cw.cpp -lm -lfftw3
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <string>
#include <complex>
#include <fftw3.h>

// Constants
#define SAMPLE_RATE     8000
#define KWPM            (12 * SAMPLE_RATE / 10 / DEC_RATIO)  // 600 at decimated rate
#define DEC_RATIO       16
#define CW_FFT_SIZE     2048
#define MAX_MORSE_ELEMENTS 6
#define WGT_SIZE        7
#define TRACKING_FILTER_SIZE 16
#define INITIAL_SPEED   20
#define TWOPI           (2.0 * M_PI)

typedef std::complex<double> cmplx;

// --- Morse lookup table ---
struct MorseEntry {
    const char *pattern;  // e.g. ".-"
    char ch;
};

static const MorseEntry morse_table[] = {
    {".-",    'A'}, {"-...",  'B'}, {"-.-.",  'C'}, {"-..",   'D'},
    {".",     'E'}, {"..-.",  'F'}, {"--.",   'G'}, {"....",  'H'},
    {"..",    'I'}, {".---",  'J'}, {"-.-",   'K'}, {".-..",  'L'},
    {"--",    'M'}, {"-.",    'N'}, {"---",   'O'}, {".--.",  'P'},
    {"--.-",  'Q'}, {".-.",   'R'}, {"...",   'S'}, {"-",     'T'},
    {"..-",   'U'}, {"...-",  'V'}, {".--",   'W'}, {"-..-",  'X'},
    {"-.--",  'Y'}, {"--..",  'Z'},
    {"-----", '0'}, {".----", '1'}, {"..---", '2'}, {"...--", '3'},
    {"....-", '4'}, {".....", '5'}, {"-....", '6'}, {"--...", '7'},
    {"---..", '8'}, {"----.", '9'},
    {".-.-.-",'.'}, {"--..--",','}, {"..--..",'?'}, {"-..-.", '/'},
    {NULL, 0}
};

static char morse_lookup(const std::string &rep) {
    for (int i = 0; morse_table[i].pattern; i++) {
        if (rep == morse_table[i].pattern)
            return morse_table[i].ch;
    }
    return '*';  // unknown
}

// --- Moving average filter ---
class MovAvg {
    double *buf;
    int len, ptr;
    double sum;
public:
    MovAvg(int n) : len(n), ptr(0), sum(0) {
        buf = new double[n]();
    }
    ~MovAvg() { delete[] buf; }
    double run(double v) {
        sum -= buf[ptr];
        buf[ptr] = v;
        sum += v;
        if (++ptr >= len) ptr = 0;
        return sum / len;
    }
};

// --- FFT bandpass filter ---
class FFTFilter {
    fftwf_plan fwd, rev;
    fftwf_complex *freq;
    float *timebuf;
    int fftlen, filterlen;
    cmplx *ovlbuf;
    int inptr;
public:
    FFTFilter(double f1, double f2, int len) : fftlen(len), filterlen(len/2) {
        freq = (fftwf_complex *)fftwf_malloc(sizeof(fftwf_complex) * (fftlen/2+1));
        timebuf = (float *)fftwf_malloc(sizeof(float) * fftlen);
        ovlbuf = new cmplx[filterlen]();
        inptr = 0;

        // Create filter kernel
        float *kernel = (float *)calloc(fftlen, sizeof(float));
        int lo = (int)(f1 * fftlen / SAMPLE_RATE);
        int hi = (int)(f2 * fftlen / SAMPLE_RATE);
        if (lo < 0) lo = 0;
        if (hi > fftlen/2) hi = fftlen/2;
        fftwf_complex *fk = (fftwf_complex *)fftwf_malloc(sizeof(fftwf_complex) * (fftlen/2+1));

        // Simple rectangular bandpass in frequency domain
        memset(fk, 0, sizeof(fftwf_complex) * (fftlen/2+1));
        for (int i = lo; i <= hi; i++) {
            fk[i][0] = 1.0f;
            fk[i][1] = 0.0f;
        }

        // We'll just use the frequency domain filter directly
        memcpy(freq, fk, sizeof(fftwf_complex) * (fftlen/2+1));
        fftwf_free(fk);
        free(kernel);

        fwd = fftwf_plan_dft_r2c_1d(fftlen, timebuf, freq, FFTW_ESTIMATE);
        rev = fftwf_plan_dft_c2r_1d(fftlen, freq, timebuf, FFTW_ESTIMATE);
    }
    ~FFTFilter() {
        fftwf_destroy_plan(fwd);
        fftwf_destroy_plan(rev);
        fftwf_free(freq);
        fftwf_free(timebuf);
        delete[] ovlbuf;
    }
    // Not using the full overlap-save — simplified for standalone use
};

// --- CW Decoder (fldigi-derived) ---
class CWDecoder {
    // State
    enum { RS_IDLE, RS_IN_TONE, RS_AFTER_TONE } rx_state;
    unsigned int smpl_ctr;
    unsigned int start_timestamp, end_timestamp;

    // AGC
    double agc_peak, noise_floor, sig_avg;

    // Thresholds
    double upper_thresh, lower_thresh;

    // Timing
    long two_dots;
    long dot_len, dash_len;
    long noise_threshold;
    int last_element;
    bool space_sent;

    // Tracking filter
    MovAvg *trackfilter;

    // Decode buffer
    std::string rep_buf;

    // Signal processing
    double phase;
    double carrier_freq;
    MovAvg *bitfilter;

    // FFT bandpass
    fftwf_complex *fft_in, *fft_out;
    fftwf_plan fft_fwd, fft_rev;
    float *fft_buf;
    int fft_ptr;

    // Decimation
    int dec_count;

    // Config
    int initial_speed;

    double decayavg(double avg, double val, double weight) {
        if (weight <= 1.0) return val;
        return avg * (1.0 - 1.0/weight) + val * (1.0/weight);
    }

    double clamp(double v, double lo, double hi) {
        return v < lo ? lo : v > hi ? hi : v;
    }

    void sync_parameters() {
        dot_len = two_dots / 2;
        dash_len = 3 * dot_len;
        noise_threshold = dot_len / 2;
    }

    void update_tracking(int dot, int dash) {
        two_dots = (long)trackfilter->run((dot + dash) / 2.0);
        sync_parameters();
    }

    int handle_event(int event, std::string &sc) {
        int element_usec;

        switch (event) {
        case 0: // RESET
            rx_state = RS_IDLE;
            smpl_ctr = 0;
            rep_buf.clear();
            space_sent = true;
            last_element = 0;
            break;

        case 1: // KEYDOWN
            if (rx_state == RS_IN_TONE) return -1;
            if (rx_state == RS_IDLE) {
                smpl_ctr = 0;
                rep_buf.clear();
            }
            start_timestamp = smpl_ctr;
            rx_state = RS_IN_TONE;
            return -1;

        case 2: // KEYUP
            if (rx_state != RS_IN_TONE) return -1;
            end_timestamp = smpl_ctr;
            element_usec = (start_timestamp < end_timestamp) ?
                           (end_timestamp - start_timestamp) : 0;

            sync_parameters();

            // Noise spike filter
            if (noise_threshold > 0 && element_usec < noise_threshold) {
                rx_state = RS_IDLE;
                return -1;
            }

            // Speed tracking from dot-dash pairs
            if (last_element > 0) {
                if (element_usec > 2 * last_element && element_usec < 4 * last_element)
                    update_tracking(last_element, element_usec);
                if (last_element > 2 * element_usec && last_element < 4 * element_usec)
                    update_tracking(element_usec, last_element);
            }
            last_element = element_usec;

            // Dot or dash?
            if (element_usec <= two_dots)
                rep_buf += '.';
            else
                rep_buf += '-';

            // Buffer overflow = noise
            if (rep_buf.length() > MAX_MORSE_ELEMENTS) {
                rx_state = RS_IDLE;
                rep_buf.clear();
                return -1;
            }

            rx_state = RS_AFTER_TONE;
            return -1;

        case 3: // QUERY
            if (rx_state == RS_IN_TONE) return -1;

            sync_parameters();
            element_usec = (end_timestamp < smpl_ctr) ?
                           (smpl_ctr - end_timestamp) : 0;

            // Too short — wait
            if (element_usec < 2 * dot_len) return -1;

            // Character space (2-4 dot lengths)
            if (element_usec >= 2 * dot_len &&
                element_usec <= 4 * dot_len &&
                rx_state == RS_AFTER_TONE) {
                char ch = morse_lookup(rep_buf);
                sc = std::string(1, ch);
                rep_buf.clear();
                rx_state = RS_IDLE;
                space_sent = false;
                return 0;  // SUCCESS
            }

            // Word space (>4 dot lengths)
            if (element_usec > 4 * dot_len && !space_sent) {
                sc = " ";
                space_sent = true;
                return 0;
            }

            return -1;
        }
        return -1;
    }

public:
    CWDecoder(double freq = 600.0, int speed = INITIAL_SPEED) {
        carrier_freq = freq;
        initial_speed = speed;
        phase = 0;
        dec_count = 0;

        // Initialize timing
        two_dots = 2 * KWPM / speed;
        rx_state = RS_IDLE;
        smpl_ctr = 0;
        start_timestamp = end_timestamp = 0;
        last_element = 0;
        space_sent = true;

        // AGC
        agc_peak = 0.001;
        noise_floor = 0.0;
        sig_avg = 0.0;

        // Filters
        bitfilter = new MovAvg(8);
        trackfilter = new MovAvg(TRACKING_FILTER_SIZE);

        // Pre-seed tracking filter
        for (int i = 0; i < TRACKING_FILTER_SIZE; i++)
            trackfilter->run(two_dots);

        sync_parameters();
    }

    ~CWDecoder() {
        delete bitfilter;
        delete trackfilter;
    }

    // Process one audio sample. Returns decoded character or 0.
    char process_sample(double sample) {
        // Mix to baseband
        cmplx z(sample * cos(phase), sample * sin(phase));
        phase += TWOPI * carrier_freq / SAMPLE_RATE;
        if (phase > TWOPI) phase -= TWOPI;

        // Decimation counter — only process every DEC_RATIO samples
        if (++dec_count < DEC_RATIO)
            return 0;
        dec_count = 0;

        // Timing counter increments at decimated rate (500 Hz)
        smpl_ctr++;

        // Demodulate — take magnitude
        double value = std::abs(z);
        value = bitfilter->run(value);

        // AGC
        int attack, decay;
        attack = 200;  // samples at decimated rate
        decay = 1000;

        sig_avg = decayavg(sig_avg, value, decay);

        if (value < sig_avg) {
            if (value < noise_floor)
                noise_floor = decayavg(noise_floor, value, attack);
            else
                noise_floor = decayavg(noise_floor, value, decay);
        }
        if (value > sig_avg) {
            if (value > agc_peak)
                agc_peak = decayavg(agc_peak, value, attack);
            else
                agc_peak = decayavg(agc_peak, value, decay);
        }

        // Normalize
        if (agc_peak > 1e-6)
            value /= agc_peak;
        else
            value = 0;

        // Dynamic thresholds
        double norm_noise = noise_floor / (agc_peak > 1e-6 ? agc_peak : 1.0);
        double norm_sig = sig_avg / (agc_peak > 1e-6 ? agc_peak : 1.0);
        double diff = norm_sig - norm_noise;

        upper_thresh = norm_sig - 0.2 * diff;
        lower_thresh = norm_noise + 0.7 * diff;

        // SNR check (simple squelch)
        double metric = 0;
        if (noise_floor > 1e-6 && noise_floor < sig_avg)
            metric = clamp(2.5 * (20 * log10(sig_avg / noise_floor)), 0, 100);

        std::string sc;

        if (metric > 5.0) {  // minimum SNR to attempt decode
            // Hysteresis keying detector
            if (value > upper_thresh && rx_state != RS_IN_TONE) {
                handle_event(1, sc);  // KEYDOWN
            }
            if (value < lower_thresh && rx_state == RS_IN_TONE) {
                handle_event(2, sc);  // KEYUP
            }
        }

        // Check for completed character
        if (handle_event(3, sc) == 0) {  // QUERY
            if (!sc.empty())
                return sc[0];
        }

        return 0;
    }
};

// --- Main ---
int main(int argc, char *argv[]) {
    double freq = 600.0;
    int speed = 20;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-f") == 0 && i+1 < argc)
            freq = atof(argv[++i]);
        else if (strcmp(argv[i], "-s") == 0 && i+1 < argc)
            speed = atoi(argv[++i]);
        else if (strcmp(argv[i], "-h") == 0) {
            fprintf(stderr, "fldigi_cw — Standalone CW decoder (fldigi-derived)\n");
            fprintf(stderr, "Reads 8kHz 16-bit signed mono from stdin\n");
            fprintf(stderr, "Usage: %s [-f freq_hz] [-s initial_wpm]\n", argv[0]);
            fprintf(stderr, "  -f freq   CW tone frequency in Hz (default 600)\n");
            fprintf(stderr, "  -s speed  Initial WPM (default 20, adapts automatically)\n");
            return 0;
        }
    }

    fprintf(stderr, "fldigi_cw: freq=%.0f Hz, initial speed=%d WPM\n", freq, speed);

    CWDecoder decoder(freq, speed);

    short sample;
    int count = 0;

    while (fread(&sample, sizeof(short), 1, stdin) == 1) {
        double s = sample / 32768.0;
        char ch = decoder.process_sample(s);
        if (ch) {
            putchar(ch);
            fflush(stdout);
        }
        count++;
    }

    fprintf(stderr, "Processed %d samples (%.1f seconds)\n",
            count, (double)count / SAMPLE_RATE);

    return 0;
}
