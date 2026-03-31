/*
 * uhsdr-skimmer.cpp — Channelizer + UHSDR in-process decoder pipeline
 *
 * Reads a WAV file, finds active CW channels via FFT, channelizes each
 * to 12kHz audio with CW tone at 700 Hz, decodes in-process via
 * libuhsdr_cw API. No subprocess needed.
 *
 * Usage: uhsdr-skimmer [options] <input.wav>
 *   -r <rate>     Sample rate (default: from WAV header)
 *   -s <speed>    WPM for decoder (default: 0 = auto)
 *   -c <pitch>    CW pitch Hz (default: 600)
 *   -t <thresh>   Signal detection threshold dB above noise (default: 6)
 *   -w <width>    Channel spacing Hz (default: 200)
 *   -l <low>      Low freq Hz of CW sub-band (default: 37000)
 *   -h <high>     High freq Hz of CW sub-band (default: 90000)
 *   -n <cores>    Number of parallel channels (default: 4)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <fftw3.h>
#include <omp.h>
#include "uhsdr_cw_lib.h"

// WAV header parser (16/24-bit PCM, mono or stereo)
struct WavHeader {
    int sampleRate;
    int numChannels;
    int bitsPerSample;
    int dataSize;
    int numSamples;
};

int readWavHeader(FILE *f, WavHeader *hdr) {
    char chunk[5] = {0};
    unsigned int chunkSize;
    unsigned short audioFormat, numChannels, bitsPerSample, blockAlign;
    unsigned int sampleRate, byteRate, dataSize;

    // RIFF header
    fread(chunk, 1, 4, f);
    if (strcmp(chunk, "RIFF") != 0) return -1;
    fread(&chunkSize, 4, 1, f);
    fread(chunk, 1, 4, f);
    if (strcmp(chunk, "WAVE") != 0) return -1;

    // Find fmt chunk
    while (fread(chunk, 1, 4, f) == 4) {
        fread(&chunkSize, 4, 1, f);
        if (strncmp(chunk, "fmt ", 4) == 0) {
            fread(&audioFormat, 2, 1, f);
            fread(&numChannels, 2, 1, f);
            fread(&sampleRate, 4, 1, f);
            fread(&byteRate, 4, 1, f);
            fread(&blockAlign, 2, 1, f);
            fread(&bitsPerSample, 2, 1, f);
            if (chunkSize > 16) fseek(f, chunkSize - 16, SEEK_CUR);
            break;
        }
        fseek(f, chunkSize, SEEK_CUR);
    }

    // Find data chunk
    while (fread(chunk, 1, 4, f) == 4) {
        fread(&chunkSize, 4, 1, f);
        if (strncmp(chunk, "data", 4) == 0) {
            dataSize = chunkSize;
            break;
        }
        fseek(f, chunkSize, SEEK_CUR);
    }

    hdr->sampleRate = sampleRate;
    hdr->numChannels = numChannels;
    hdr->bitsPerSample = bitsPerSample;
    hdr->dataSize = dataSize;
    hdr->numSamples = dataSize / (numChannels * bitsPerSample / 8);
    return 0;
}

// Read WAV data as float IQ (I = channel 0, Q = channel 1)
void readWavIQ(FILE *f, WavHeader *hdr, float **out_i, float **out_q) {
    int n = hdr->numSamples;
    *out_i = (float *)malloc(n * sizeof(float));
    *out_q = (float *)malloc(n * sizeof(float));
    int bytesPerSample = hdr->bitsPerSample / 8;
    int frameSize = hdr->numChannels * bytesPerSample;
    unsigned char *buf = (unsigned char *)malloc(frameSize);

    for (int i = 0; i < n; i++) {
        if ((int)fread(buf, 1, frameSize, f) != frameSize) break;
        if (bytesPerSample == 2) {
            short i_val = *(short *)buf;
            short q_val = (hdr->numChannels >= 2) ? *(short *)(buf + 2) : 0;
            (*out_i)[i] = (float)i_val / 32768.0f;
            (*out_q)[i] = (float)q_val / 32768.0f;
        } else if (bytesPerSample == 3) {
            int i_val = buf[0] | (buf[1] << 8) | (buf[2] << 16);
            if (i_val >= 0x800000) i_val -= 0x1000000;
            (*out_i)[i] = (float)i_val / 8388608.0f;
            if (hdr->numChannels >= 2) {
                int q_val = buf[3] | (buf[4] << 8) | (buf[5] << 16);
                if (q_val >= 0x800000) q_val -= 0x1000000;
                (*out_q)[i] = (float)q_val / 8388608.0f;
            } else {
                (*out_q)[i] = 0.0f;
            }
        }
    }
    free(buf);
}

// Write 16-bit mono WAV to a temp file
void writeWav(const char *path, float *data, int n, int sampleRate) {
    FILE *f = fopen(path, "wb");
    if (!f) return;

    int dataSize = n * 2;
    int fileSize = 36 + dataSize;

    // RIFF header
    fwrite("RIFF", 1, 4, f);
    fwrite(&fileSize, 4, 1, f);
    fwrite("WAVE", 1, 4, f);

    // fmt chunk
    fwrite("fmt ", 1, 4, f);
    int fmtSize = 16;
    fwrite(&fmtSize, 4, 1, f);
    short audioFormat = 1, numChannels = 1, bitsPerSample = 16;
    int byteRate = sampleRate * 2;
    short blockAlign = 2;
    fwrite(&audioFormat, 2, 1, f);
    fwrite(&numChannels, 2, 1, f);
    fwrite(&sampleRate, 4, 1, f);
    fwrite(&byteRate, 4, 1, f);
    fwrite(&blockAlign, 2, 1, f);
    fwrite(&bitsPerSample, 2, 1, f);

    // data chunk
    fwrite("data", 1, 4, f);
    fwrite(&dataSize, 4, 1, f);
    for (int i = 0; i < n; i++) {
        float v = data[i] * 32767.0f;
        if (v > 32767.0f) v = 32767.0f;
        if (v < -32768.0f) v = -32768.0f;
        short s = (short)v;
        fwrite(&s, 2, 1, f);
    }
    fclose(f);
}

// FIR low-pass filter design (windowed sinc)
float *designFIR(int numTaps, float cutoff, float sampleRate) {
    float *fir = (float *)malloc(numTaps * sizeof(float));
    float nyq = sampleRate / 2.0f;
    float fc = cutoff / nyq;
    int M = numTaps - 1;
    float sum = 0;

    for (int i = 0; i <= M; i++) {
        float n = i - M / 2.0f;
        if (fabs(n) < 1e-6)
            fir[i] = 2.0f * fc;
        else
            fir[i] = sin(2.0f * M_PI * fc * n) / (M_PI * n);
        // Hamming window
        fir[i] *= 0.54f - 0.46f * cos(2.0f * M_PI * i / M);
        sum += fir[i];
    }
    // Normalize
    for (int i = 0; i <= M; i++)
        fir[i] /= sum;

    return fir;
}

// Channelize: IQ mix to CW pitch, FIR filter, decimate
float *channelize(float *i_samples, float *q_samples, int n, int sampleRate,
                  float centerFreq, float cwPitch, int targetRate, int *outLen) {
    float mixFreq = centerFreq - cwPitch;
    int decimFactor = sampleRate / targetRate;
    int outN = n / decimFactor;

    // FIR filter
    float cutoff = targetRate / 2.0f * 0.8f;
    int numTaps = decimFactor * 20 + 1;
    if (numTaps > 255) numTaps = 255;
    if (numTaps % 2 == 0) numTaps++;
    float *fir = designFIR(numTaps, cutoff, sampleRate);

    // IQ mix + filter + decimate in one pass
    float *out = (float *)calloc(outN, sizeof(float));
    float phaseInc = 2.0f * M_PI * mixFreq / sampleRate;

    for (int oi = 0; oi < outN; oi++) {
        int center = oi * decimFactor;
        float sum = 0;
        for (int j = 0; j < numTaps; j++) {
            int si = center - numTaps / 2 + j;
            if (si >= 0 && si < n) {
                float phase = phaseInc * si;
                float mixed = i_samples[si] * cosf(phase) + q_samples[si] * sinf(phase);
                sum += mixed * fir[j];
            }
        }
        out[oi] = sum;
    }

    free(fir);

    // Normalize to 0.9 peak
    float peak = 0;
    for (int i = 0; i < outN; i++)
        if (fabs(out[i]) > peak) peak = fabs(out[i]);
    if (peak > 1e-6f)
        for (int i = 0; i < outN; i++)
            out[i] = out[i] * 0.9f / peak;

    *outLen = outN;
    return out;
}

// Find exact signal frequency using complex IQ FFT peak detection
// Handles negative frequencies (signals below center)
float findPeakFreqIQ(float *i_samp, float *q_samp, int n, int sampleRate,
                     float approxFreq, float tolerance) {
    int fftSize = 8192;
    if (n < fftSize) fftSize = n;

    fftw_complex *in = (fftw_complex *)fftw_malloc(fftSize * sizeof(fftw_complex));
    fftw_complex *out = (fftw_complex *)fftw_malloc(fftSize * sizeof(fftw_complex));
    fftw_plan plan = fftw_plan_dft_1d(fftSize, in, out, FFTW_FORWARD, FFTW_ESTIMATE);

    // Window + copy IQ
    for (int i = 0; i < fftSize; i++) {
        double w = 0.5 * (1.0 - cos(2.0 * M_PI * i / (fftSize - 1)));
        in[i][0] = i_samp[i] * w;  // real = I
        in[i][1] = q_samp[i] * w;  // imag = Q
    }
    fftw_execute(plan);

    // Complex FFT: bin k = k * sampleRate / fftSize for k < N/2
    //              bin k = (k - N) * sampleRate / fftSize for k >= N/2
    float freqRes = (float)sampleRate / fftSize;

    // Convert approxFreq to bin range (handle negative frequencies)
    float loFreq = approxFreq - tolerance;
    float hiFreq = approxFreq + tolerance;

    float maxMag = 0;
    int maxBin = 0;
    for (int k = 0; k < fftSize; k++) {
        float f = (k < fftSize / 2) ? k * freqRes : (k - fftSize) * freqRes;
        if (f >= loFreq && f <= hiFreq) {
            float mag = sqrt(out[k][0] * out[k][0] + out[k][1] * out[k][1]);
            if (mag > maxMag) {
                maxMag = mag;
                maxBin = k;
            }
        }
    }

    fftw_destroy_plan(plan);
    fftw_free(in);
    fftw_free(out);

    float result = (maxBin < fftSize / 2) ? maxBin * freqRes : (maxBin - fftSize) * freqRes;
    return result;
}

// Find peak in real-valued audio (for pitch detection in channelized audio)
float findPeakFreq(float *samples, int n, int sampleRate, float approxFreq, float tolerance) {
    int fftSize = 8192;
    if (n < fftSize) fftSize = n;

    double *in = (double *)fftw_malloc(fftSize * sizeof(double));
    fftw_complex *out = (fftw_complex *)fftw_malloc((fftSize / 2 + 1) * sizeof(fftw_complex));
    fftw_plan plan = fftw_plan_dft_r2c_1d(fftSize, in, out, FFTW_ESTIMATE);

    for (int i = 0; i < fftSize; i++) {
        double w = 0.5 * (1.0 - cos(2.0 * M_PI * i / (fftSize - 1)));
        in[i] = samples[i] * w;
    }
    fftw_execute(plan);

    float freqRes = (float)sampleRate / fftSize;
    int lobin = (int)((approxFreq - tolerance) / freqRes);
    int hibin = (int)((approxFreq + tolerance) / freqRes);
    if (lobin < 1) lobin = 1;
    if (hibin >= fftSize / 2) hibin = fftSize / 2 - 1;

    float maxMag = 0;
    int maxBin = lobin;
    for (int i = lobin; i <= hibin; i++) {
        float mag = sqrt(out[i][0] * out[i][0] + out[i][1] * out[i][1]);
        if (mag > maxMag) {
            maxMag = mag;
            maxBin = i;
        }
    }

    fftw_destroy_plan(plan);
    fftw_free(in);
    fftw_free(out);

    return maxBin * freqRes;
}

int main(int argc, char *argv[]) {
    int wpm = 0;  // auto
    float cwPitch = 700.0f;
    float threshDb = 6.0f;
    float chanWidth = 200.0f;
    float freqLow = 37000.0f;
    float freqHigh = 90000.0f;
    int numCores = 4;
    int targetRate = 12000;
    const char *inFile = NULL;

    // Parse args
    for (int i = 1; i < argc; i++) {
        if (argv[i][0] == '-' && i + 1 < argc) {
            switch (argv[i][1]) {
                case 's': wpm = atoi(argv[++i]); break;
                case 'c': cwPitch = atof(argv[++i]); break;
                case 't': threshDb = atof(argv[++i]); break;
                case 'w': chanWidth = atof(argv[++i]); break;
                case 'l': freqLow = atof(argv[++i]); break;
                case 'h': freqHigh = atof(argv[++i]); break;
                case 'n': numCores = atoi(argv[++i]); break;
            }
        } else {
            inFile = argv[i];
        }
    }

    if (!inFile) {
        fprintf(stderr, "Usage: %s [options] <input.wav>\n", argv[0]);
        fprintf(stderr, "  -s <wpm>    Speed (default 25)\n");
        fprintf(stderr, "  -c <pitch>  CW pitch Hz (default 600)\n");
        fprintf(stderr, "  -t <db>     Detection threshold (default 6)\n");
        fprintf(stderr, "  -w <hz>     Channel spacing (default 200)\n");
        fprintf(stderr, "  -l <hz>     Low freq (default 37000)\n");
        fprintf(stderr, "  -h <hz>     High freq (default 90000)\n");
        fprintf(stderr, "  -n <cores>  Parallel channels (default 4)\n");
        return 1;
    }

    // Read WAV
    FILE *f = fopen(inFile, "rb");
    if (!f) { fprintf(stderr, "Cannot open %s\n", inFile); return 1; }

    WavHeader hdr;
    if (readWavHeader(f, &hdr) != 0) {
        fprintf(stderr, "Invalid WAV file\n");
        fclose(f);
        return 1;
    }
    fprintf(stderr, "Input: %d Hz, %d-bit, %d ch, %d samples (%.1f sec)\n",
            hdr.sampleRate, hdr.bitsPerSample, hdr.numChannels,
            hdr.numSamples, (float)hdr.numSamples / hdr.sampleRate);

    float *i_samples, *q_samples;
    readWavIQ(f, &hdr, &i_samples, &q_samples);
    fclose(f);

    int sr = hdr.sampleRate;
    int n = hdr.numSamples;

    // Build channel list
    int numChannels = (int)((freqHigh - freqLow) / chanWidth);
    float *channelFreqs = (float *)malloc(numChannels * sizeof(float));
    for (int i = 0; i < numChannels; i++)
        channelFreqs[i] = freqLow + i * chanWidth;

    fprintf(stderr, "Channels: %d (%.0f-%.0f Hz, %.0f Hz spacing)\n",
            numChannels, freqLow, freqHigh, chanWidth);
    fprintf(stderr, "bmorse: %d WPM, %.0f Hz pitch, %d cores\n", wpm, cwPitch, numCores);

    int processed = 0;

    omp_set_num_threads(numCores);
    fprintf(stderr, "Running with %d OpenMP threads\n", numCores);

    #pragma omp parallel for schedule(dynamic) reduction(+:processed)
    for (int ch = 0; ch < numChannels; ch++) {
        float freq = channelFreqs[ch];

        // Find exact peak using complex IQ FFT
        float exactFreq = findPeakFreqIQ(i_samples, q_samples, n, sr, freq, chanWidth / 2);

        // Step 1: Channelize first 15s at default pitch for pitch detection
        int pitchSamples = (n < sr * 15) ? n : sr * 15;
        int pitchOutLen;
        float *pitchAudio = channelize(i_samples, q_samples, pitchSamples, sr,
                                        exactFreq, cwPitch, targetRate, &pitchOutLen);

        if (pitchOutLen < 1000) {
            free(pitchAudio);
            continue;
        }

        // Step 2: Find actual tone frequency in channelized audio
        float actualPitch = findPeakFreq(pitchAudio, pitchOutLen, targetRate, cwPitch, 200);
        free(pitchAudio);

        // Step 3: Re-channelize full signal with corrected center
        float correctedCenter = exactFreq + (actualPitch - cwPitch);
        int outLen;
        float *chanAudio = channelize(i_samples, q_samples, n, sr,
                                       correctedCenter, cwPitch, targetRate, &outLen);

        if (outLen < 1000) {
            free(chanAudio);
            continue;
        }

        // Convert float audio to int16 for uhsdr
        int16_t *pcm = (int16_t *)malloc(outLen * sizeof(int16_t));
        for (int i = 0; i < outLen; i++) {
            float v = chanAudio[i] * 32767.0f * 0.3f;  // match peak normalization
            if (v > 32767.0f) v = 32767.0f;
            if (v < -32768.0f) v = -32768.0f;
            pcm[i] = (int16_t)v;
        }
        free(chanAudio);

        if (fabs(actualPitch - cwPitch) > 5.0f) {
            #pragma omp critical
            fprintf(stderr, "  ch %d: %.0f Hz → pitch %.0f → corrected %.0f Hz\n",
                    ch, exactFreq, actualPitch, correctedCenter);
        }

        // Decode in-process via uhsdr library (init uses globals, needs critical section)
        uhsdr_handle_t decoder;
        #pragma omp critical
        {
            decoder = uhsdr_init(cwPitch, (float)targetRate, wpm);
        }
        if (decoder) {
            char outBuf[4096];
            // Feed in 1-second chunks
            int chunkSize = targetRate;
            for (int pos = 0; pos < outLen; pos += chunkSize) {
                int feedLen = (pos + chunkSize <= outLen) ? chunkSize : outLen - pos;
                int nchars = uhsdr_feed(decoder, pcm + pos, feedLen, outBuf, sizeof(outBuf));
                if (nchars > 0) {
                    outBuf[nchars] = '\0';
                    #pragma omp critical
                    {
                        printf("%.1f:%.0f:%d:%s\n", exactFreq, cwPitch, uhsdr_get_wpm(decoder), outBuf);
                        fflush(stdout);
                    }
                }
            }
            uhsdr_free(decoder);
        }
        free(pcm);

        processed++;

        if (processed % 10 == 0) {
            fprintf(stderr, "  %d/%d channels processed\n", processed, numChannels);
        }
    }

    fprintf(stderr, "Done: %d channels processed\n", processed);

    free(i_samples);
    free(q_samples);
    free(channelFreqs);
    return 0;
}
