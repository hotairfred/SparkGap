/*
 * bayes-skimmer.cpp — CW Skimmer using AG1LE's Bayesian Morse Decoder
 *
 * Uses csdr-skimmer's FFT channelizer for signal detection,
 * but replaces libcsdr's CwDecoder with AG1LE's Bayesian decoder
 * (morse class from bmorse/morse-wip).
 *
 * Each detected channel gets its own morse instance for independent
 * speed tracking and probabilistic decoding.
 *
 * Copyright 2026 WF8Z — GPL-3
 * Based on csdr-skimmer by Marat Fayzullin and bmorse by AG1LE
 */

#include "fftw3.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>
#include <math.h>

// AG1LE's Bayesian decoder
#include "../morse-wip/src/bmorse.h"

#define BANDWIDTH    (50)      // Hz per channel (50 = good balance)
#define NUM_SCALES   (16)
#define AVG_SECONDS  (3)
#define THRES_WEIGHT (4.0)     // Signal detection threshold (lower = more sensitive)
#define MAX_CHANNELS (960)     // Max simultaneous decoders
#define MIN_SNR_DB   (3.0)     // Minimum SNR to attempt decoding

// Initialize AG1LE params
PARAMS params = {
    0,      // print_variables
    0,      // print_symbols
    0,      // print_speed
    0,      // process_textfile
    0,      // print_text
    0,      // print_xplot
    8192,   // width
    32,     // speclen
    0,      // bfv
    600,    // frequency (not used per-channel)
    5,      // sample_duration
    4000,   // sample_rate (will be overridden)
    10.0,   // delta
    0.0,    // amplify
    0,      // fft
    0,      // agc
    20,     // speed
    20      // dec_ratio
};

unsigned int sampleRate = 48000;
unsigned int printChars = 12;
bool use16bit = false;
bool showDbg = false;

// Per-channel decoder state
struct Channel {
    morse *decoder;
    float snr;
    float rn;          // noise estimate
    int active;        // frames since last activity
    int sampleCounter; // decimation counter
    char textBuf[256]; // accumulated decoded text
    int textLen;
    float magL;        // per-channel low (noise floor)
    float magH;        // per-channel high (signal peak)
};

Channel channels[MAX_CHANNELS];
int numChannels = 0;

// Decimation: AG1LE decoder expects 200 Hz input rate
int decRatio = 1;

void initChannel(Channel *ch, float initialPower) {
    ch->decoder = new morse();
    ch->snr = 0.0;
    ch->rn = 0.1f;
    ch->active = 0;
    ch->sampleCounter = 0;
    memset(ch->textBuf, 0, sizeof(ch->textBuf));
    ch->textLen = 0;
    ch->magL = initialPower * 0.5;
    ch->magH = initialPower;
}

void resetChannel(Channel *ch) {
    if (ch->decoder) delete ch->decoder;
    ch->decoder = new morse();
    ch->snr = 0.0;
    ch->rn = 0.1f;
    ch->active = 0;
    ch->sampleCounter = 0;
    memset(ch->textBuf, 0, sizeof(ch->textBuf));
    ch->textLen = 0;
    ch->magL = 0.5;
    ch->magH = 0.5;
}

void printOutput(FILE *outFile, int chanIdx, unsigned int freq) {
    Channel *ch = &channels[chanIdx];
    if (ch->textLen < 4) return; // Need minimum characters

    // Print freq:snr_db:text
    int snrDb = (int)(20.0 * log10f(fmax(ch->snr, 0.001)));
    fprintf(outFile, "%d:%d:%s\n", freq, snrDb, ch->textBuf);
    fflush(outFile);

    // Reset text buffer
    memset(ch->textBuf, 0, sizeof(ch->textBuf));
    ch->textLen = 0;
}

