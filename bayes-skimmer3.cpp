/*
 * bayes-skimmer3.cpp — CW Skimmer with overlapping FFT for envelope extraction
 *
 * Uses overlapping FFTs (75% overlap) to get 200 Hz frame rate from 50 Hz bins.
 * Each FFT bin's power over time IS the keying envelope.
 * No complex mix-down needed — just track bin power at high frame rate.
 *
 * Copyright 2026 WF8Z/Spark Gap — GPL-3
 */

#include "fftw3.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>
#include <math.h>

#include "../morse-wip/src/bmorse.h"

#define BIN_HZ       (50)       // Hz per FFT bin
#define NUM_SCALES   (16)
#define AVG_SECONDS  (3)
#define THRES_WEIGHT (4.0)
#define MAX_DECODERS (100)
#define OVERLAP      (4)        // 4x overlap = 75% = 200 Hz frame rate at 50 Hz bins

PARAMS params = {
    0, 0, 0, 0, 0, 0, 8192, 32, 0, 600, 5, 4000, 10.0, 0.0, 0, 0, 20, 20
};

unsigned int sampleRate = 48000;
unsigned int printChars = 8;
bool use16bit = false;
bool showDbg = false;

struct Decoder {
    morse *bayesDecoder;
    int freqBin;
    float snr;
    char textBuf[256];
    int textLen;
    int idleFrames;
    bool active;
};

Decoder decoders[MAX_DECODERS];
int numDecoders = 0;

Decoder* getDecoder(int freqBin) {
    for (int i = 0; i < numDecoders; i++)
        if (decoders[i].active && decoders[i].freqBin == freqBin)
            return &decoders[i];
    int slot = -1;
    for (int i = 0; i < numDecoders; i++)
        if (!decoders[i].active) { slot = i; break; }
    if (slot < 0 && numDecoders < MAX_DECODERS) slot = numDecoders++;
    if (slot < 0) return NULL;

    decoders[slot].bayesDecoder = new morse();
    decoders[slot].freqBin = freqBin;
    decoders[slot].snr = 0;
    memset(decoders[slot].textBuf, 0, sizeof(decoders[slot].textBuf));
    decoders[slot].textLen = 0;
    decoders[slot].idleFrames = 0;
    decoders[slot].active = true;
    return &decoders[slot];
}

void releaseDecoder(Decoder *d) {
    if (d->bayesDecoder) delete d->bayesDecoder;
    d->bayesDecoder = NULL;
    d->active = false;
}

void printOutput(FILE *f, Decoder *d) {
    if (d->textLen < 4) return;
    int snrDb = (int)(20.0 * log10f(fmax(d->snr, 0.001)));
    fprintf(f, "%d:%d:%s\n", d->freqBin * BIN_HZ, snrDb, d->textBuf);
    fflush(f);
    memset(d->textBuf, 0, sizeof(d->textBuf));
    d->textLen = 0;
}

