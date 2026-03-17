/*
 * threshold-skimmer.cpp — CW Skimmer with overlapping FFT + adaptive threshold decoder
 *
 * Uses overlapping FFT (4x, 200 Hz frame rate) for signal detection and
 * per-bin envelope extraction. Simple adaptive threshold decoder converts
 * mark/space transitions to Morse elements.
 *
 * Designed for callsign extraction, not general CW text decoding.
 * Paired with master.scp validation for accurate spotting.
 *
 * Copyright 2026 WF8Z/Spark Gap — GPL-3
 */

#include "fftw3.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>
#include <math.h>

#define BIN_HZ       (50)
#define NUM_SCALES   (16)
#define AVG_SECONDS  (3)
#define THRES_WEIGHT (6.0)     // Higher threshold = fewer false triggers
#define MAX_DECODERS (200)
#define OVERLAP      (8)       // 8x overlap = 400 Hz frame rate
#define FRAME_MS     (2.5)     // ms per frame at 400 Hz

// Morse lookup tree — index by accumulated code
// code starts at 1, dit shifts left and adds 1, dah shifts left and adds 0
static const char morseTree[] =
    "__TEMNAIOGKDWRUS"
    "__QZYCXBJP_L_FVH"
    "09_8___7_____/6"
    "1______&2___3_45"
    "________________"
    "________________"
    "________________"
    "________________";

char code2char(unsigned int code) {
    if (code == 0 || code >= 128) return '_';
    return morseTree[code < 128 ? code : 0];
}

unsigned int sampleRate = 48000;
unsigned int printChars = 8;
bool use16bit = false;
bool showDbg = false;

struct CWDecoder {
    int freqBin;
    bool active;
    float snr;

    // Per-bin envelope tracking
    float binMin, binMax;
    bool binInit;

    // Signal state
    bool keyDown;           // current key state
    int markFrames;         // frames in current mark
    int spaceFrames;        // frames in current space
    int totalFrames;        // total frames processed

    // Adaptive timing
    float avgDitFrames;     // average dit length in frames
    float avgDahFrames;     // average dah length in frames
    float avgSpaceFrames;   // average inter-element space

    // Morse code accumulator
    unsigned int code;      // current code (1 = empty, shift left for each element)
    char textBuf[256];
    int textLen;

    // Idle tracking
    int idleFrames;
};

CWDecoder decoders[MAX_DECODERS];
int numDecoders = 0;

void initDecoder(CWDecoder *d, int bin) {
    memset(d, 0, sizeof(CWDecoder));
    d->freqBin = bin;
    d->active = true;
    d->binInit = false;
    d->keyDown = false;
    d->avgDitFrames = 12;   // ~30ms at 400 Hz = 30 WPM default
    d->avgDahFrames = 36;   // ~90ms = 3x dit
    d->avgSpaceFrames = 12; // ~30ms
    d->code = 1;
}

CWDecoder* getDecoder(int bin) {
    for (int i = 0; i < numDecoders; i++)
        if (decoders[i].active && decoders[i].freqBin == bin)
            return &decoders[i];
    int slot = -1;
    for (int i = 0; i < numDecoders; i++)
        if (!decoders[i].active) { slot = i; break; }
    if (slot < 0 && numDecoders < MAX_DECODERS) slot = numDecoders++;
    if (slot < 0) return NULL;
    initDecoder(&decoders[slot], bin);
    return &decoders[slot];
}

void emitChar(CWDecoder *d, char c) {
    if (c == '_' || c == '\0') return;
    if (d->textLen < 250) {
        d->textBuf[d->textLen++] = c;
    }
}

