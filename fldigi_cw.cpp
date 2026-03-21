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
                if (dur > 2*last_element && dur < 4*last_element)
                    update_tracking(last_element, dur);
                if (last_element > 2*dur && last_element < 4*dur)
                    update_tracking(dur, last_element);
            }
            last_element = dur;
            rep += (dur <= two_dots) ? '.' : '-';
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
            if (dur < 2 * dot_len) return -1;
            if (dur >= 2*dot_len && dur <= 4*dot_len && state == AFTER_TONE) {
                ch = lookup(rep);
                rep.clear();
                state = IDLE;
                space_sent = false;
                return 0;
            }
            if (dur > 4*dot_len && !space_sent) {
                ch = ' ';
                space_sent = true;
                return 0;
            }
            return -1;
        }
        return -1;
    }

public:
    CWDecoder(double freq, int speed, double bandwidth = 60.0) {
        two_dots = 2 * KWPM() / speed;
        state = IDLE; smpl_ctr = 0;
        tone_start = tone_end = 0;
        last_element = 0; space_sent = true;
        agc_peak = 0.001; noise_floor = 0; sig_avg = 0;

        trackfilter = new Avg(16);
        // bitfilter length: ~8ms at decimated rate
        int bf_len = DEC_RATE() * 8 / 1000;
        if (bf_len < 2) bf_len = 2;
        if (bf_len > 32) bf_len = 32;
        bitfilter = new Avg(bf_len);
        bpf = new BandpassFilter(freq, bandwidth);

        for (int i = 0; i < 16; i++)
            trackfilter->run(two_dots);
        sync_params();
    }

    ~CWDecoder() {
        delete trackfilter;
        delete bitfilter;
        delete bpf;
    }

    char process(double sample_i, double sample_q = 0.0) {
        // Bandpass filter + decimate + envelope
        double value = bpf->process(sample_i, sample_q);
        if (value < 0) return 0;  // decimation — no output yet

        smpl_ctr++;

        // Smooth envelope
        value = bitfilter->run(value);

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