int main(int argc, char *argv[]) {
    FILE *inFile = stdin, *outFile = stdout;
    const char *inName = NULL, *outName = NULL;

    for (int j = 1; j < argc; j++) {
        if (argv[j][0] != '-') {
            if (!inName) inName = argv[j]; else outName = argv[j];
        } else switch(argv[j][1]) {
            case 'n': printChars = atoi(argv[++j]); break;
            case 'r': sampleRate = atoi(argv[++j]); break;
            case 'i': use16bit = true; break;
            case 'f': use16bit = false; break;
            case 'd': showDbg = true; break;
            case 'h':
                fprintf(stderr, "Bayesian CW Skimmer v3 (overlapping FFT)\n");
                fprintf(stderr, "Usage: %s [options] [<infile>]\n", argv[0]);
                fprintf(stderr, "  -r <rate>  -n <chars>  -i (16bit)  -d (debug)\n");
                return 0;
        }
    }

    inFile = inName ? fopen(inName, "rb") : stdin;
    if (!inFile) { fprintf(stderr, "Cannot open %s\n", inName); return 1; }

    unsigned int fftSize = sampleRate / BIN_HZ;      // e.g. 48000/50 = 960
    unsigned int hopSize = fftSize / OVERLAP;         // e.g. 960/4 = 240
    unsigned int numBins = fftSize / 2;               // e.g. 480
    unsigned int frameRate = sampleRate / hopSize;     // e.g. 48000/240 = 200 Hz

    fftwf_complex *fftOut = new fftwf_complex[fftSize];
    float *fftIn = new float[fftSize];
    float *window = new float[fftSize];
    fftwf_plan fft = fftwf_plan_dft_r2c_1d(fftSize, fftIn, fftOut, FFTW_ESTIMATE);

    // Sliding buffer for overlap
    float *slideBuf = new float[fftSize];
    memset(slideBuf, 0, fftSize * sizeof(float));
    short *hopIn = new short[hopSize];
    float *hopBuf = new float[hopSize];

    // Precompute Hamming window
    for (unsigned int j = 0; j < fftSize; j++)
        window[j] = 0.54 - 0.46 * cos(2.0 * M_PI * j / (fftSize - 1));

    float avgPower = 1.0;
    struct { float power; int count; } scales[NUM_SCALES];

    memset(decoders, 0, sizeof(decoders));

    fprintf(stderr, "Bayesian CW Skimmer v3: %d Hz, %d Hz bins, %dx overlap = %d Hz frame rate\n",
            sampleRate, BIN_HZ, OVERLAP, frameRate);
    fprintf(stderr, "  FFT size: %d, hop: %d, bins: %d\n", fftSize, hopSize, numBins);

    int frameCount = 0;

    while (1) {
        // Read one hop of new samples
        if (!use16bit) {
            if (fread(hopBuf, sizeof(float), hopSize, inFile) != hopSize) break;
        } else {
            if (fread(hopIn, sizeof(short), hopSize, inFile) != hopSize) break;
            for (unsigned int j = 0; j < hopSize; j++)
                hopBuf[j] = (float)hopIn[j] / 32768.0f;
        }

        // Shift slide buffer and append new hop
        memmove(slideBuf, slideBuf + hopSize, (fftSize - hopSize) * sizeof(float));
        memcpy(slideBuf + fftSize - hopSize, hopBuf, hopSize * sizeof(float));

        // Apply window and FFT
        for (unsigned int j = 0; j < fftSize; j++)
            fftIn[j] = slideBuf[j] * window[j];
        fftwf_execute(fft);

        // Compute bin magnitudes
        float binPower[numBins];
        for (unsigned int j = 0; j < numBins; j++)
            binPower[j] = sqrt(fftOut[j][0]*fftOut[j][0] + fftOut[j][1]*fftOut[j][1]);

        // Noise floor estimation
        memset(scales, 0, sizeof(scales));
        for (unsigned int j = 0; j < numBins; j++) {
            int s = (int)floor(log(fmax(binPower[j], FLT_MIN)));
            s = s < 0 ? 0 : s >= NUM_SCALES ? NUM_SCALES-1 : s;
            scales[s].power += binPower[j]; scales[s].count++;
        }
        float acc = 0; int n = 0;
        for (int i = 0; i < NUM_SCALES-1; i++) {
            int k = i;
            for (int jj = i+1; jj < NUM_SCALES; jj++)
                if (scales[jj].count > scales[k].count) k = jj;
            if (k != i) { auto tmp = scales[k]; scales[k] = scales[i]; scales[i] = tmp; }
            acc += scales[i].power; n += scales[i].count;
            if (n >= (int)numBins/2) break;
        }
        acc /= fmax(n, 1);
        avgPower += (acc - avgPower) * hopSize / sampleRate / AVG_SECONDS;

        // Process each bin
        for (unsigned int j = 1; j < numBins; j++) {
            bool hasSignal = binPower[j] >= avgPower * THRES_WEIGHT;

            if (hasSignal) {
                Decoder *d = getDecoder(j);
                if (!d) continue;

                d->idleFrames = 0;
                d->snr = binPower[j] / fmax(avgPower, FLT_MIN);

                // Per-bin adaptive normalization
                // Track each bin's own min (key-up) and max (key-down)
                static float binMin[480] = {0};
                static float binMax[480] = {0};
                static bool binInit[480] = {false};
                if (!binInit[j]) {
                    binMin[j] = binPower[j];
                    binMax[j] = binPower[j];
                    binInit[j] = true;
                }
                // Fast attack, slow decay for min/max tracking
                if (binPower[j] < binMin[j])
                    binMin[j] += (binPower[j] - binMin[j]) * 0.3;
                else
                    binMin[j] += (binPower[j] - binMin[j]) * 0.001;
                if (binPower[j] > binMax[j])
                    binMax[j] += (binPower[j] - binMax[j]) * 0.3;
                else
                    binMax[j] += (binPower[j] - binMax[j]) * 0.001;

                // Normalize to 0-1 based on bin's own range
                float binRange = binMax[j] - binMin[j];
                float envValue;
                if (binRange > 0.001) {
                    envValue = (binPower[j] - binMin[j]) / binRange;
                    envValue = fmin(fmax(envValue, 0.0f), 1.0f);
                } else {
                    envValue = 0.0f;
                }

                // Feed to Bayesian decoder
                float rn, zout;
                long int xhat, elmhat, imax;
                float px, spdhat, pmax;
                char buf[12];
                memset(buf, 0, sizeof(buf));

                d->bayesDecoder->noise_((double)envValue, &rn, &zout);
                zout = fmin(fmax(zout, 0.0f), 1.0f);

                int ret = d->bayesDecoder->proces_(zout, rn,
                    &xhat, &px, &elmhat, &spdhat, &imax, &pmax, buf);

                // Debug first decoder
                if (showDbg && d == &decoders[0]) {
                    static int dbg = 0;
                    if (dbg++ < 200)
                        fprintf(stderr, "  bin%d: env=%.3f rn=%.4f zout=%.3f xhat=%ld px=%.3f ret=%d buf='%s'\n",
                            j, envValue, rn, zout, xhat, px, ret, buf);
                }

                if (buf[0] != '\0') {
                    int blen = strlen(buf);
                    if (d->textLen + blen < 250) {
                        memcpy(d->textBuf + d->textLen, buf, blen);
                        d->textLen += blen;
                    }
                }

                if (d->textLen >= (int)printChars ||
                    (d->textLen > 4 && d->textBuf[d->textLen-1] == ' ')) {
                    printOutput(outFile, d);
                }
            } else {
                // Retire idle decoders
                for (int di = 0; di < numDecoders; di++) {
                    if (decoders[di].active && decoders[di].freqBin == (int)j) {
                        decoders[di].idleFrames++;
                        if (decoders[di].idleFrames > frameRate * 2) {
                            if (decoders[di].textLen > 3)
                                printOutput(outFile, &decoders[di]);
                            releaseDecoder(&decoders[di]);
                        }
                    }
                }
            }
        }

        frameCount++;
        if (showDbg && frameCount % 200 == 0) {
            int active = 0;
            for (int di = 0; di < numDecoders; di++)
                if (decoders[di].active) active++;
            fprintf(stderr, "Frame %d: %d active, avgPower=%.4f\n", frameCount, active, avgPower);
        }
    }

    // Flush
    for (int di = 0; di < numDecoders; di++) {
        if (decoders[di].active) {
            if (decoders[di].textLen > 3) printOutput(outFile, &decoders[di]);
            releaseDecoder(&decoders[di]);
        }
    }

    fftwf_destroy_plan(fft);
    delete[] fftOut; delete[] fftIn; delete[] window;
    delete[] slideBuf; delete[] hopIn; delete[] hopBuf;

    fprintf(stderr, "Done. %d frames.\n", frameCount);
    return 0;
}