void processEnvelope(CWDecoder *d, float envelope) {
    d->totalFrames++;

    // Determine key state with hysteresis
    bool newKeyDown;
    if (d->keyDown)
        newKeyDown = envelope > 0.35;  // Lower threshold to release
    else
        newKeyDown = envelope > 0.55;  // Higher threshold to trigger

    if (newKeyDown) {
        if (!d->keyDown) {
            // Key just went DOWN — end of space
            d->keyDown = true;
            int spaceDur = d->spaceFrames;

            if (spaceDur > 0 && d->code > 1) {
                // Classify the space
                float midCharSpace = (d->avgDitFrames + d->avgDahFrames) / 2.0;
                float midWordSpace = d->avgDahFrames * 2.5;

                if (spaceDur >= midWordSpace) {
                    // Word space — emit character + space
                    emitChar(d, code2char(d->code));
                    emitChar(d, ' ');
                    d->code = 1;
                } else if (spaceDur >= midCharSpace) {
                    // Character space — emit character
                    emitChar(d, code2char(d->code));
                    d->code = 1;
                }
                // Else inter-element space — continue accumulating

                // Update average space timing
                if (spaceDur > 1 && spaceDur < d->avgDitFrames * 2.0)
                    d->avgSpaceFrames += (spaceDur - d->avgSpaceFrames) * 0.2;
            }

            d->markFrames = 0;
        }
        d->markFrames++;
    } else {
        if (d->keyDown) {
            // Key just went UP — end of mark
            d->keyDown = false;
            int markDur = d->markFrames;

            if (markDur > 0) {
                // Classify: dit or dah using probabilistic distance
                float ditDist = fabs(markDur - d->avgDitFrames);
                float dahDist = fabs(markDur - d->avgDahFrames);

                if (markDur > 1) {  // ignore very short noise spikes
                    if (ditDist <= dahDist) {
                        // Dit
                        d->code = (d->code << 1) | 1;
                        // Update dit average
                        if (markDur > 1 && markDur < d->avgDahFrames)
                            d->avgDitFrames += (markDur - d->avgDitFrames) * 0.15;
                    } else {
                        // Dah
                        d->code = (d->code << 1) | 0;
                        // Update dah average
                        if (markDur > d->avgDitFrames * 1.5)
                            d->avgDahFrames += (markDur - d->avgDahFrames) * 0.15;
                    }

                    // Enforce approximate 3:1 ratio
                    float ratio = d->avgDahFrames / fmax(d->avgDitFrames, 1.0);
                    if (ratio < 2.0) d->avgDahFrames = d->avgDitFrames * 2.5;
                    if (ratio > 5.0) d->avgDahFrames = d->avgDitFrames * 4.0;
                }
            }

            d->spaceFrames = 0;
        }
        d->spaceFrames++;
    }

    // Timeout: long silence = flush remaining character
    if (!d->keyDown && d->spaceFrames > d->avgDahFrames * 4 && d->code > 1) {
        emitChar(d, code2char(d->code));
        emitChar(d, ' ');
        d->code = 1;
    }

    // Code overflow protection
    if (d->code >= 128) d->code = 1;
}

void printOutput(FILE *f, CWDecoder *d) {
    if (d->textLen < 4) return;
    int snrDb = (int)(20.0 * log10f(fmax(d->snr, 0.001)));
    int wpm = (int)(1200.0 / fmax(d->avgDitFrames * FRAME_MS, 1.0));
    fprintf(f, "%d:%d:%s\n", d->freqBin * BIN_HZ, wpm, d->textBuf);
    fflush(f);
    memset(d->textBuf, 0, sizeof(d->textBuf));
    d->textLen = 0;
}

