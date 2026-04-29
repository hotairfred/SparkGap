/*
 * bayes-skimmer2.cpp — CW Skimmer with proper two-stage processing
 *
 * Stage 1: FFT channelizer for signal detection (which frequencies have CW)
 * Stage 2: Per-channel time-domain demodulation for keying envelope
 *          Mix down → LPF → magnitude → Bayesian decoder
 *
 * Copyright 2026 WF8Z/Spark Gap — GPL-3
 * Based on csdr-skimmer by Marat Fayzullin and bmorse by AG1LE
 */

#include "fftw3.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>
#include <math.h>

#include "../morse-wip/src/bmorse.h"

#define BANDWIDTH    (50)       // Hz per FFT bin for signal detection
#define NUM_SCALES   (16)
#define AVG_SECONDS  (3)
#define THRES_WEIGHT (4.0)      // Signal detection threshold
#define MAX_DECODERS (100)      // Max simultaneous CW decoders
#define BAYES_RATE   (200)      // Bayesian decoder sample rate (Hz)
#define CW_FILTER_BW (100.0)   // CW signal bandwidth for LPF (Hz)
#define TWOPI        (2.0 * M_PI)

PARAMS params = {
    0, 0, 0, 0, 0, 0, 8192, 32, 0, 600, 5, 4000, 10.0, 0.0, 0, 0, 20, 20
};

unsigned int sampleRate = 48000;
unsigned int printChars = 8;
bool use16bit = false;
bool showDbg = false;

// Per-decoder state — one per active CW signal
struct Decoder {
    morse *bayesDecoder;
    int freqBin;            // FFT bin index
    double centerFreq;      // Hz
    double phase;           // oscillator phase for mix-down
    // Simple IIR low-pass filter state
    double lpfI, lpfQ;      // filter state for I and Q
    double lpfAlpha;        // filter coefficient
    // Envelope smoothing
    double envAvg;
    // Decoder output
    char textBuf[256];
    int textLen;
    float snr;
    int idleFrames;         // frames since last signal detected
    bool active;
};

Decoder decoders[MAX_DECODERS];
int numDecoders = 0;

// Find or create a decoder for a given frequency bin
Decoder* getDecoder(int freqBin, double centerFreq) {
    // Look for existing decoder on this bin
    for (int i = 0; i < numDecoders; i++) {
        if (decoders[i].active && decoders[i].freqBin == freqBin)
            return &decoders[i];
    }
    // Look for an inactive slot
    for (int i = 0; i < numDecoders; i++) {
        if (!decoders[i].active) {
            decoders[i].bayesDecoder = new morse();
            decoders[i].freqBin = freqBin;
            decoders[i].centerFreq = centerFreq;
            decoders[i].phase = 0;
            decoders[i].lpfI = 0;
            decoders[i].lpfQ = 0;
            // LPF cutoff: CW_FILTER_BW Hz, using simple IIR
            double dt = 1.0 / sampleRate;
            double rc = 1.0 / (TWOPI * CW_FILTER_BW);
            decoders[i].lpfAlpha = dt / (rc + dt);
            decoders[i].envAvg = 0;
            memset(decoders[i].textBuf, 0, sizeof(decoders[i].textBuf));
            decoders[i].textLen = 0;
            decoders[i].snr = 0;
            decoders[i].idleFrames = 0;
            decoders[i].active = true;
            return &decoders[i];
        }
    }
    // Create new if room
    if (numDecoders < MAX_DECODERS) {
        int i = numDecoders++;
        decoders[i].bayesDecoder = new morse();
        decoders[i].freqBin = freqBin;
        decoders[i].centerFreq = centerFreq;
        decoders[i].phase = 0;
        decoders[i].lpfI = 0;
        decoders[i].lpfQ = 0;
        double dt = 1.0 / sampleRate;
        double rc = 1.0 / (TWOPI * CW_FILTER_BW);
        decoders[i].lpfAlpha = dt / (rc + dt);
        decoders[i].envAvg = 0;
        memset(decoders[i].textBuf, 0, sizeof(decoders[i].textBuf));
        decoders[i].textLen = 0;
        decoders[i].snr = 0;
        decoders[i].idleFrames = 0;
        decoders[i].active = true;
        return &decoders[i];
    }
    return NULL;
}

void releaseDecoder(Decoder *dec) {
    if (dec->bayesDecoder) {
        delete dec->bayesDecoder;
        dec->bayesDecoder = NULL;
    }
    dec->active = false;
}