int main(int argc, char *argv[]) {
    FILE *inFile = stdin;
    FILE *outFile = stdout;
    const char *inName = NULL;
    const char *outName = NULL;
    int j;

    // Parse arguments
    for (j = 1; j < argc; j++) {
        if (argv[j][0] != '-') {
            if (!inName) inName = argv[j];
            else if (!outName) outName = argv[j];
        } else if (strlen(argv[j]) != 2) {
            fprintf(stderr, "Unrecognized option '%s'\n", argv[j]);
            return 2;
        } else switch(argv[j][1]) {
            case 'n':
                printChars = j < argc-1 ? atoi(argv[++j]) : printChars;
                break;
            case 'r':
                sampleRate = j < argc-1 ? atoi(argv[++j]) : sampleRate;
                break;
            case 'i':
                use16bit = true;
                break;
            case 'f':
                use16bit = false;
                break;
            case 'd':
                showDbg = true;
                break;
            case 'h':
                fprintf(stderr, "Bayesian CW Skimmer (AG1LE decoder + csdr-skimmer channelizer)\n");
                fprintf(stderr, "Usage: %s [options] [<infile> [<outfile>]]\n", argv[0]);
                fprintf(stderr, "  -r <rate>  -- Sampling rate (default 48000)\n");
                fprintf(stderr, "  -n <chars> -- Min characters to print (default 12)\n");
                fprintf(stderr, "  -i         -- 16bit signed integer input\n");
                fprintf(stderr, "  -f         -- 32bit float input\n");
                fprintf(stderr, "  -d         -- Debug output to stderr\n");
                fprintf(stderr, "  -h         -- This help\n");
                return 0;
        }
    }

    // Open files
    inFile = inName ? fopen(inName, "rb") : stdin;
    if (!inFile) { fprintf(stderr, "Cannot open %s\n", inName); return 1; }
    outFile = outName ? fopen(outName, "wb") : stdout;
    if (!outFile) { fprintf(stderr, "Cannot open %s\n", outName); return 1; }

    // FFT setup
    unsigned int inputStep = sampleRate / BANDWIDTH;
    numChannels = inputStep / 2;
    if (numChannels > MAX_CHANNELS) numChannels = MAX_CHANNELS;

    // Decimation ratio: deliver samples to decoder at ~200 Hz
    decRatio = BANDWIDTH / 200;
    if (decRatio < 1) decRatio = 1;

    // Set AG1LE params
    params.sample_rate = BANDWIDTH;  // Per-channel rate
    params.dec_ratio = decRatio;

    fftwf_complex *fftOut = new fftwf_complex[inputStep];
    short *dataIn = new short[inputStep];
    float *dataBuf = new float[inputStep];
    float *fftIn = new float[inputStep];
    fftwf_plan fft = fftwf_plan_dft_r2c_1d(inputStep, fftIn, fftOut, FFTW_ESTIMATE);

    // Initialize channels
    for (j = 0; j < numChannels; j++) {
        channels[j].decoder = NULL;
        channels[j].snr = 0.0;
        channels[j].active = 0;
    }

    float avgPower = 4.0;

    struct {
        float power;
        int count;
    } scales[NUM_SCALES];

    fprintf(stderr, "Bayesian CW Skimmer: %d Hz sample rate, %d Hz bins, %d channels\n",
            sampleRate, BANDWIDTH, numChannels);

    // Main decode loop
    while (1) {
        // Read input
        if (!use16bit) {
            if (fread(dataBuf, sizeof(float), inputStep, inFile) != inputStep)
                break;
        } else {
            if (fread(dataIn, sizeof(short), inputStep, inFile) != inputStep)
                break;
            for (j = 0; j < (int)inputStep; j++)
                dataBuf[j] = (float)dataIn[j] / 32768.0;
        }

        // Hamming window
        double hk = 2.0 * M_PI / (inputStep - 1);
        for (j = 0; j < (int)inputStep; j++)
            fftIn[j] = dataBuf[j] * (0.54 - 0.46 * cos(j * hk));

        // FFT
        fftwf_execute(fft);

        // Compute magnitudes
        for (j = 0; j < numChannels; j++)
            fftOut[j][0] = fftOut[j][1] = sqrt(fftOut[j][0]*fftOut[j][0] + fftOut[j][1]*fftOut[j][1]);

        // Compute average power for noise floor estimation
        memset(scales, 0, sizeof(scales));
        float maxPower = 0.0;
        for (j = 0; j < numChannels; j++) {
            float v = fftOut[j][0];
            int scale = floor(log(v));
            scale = scale < 0 ? 0 : scale+1 >= NUM_SCALES ? NUM_SCALES-1 : scale+1;
            maxPower = fmax(maxPower, v);
            scales[scale].power += v;
            scales[scale].count++;
        }

        // Find noise floor from most populated scales
        float accPower = 0.0;
        int n = 0;
        for (int i = 0; i < NUM_SCALES-1; i++) {
            int k = i;
            for (int jj = i+1; jj < NUM_SCALES; jj++)
                if (scales[jj].count > scales[k].count) k = jj;
            if (k != i) {
                float v = scales[k].power;
                int c = scales[k].count;
                scales[k] = scales[i];
                scales[i].power = v;
                scales[i].count = c;
            }
            accPower += scales[i].power;
            n += scales[i].count;
            if (n >= numChannels/2) break;
        }
        accPower /= fmax(n, 1);
        avgPower += (accPower - avgPower) * inputStep / sampleRate / AVG_SECONDS;

        // Process each channel
        for (j = 0; j < numChannels; j++) {
            float power = fftOut[j][0];
            float signalLevel = power / fmax(avgPower, FLT_MIN);

            // Track SNR
            channels[j].snr += (signalLevel - channels[j].snr) *
                (signalLevel >= channels[j].snr ? 0.25 : 0.05);

            // Only decode channels with signal above threshold
            if (power >= avgPower * THRES_WEIGHT) {
                // Lazy init decoder
                if (!channels[j].decoder) {
                    initChannel(&channels[j], power);
                }

                channels[j].active = 0;

                // Per-channel adaptive envelope detection
                // Track this channel's own min/max to detect keying
                float range = channels[j].magH - channels[j].magL;
                float envelope;
                if (range > 0.01) {
                    // Hysteresis threshold within channel's own range
                    envelope = power > (channels[j].magL + range * 0.6) ? 1.0f :
                               power < (channels[j].magL + range * 0.4) ? 0.0f :
                               -1.0f; // keep previous state
                } else {
                    envelope = 0.0f;
                }
                // If hysteresis says keep previous, use last known state
                static float lastEnv[MAX_CHANNELS] = {0};
                if (envelope < 0) envelope = lastEnv[j];
                lastEnv[j] = envelope;

                // Update per-channel magnitude tracking
                float attack = 0.1f;
                float decay = 0.001f;
                channels[j].magL += power < channels[j].magL ?
                    (power - channels[j].magL) * attack : range * decay;
                channels[j].magH += power > channels[j].magH ?
                    (power - channels[j].magH) * attack : -range * decay;

                // Feed to Bayesian decoder
                long int xhat, elmhat, imax;
                float px, spdhat, pmax;
                char buf[12];

                // Debug
                if (showDbg && j > 0 && channels[j].active == 0) {
                    static int dbgCount = 0;
                    if (dbgCount++ < 100)
                        fprintf(stderr, "ch%d: pwr=%.3f L=%.3f H=%.3f env=%.0f snr=%.1f\n",
                            j, power, channels[j].magL, channels[j].magH, envelope, channels[j].snr);
                }

                // Feed decoder at ~200 Hz rate by repeating samples
                int repeats = 200 / BANDWIDTH;
                if (repeats < 1) repeats = 1;
                int ret = 0;
                for (int rep = 0; rep < repeats; rep++) {
                    memset(buf, 0, sizeof(buf));
                    // Feed envelope directly with small noise estimate
                    float rn = 0.05f;
                    ret = channels[j].decoder->proces_(envelope, rn,
                        &xhat, &px, &elmhat, &spdhat, &imax, &pmax, buf);
                    if (buf[0] != '\0') {
                        int blen = strlen(buf);
                        if (channels[j].textLen + blen < 250) {
                            memcpy(channels[j].textBuf + channels[j].textLen, buf, blen);
                            channels[j].textLen += blen;
                        }
                    }
                }

                // Accumulate decoded text (already done in loop above)
                if (0 && ret && buf[0] != '\0') {
                    int blen = strlen(buf);
                    if (channels[j].textLen + blen < 250) {
                        memcpy(channels[j].textBuf + channels[j].textLen, buf, blen);
                        channels[j].textLen += blen;
                    }
                }

                // Print when we have enough text or a word break
                if (channels[j].textLen >= (int)printChars ||
                    (channels[j].textLen > 4 && channels[j].textBuf[channels[j].textLen-1] == ' ')) {
                    printOutput(outFile, j, j * BANDWIDTH);
                }
            } else {
                // No signal — if decoder was active, flush remaining text
                if (channels[j].decoder) {
                    channels[j].active++;
                    if (channels[j].active > sampleRate / BANDWIDTH * 2) {
                        // 2 seconds of silence — flush and reset
                        if (channels[j].textLen > 3) {
                            printOutput(outFile, j, j * BANDWIDTH);
                        }
                        resetChannel(&channels[j]);
                    }
                }
            }
        }
    }

    // Final flush
    for (j = 0; j < numChannels; j++) {
        if (channels[j].decoder && channels[j].textLen > 3) {
            printOutput(outFile, j, j * BANDWIDTH);
        }
        if (channels[j].decoder) delete channels[j].decoder;
    }

    // Cleanup
    fftwf_destroy_plan(fft);
    delete[] fftOut;
    delete[] fftIn;
    delete[] dataBuf;
    delete[] dataIn;

    if (outFile != stdout) fclose(outFile);
    if (inFile != stdin) fclose(inFile);

    return 0;
}