int main(int argc, char *argv[]) {
    FILE *inFile = stdin, *outFile = stdout;
    const char *inName = NULL;

    for (int j = 1; j < argc; j++) {
        if (argv[j][0] != '-') { inName = argv[j]; }
        else switch(argv[j][1]) {
            case 'n': printChars = atoi(argv[++j]); break;
            case 'r': sampleRate = atoi(argv[++j]); break;
            case 'i': use16bit = true; break;
            case 'd': showDbg = true; break;
            case 'h':
                fprintf(stderr, "Threshold CW Skimmer (overlapping FFT + adaptive decoder)\n");
                fprintf(stderr, "Usage: %s [options] [<infile>]\n", argv[0]);
                fprintf(stderr, "  -r <rate>  -n <chars>  -i (16bit)  -d (debug)\n");
                return 0;
        }
    }

    inFile = inName ? fopen(inName, "rb") : stdin;
    if (!inFile) { fprintf(stderr, "Cannot open %s\n", inName); return 1; }

    unsigned int fftSize = sampleRate / BIN_HZ;
    unsigned int hopSize = fftSize / OVERLAP;
    unsigned int numBins = fftSize / 2;

    fftwf_complex *fftOut = new fftwf_complex[fftSize];
    float *fftIn = new float[fftSize];
    float *window = new float[fftSize];
    fftwf_plan fft = fftwf_plan_dft_r2c_1d(fftSize, fftIn, fftOut, FFTW_ESTIMATE);

    float *slideBuf = new float[fftSize];
    memset(slideBuf, 0, fftSize * sizeof(float));
    short *hopIn = new short[hopSize];
    float *hopBuf = new float[hopSize];

    for (unsigned int j = 0; j < fftSize; j++)
        window[j] = 0.54 - 0.46 * cos(2.0 * M_PI * j / (fftSize - 1));

    float avgPower = 1.0;
    struct { float power; int count; } scales[NUM_SCALES];

    // Per-bin envelope tracking (separate from decoders for efficiency)
    float binMin[numBins], binMax[numBins];
    bool binInit[numBins];
    memset(binInit, 0, sizeof(binInit));

    memset(decoders, 0, sizeof(decoders));

    fprintf(stderr, "Threshold CW Skimmer: %d Hz, %d Hz bins, %dx overlap, %d max decoders\n",
            sampleRate, BIN_HZ, OVERLAP, MAX_DECODERS);

    int frameCount = 0;

    while (1) {
        if (!use16bit) {
            if (fread(hopBuf, sizeof(float), hopSize, inFile) != hopSize) break;
        } else {
            if (fread(hopIn, sizeof(short), hopSize, inFile) != hopSize) break;
            for (unsigned int j = 0; j < hopSize; j++)
                hopBuf[j] = (float)hopIn[j] / 32768.0f;
        }

        memmove(slideBuf, slideBuf + hopSize, (fftSize - hopSize) * sizeof(float));
        memcpy(slideBuf + fftSize - hopSize, hopBuf, hopSize * sizeof(float));

        for (unsigned int j = 0; j < fftSize; j++)
            fftIn[j] = slideBuf[j] * window[j];
        fftwf_execute(fft);

        float binPower[numBins];
        for (unsigned int j = 0; j < numBins; j++)
            binPower[j] = sqrt(fftOut[j][0]*fftOut[j][0] + fftOut[j][1]*fftOut[j][1]);

        // Noise floor
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

        // Process bins — two passes:
        // 1. Activate new decoders for strong signals
        // 2. Feed ALL active decoders (signal or not) with current bin power

        // Pass 1: Activate new decoders
        for (unsigned int j = 1; j < numBins; j++) {
            if (binPower[j] >= avgPower * THRES_WEIGHT) {
                CWDecoder *d = getDecoder(j);
                if (d) {
                    d->idleFrames = 0;
                    d->snr = binPower[j] / fmax(avgPower, FLT_MIN);
                }
            }
        }

        // Pass 2: Feed all active decoders with hard threshold envelope
        for (int di = 0; di < numDecoders; di++) {
            if (!decoders[di].active) continue;

            int j = decoders[di].freqBin;
            if (j < 0 || j >= (int)numBins) continue;

            // Hard threshold: is this bin above the noise floor?
            float envelope = binPower[j] >= avgPower * THRES_WEIGHT ? 1.0f : 0.0f;

            processEnvelope(&decoders[di], envelope);

            // Print when enough text
            if (decoders[di].textLen >= (int)printChars ||
                (decoders[di].textLen > 4 && decoders[di].textBuf[decoders[di].textLen-1] == ' ')) {
                printOutput(outFile, &decoders[di]);
            }

            // Check for signal gone — retire after long silence
            if (binPower[j] < avgPower * THRES_WEIGHT) {
                decoders[di].idleFrames++;
                if (decoders[di].idleFrames > 800) { // ~2 sec at 400 Hz
                    if (decoders[di].textLen > 3)
                        printOutput(outFile, &decoders[di]);
                    decoders[di].active = false;
                }
            }
        }

        frameCount++;
        if (showDbg && frameCount % 1000 == 0) {
            int act = 0;
            for (int di = 0; di < numDecoders; di++)
                if (decoders[di].active) act++;
            fprintf(stderr, "Frame %d: %d active decoders\n", frameCount, act);
        }
    }

    // Flush all
    for (int di = 0; di < numDecoders; di++) {
        if (decoders[di].active && decoders[di].textLen > 3)
            printOutput(outFile, &decoders[di]);
    }

    fftwf_destroy_plan(fft);
    delete[] fftOut; delete[] fftIn; delete[] window;
    delete[] slideBuf; delete[] hopIn; delete[] hopBuf;

    fprintf(stderr, "Done. %d frames.\n", frameCount);
    return 0;
}