void printOutput(FILE *outFile, Decoder *dec) {
    if (dec->textLen < 4) return;
    int snrDb = (int)(20.0 * log10f(fmax(dec->snr, 0.001)));
    fprintf(outFile, "%d:%d:%s\n", (int)dec->centerFreq, snrDb, dec->textBuf);
    fflush(outFile);
    memset(dec->textBuf, 0, sizeof(dec->textBuf));
    dec->textLen = 0;
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
        } else switch(argv[j][1]) {
            case 'n': printChars = atoi(argv[++j]); break;
            case 'r': sampleRate = atoi(argv[++j]); break;
            case 'i': use16bit = true; break;
            case 'f': use16bit = false; break;
            case 'd': showDbg = true; break;
            case 'h':
                fprintf(stderr, "Bayesian CW Skimmer v2 (two-stage: FFT detection + time-domain decoding)\n");
                fprintf(stderr, "Usage: %s [options] [<infile> [<outfile>]]\n", argv[0]);
                fprintf(stderr, "  -r <rate>  -- Sampling rate (default 48000)\n");
                fprintf(stderr, "  -n <chars> -- Min characters to print (default 8)\n");
                fprintf(stderr, "  -i / -f    -- 16bit integer / 32bit float input\n");
                fprintf(stderr, "  -d         -- Debug output\n");
                return 0;
        }
    }

    inFile = inName ? fopen(inName, "rb") : stdin;
    if (!inFile) { fprintf(stderr, "Cannot open %s\n", inName); return 1; }
    outFile = outName ? fopen(outName, "wb") : stdout;

    // FFT setup for signal detection
    unsigned int fftSize = sampleRate / BANDWIDTH;
    unsigned int numBins = fftSize / 2;

    fftwf_complex *fftOut = new fftwf_complex[fftSize];
    float *fftIn = new float[fftSize];
    fftwf_plan fft = fftwf_plan_dft_r2c_1d(fftSize, fftIn, fftOut, FFTW_ESTIMATE);

    // Raw sample buffer — we need to keep the time-domain samples
    // for per-channel demodulation after FFT identifies active channels
    float *rawBuf = new float[fftSize];
    short *dataIn = new short[fftSize];

    // Decimation counter for feeding Bayesian decoder at BAYES_RATE
    int decimation = sampleRate / BAYES_RATE;

    float avgPower = 1.0;
    struct { float power; int count; } scales[NUM_SCALES];

    // Active channel tracking from FFT
    bool binActive[numBins];
    float binPower[numBins];

    memset(decoders, 0, sizeof(decoders));

    fprintf(stderr, "Bayesian CW Skimmer v2: %d Hz, %d Hz bins, %d max decoders, %d Hz decoder rate\n",
            sampleRate, BANDWIDTH, MAX_DECODERS, BAYES_RATE);
    fprintf(stderr, "  Decimation: 1:%d, CW filter BW: %.0f Hz\n", decimation, CW_FILTER_BW);

    int frameCount = 0;

    // Main loop
    while (1) {
        // Read raw samples
        if (!use16bit) {
            if (fread(rawBuf, sizeof(float), fftSize, inFile) != fftSize) break;
        } else {
            if (fread(dataIn, sizeof(short), fftSize, inFile) != fftSize) break;
            for (j = 0; j < (int)fftSize; j++)
                rawBuf[j] = (float)dataIn[j] / 32768.0f;
        }

        // === STAGE 1: FFT for signal detection ===
        double hk = TWOPI / (fftSize - 1);
        for (j = 0; j < (int)fftSize; j++)
            fftIn[j] = rawBuf[j] * (0.54 - 0.46 * cos(j * hk));

        fftwf_execute(fft);

        // Compute magnitudes
        for (j = 0; j < (int)numBins; j++) {
            binPower[j] = sqrt(fftOut[j][0]*fftOut[j][0] + fftOut[j][1]*fftOut[j][1]);
        }

        // Compute noise floor
        memset(scales, 0, sizeof(scales));
        for (j = 0; j < (int)numBins; j++) {
            int scale = (int)floor(log(fmax(binPower[j], FLT_MIN)));
            scale = scale < 0 ? 0 : scale >= NUM_SCALES ? NUM_SCALES-1 : scale;
            scales[scale].power += binPower[j];
            scales[scale].count++;
        }
        float accPower = 0; int n = 0;
        for (int i = 0; i < NUM_SCALES-1; i++) {
            int k = i;
            for (int jj = i+1; jj < NUM_SCALES; jj++)
                if (scales[jj].count > scales[k].count) k = jj;
            if (k != i) { auto tmp = scales[k]; scales[k] = scales[i]; scales[i] = tmp; }
            accPower += scales[i].power;
            n += scales[i].count;
            if (n >= (int)numBins/2) break;
        }
        accPower /= fmax(n, 1);
        avgPower += (accPower - avgPower) * fftSize / sampleRate / AVG_SECONDS;

        // Identify active bins
        memset(binActive, 0, sizeof(binActive));
        for (j = 0; j < (int)numBins; j++) {
            binActive[j] = binPower[j] >= avgPower * THRES_WEIGHT;
        }

        // === STAGE 2: Per-channel time-domain demodulation ===
        // For each active bin, process the raw samples
        for (j = 1; j < (int)numBins; j++) {
            if (!binActive[j]) {
                // Check if we have a decoder on this bin that should be retired
                for (int d = 0; d < numDecoders; d++) {
                    if (decoders[d].active && decoders[d].freqBin == j) {
                        decoders[d].idleFrames++;
                        if (decoders[d].idleFrames > 100) { // ~2 seconds at 50 Hz
                            if (decoders[d].textLen > 3)
                                printOutput(outFile, &decoders[d]);
                            releaseDecoder(&decoders[d]);
                        }
                    }
                }
                continue;
            }

            // Get or create decoder for this bin
            double centerFreq = j * BANDWIDTH;
            Decoder *dec = getDecoder(j, centerFreq);
            if (!dec) continue;

            dec->idleFrames = 0;
            dec->snr = binPower[j] / fmax(avgPower, FLT_MIN);

            // Process each raw sample: mix down, filter, detect envelope
            for (unsigned int s = 0; s < fftSize; s++) {
                // Mix down to baseband (multiply by complex conjugate of center freq)
                double cosVal = cos(dec->phase);
                double sinVal = sin(dec->phase);
                double mixI = rawBuf[s] * cosVal;
                double mixQ = rawBuf[s] * sinVal;
                dec->phase += TWOPI * centerFreq / sampleRate;
                if (dec->phase > TWOPI) dec->phase -= TWOPI;

                // Simple IIR low-pass filter
                dec->lpfI += dec->lpfAlpha * (mixI - dec->lpfI);
                dec->lpfQ += dec->lpfAlpha * (mixQ - dec->lpfQ);

                // Magnitude = envelope
                double envelope = sqrt(dec->lpfI * dec->lpfI + dec->lpfQ * dec->lpfQ);

                // Smooth envelope slightly
                dec->envAvg += (envelope - dec->envAvg) * 0.3;

                // Decimate to BAYES_RATE
                static int sampleCounters[MAX_DECODERS] = {0};
                int dIdx = dec - decoders;
                sampleCounters[dIdx]++;
                if (sampleCounters[dIdx] >= decimation) {
                    sampleCounters[dIdx] = 0;

                    // Feed to Bayesian decoder
                    float rn = 0.1f;
                    float zout;
                    long int xhat, elmhat, imax;
                    float px, spdhat, pmax;
                    char buf[12];
                    memset(buf, 0, sizeof(buf));

                    // Use noise estimation from AG1LE
                    float envFloat = (float)dec->envAvg;
                    dec->bayesDecoder->noise_((double)envFloat, &rn, &zout);
                    zout = fmin(fmax(zout, 0.0f), 1.0f);

                    // Debug: show envelope values for first decoder
                    if (showDbg && dIdx == 0) {
                        static int envDbg = 0;
                        if (envDbg++ < 200)
                            fprintf(stderr, "  env=%.4f rn=%.4f zout=%.4f xhat=%ld px=%.3f\n",
                                envFloat, rn, zout, xhat, px);
                    }

                    int ret = dec->bayesDecoder->proces_(zout, rn,
                        &xhat, &px, &elmhat, &spdhat, &imax, &pmax, buf);

                    if (buf[0] != '\0') {
                        int blen = strlen(buf);
                        if (dec->textLen + blen < 250) {
                            memcpy(dec->textBuf + dec->textLen, buf, blen);
                            dec->textLen += blen;
                        }
                    }

                    // Output when enough text accumulated
                    if (dec->textLen >= (int)printChars ||
                        (dec->textLen > 4 && dec->textBuf[dec->textLen-1] == ' ')) {
                        printOutput(outFile, dec);
                    }
                }
            }
        }

        frameCount++;
        if (showDbg && frameCount % 50 == 0) {
            int activeCount = 0;
            for (int d = 0; d < numDecoders; d++)
                if (decoders[d].active) activeCount++;
            fprintf(stderr, "Frame %d: %d active decoders, avgPower=%.4f\n",
                    frameCount, activeCount, avgPower);
        }
    }

    // Final flush
    for (int d = 0; d < numDecoders; d++) {
        if (decoders[d].active) {
            if (decoders[d].textLen > 3)
                printOutput(outFile, &decoders[d]);
            releaseDecoder(&decoders[d]);
        }
    }

    fftwf_destroy_plan(fft);
    delete[] fftOut;
    delete[] fftIn;
    delete[] rawBuf;
    delete[] dataIn;

    if (outFile != stdout) fclose(outFile);
    if (inFile != stdin) fclose(inFile);

    fprintf(stderr, "Done. Processed %d frames.\n", frameCount);
    return 0;
}
