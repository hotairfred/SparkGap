#include "bufmodule.hpp"

#include "csdr/ringbuffer.hpp"
#include "csdr/cw.hpp"
#include "fftw3.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>
#include <math.h>

/*
 * Multi-variant CW skimmer: accepts all tuning parameters via command line.
 * One binary handles all decoder variants, bandwidths, and thresholds.
 *
 * Usage: csdr-cwskimmer-multi [options] [infile [outfile]]
 *   -r <rate>      Sample rate (default 48000)
 *   -b <bandwidth> FFT bin width in Hz (default 50)
 *   -t <threshold> Signal threshold weight (default 6.0)
 *   -n <chars>     Min chars to print (default 8)
 *   -i             Use 16-bit signed integer input
 *   -v <variant>   Decoder variant: 0-5 (default 0 = original)
 *   -H <high>      Hysteresis high (default 0.7)
 *   -L <low>       Hysteresis low (default 0.5)
 *   -a <rate>      Adaptation divisor (default 4.0)
 *   -N <ms>        Noise blanking time ms (default 20)
 *   -D <filter>    Dit filter ratio (default 0.5)
 *   -M <max>       Dah max ratio (default 3.0)
 *   -C <break>     Char break ratio (default 2.5)
 *   -W <break>     Word break ratio (default 5.0)
 */

unsigned int sampleRate = 48000;
unsigned int bandwidth  = 50;
double thresWeight      = 6.0;
unsigned int printChars = 8;
bool use16bit = false;
bool showCw   = false;

// Decoder tuning parameters
double hysteresisHigh = 0.7;
double hysteresisLow  = 0.5;
double adaptRate      = 4.0;
unsigned int nbTimeMs = 20;
double ditFilter      = 0.5;
double dahMax         = 3.0;
double charBreak      = 2.5;
double wordBreak      = 5.0;
unsigned int launchDelay = 200;  // ms before emitting chars (AGC settle)

// Preset decoder variants
void setVariant(int v) {
    switch(v) {
        case 0: // V0: Original
            hysteresisHigh=0.7; hysteresisLow=0.5; adaptRate=4.0;
            nbTimeMs=20; ditFilter=0.5; dahMax=3.0; charBreak=2.5; wordBreak=5.0;
            break;
        case 1: // V1: Faster adaptation, tighter hysteresis
            hysteresisHigh=0.6; hysteresisLow=0.4; adaptRate=3.0;
            nbTimeMs=15; ditFilter=0.4; dahMax=3.5; charBreak=2.5; wordBreak=5.0;
            break;
        case 2: // V2: Conservative, patient
            hysteresisHigh=0.65; hysteresisLow=0.45; adaptRate=5.0;
            nbTimeMs=25; ditFilter=0.5; dahMax=3.0; charBreak=3.0; wordBreak=6.0;
            break;
        case 3: // V3: Aggressive weak signal
            hysteresisHigh=0.55; hysteresisLow=0.35; adaptRate=2.0;
            nbTimeMs=10; ditFilter=0.3; dahMax=4.0; charBreak=2.0; wordBreak=4.0;
            break;
        case 4: // V4: Ultra-conservative (slow CW, 10-15 WPM)
            hysteresisHigh=0.70; hysteresisLow=0.35; adaptRate=8.0;
            nbTimeMs=30; ditFilter=0.5; dahMax=3.0; charBreak=4.0; wordBreak=8.0;
            break;
        case 5: // V5: Speed demon (40+ WPM)
            hysteresisHigh=0.55; hysteresisLow=0.45; adaptRate=1.5;
            nbTimeMs=8; ditFilter=0.4; dahMax=4.0; charBreak=2.0; wordBreak=4.0;
            break;
        case 6: // V1.5: Interpolated between V1 and V2
            hysteresisHigh=0.62; hysteresisLow=0.42; adaptRate=4.0;
            nbTimeMs=18; ditFilter=0.45; dahMax=3.2; charBreak=2.8; wordBreak=5.5;
            break;
        case 7: // V2.5: Interpolated between V2 and V3
            hysteresisHigh=0.60; hysteresisLow=0.40; adaptRate=3.5;
            nbTimeMs=12; ditFilter=0.4; dahMax=3.5; charBreak=2.5; wordBreak=5.0;
            break;
    }
}

Csdr::Ringbuffer<unsigned char> **out;
Csdr::RingbufferReader<unsigned char> **outReader;
Csdr::BufferedModule<float, unsigned char> **cwDecoder;
unsigned int *outState;
float *snr;

void printOutput(FILE *outFile, int i, unsigned int freq, unsigned int printChars)
{
  int n = outReader[i]->available();
  if(n<printChars) return;

  fprintf(outFile, "%d:%d:", freq, (int)(20.0 * log10f(snr[i])));

  unsigned char *p = outReader[i]->getReadPointer();
  for(int j=0 ; j<n ; ++j)
  {
    switch(outState[i] & 0xFF)
    {
      case '\0':
        fprintf(outFile, "%c", p[j]);
        if(p[j]==' ') outState[i] = p[j];
        break;
      case ' ':
        if(strchr("TEI ", p[j])) outState[i] = p[j];
        else
        {
          fprintf(outFile, "%c", p[j]);
          outState[i] = '\0';
        }
        break;
      default:
        if(strchr("TEI ", p[j])) outState[i] = (outState[i]<<8) | p[j];
        else
        {
          for(int k=24 ; k>=0 ; k-=8)
            if((outState[i]>>k) & 0xFF)
              fprintf(outFile, "%c", (outState[i]>>k) & 0xFF);
          fprintf(outFile, "%c", p[j]);
          outState[i] = '\0';
        }
        break;
    }
  }

  outReader[i]->advance(n);
  printf("\n");
  fflush(outFile);
}

int main(int argc, char *argv[])
{
  FILE *inFile, *outFile;
  const char *inName, *outName;
  float accPower, avgPower, maxPower;
  int j, i, k, n;

  struct { float power; int count; } scales[16];

  for(j=1, inName=outName=0, inFile=stdin, outFile=stdout ; j<argc ; ++j)
  {
    if(argv[j][0]!='-')
    {
      if(!inName) inName = argv[j];
      else if(!outName) outName = argv[j];
      else { fprintf(stderr, "Excessive file name '%s'\n", argv[j]); return(2); }
    }
    else if(strlen(argv[j])!=2)
    {
      fprintf(stderr, "Unrecognized option '%s'\n", argv[j]); return(2);
    }
    else switch(argv[j][1])
    {
      case 'n': printChars = j<argc-1? atoi(argv[++j]) : printChars; break;
      case 'r': sampleRate = j<argc-1? atoi(argv[++j]) : sampleRate; break;
      case 'b': bandwidth  = j<argc-1? atoi(argv[++j]) : bandwidth;  break;
      case 't': thresWeight= j<argc-1? atof(argv[++j]) : thresWeight;break;
      case 'v': if(j<argc-1) setVariant(atoi(argv[++j])); break;
      case 'i': use16bit = true; break;
      case 'f': use16bit = false; break;
      case 'c': showCw = true; break;
      case 'H': hysteresisHigh = j<argc-1? atof(argv[++j]) : hysteresisHigh; break;
      case 'L': hysteresisLow  = j<argc-1? atof(argv[++j]) : hysteresisLow;  break;
      case 'a': adaptRate      = j<argc-1? atof(argv[++j]) : adaptRate;      break;
      case 'N': nbTimeMs       = j<argc-1? atoi(argv[++j]) : nbTimeMs;       break;
      case 'D': ditFilter      = j<argc-1? atof(argv[++j]) : ditFilter;      break;
      case 'M': dahMax         = j<argc-1? atof(argv[++j]) : dahMax;         break;
      case 'C': charBreak      = j<argc-1? atof(argv[++j]) : charBreak;      break;
      case 'W': wordBreak      = j<argc-1? atof(argv[++j]) : wordBreak;      break;
      case 'h':
        fprintf(stderr, "Multi-Variant CW Skimmer (Arc)\n");
        fprintf(stderr, "Usage: %s [options] [<infile> [<outfile>]]\n", argv[0]);
        fprintf(stderr, "  -r <rate>      Sample rate (default 48000)\n");
        fprintf(stderr, "  -b <bandwidth> FFT bin width Hz (default 50)\n");
        fprintf(stderr, "  -t <threshold> Threshold weight (default 6.0)\n");
        fprintf(stderr, "  -n <chars>     Min chars to print (default 8)\n");
        fprintf(stderr, "  -v <variant>   Preset variant 0-7 (default 0)\n");
        fprintf(stderr, "  -i             16-bit signed integer input\n");
        fprintf(stderr, "  -H/-L          Hysteresis high/low (0.7/0.5)\n");
        fprintf(stderr, "  -a <rate>      Adaptation divisor (4.0)\n");
        fprintf(stderr, "  -N <ms>        Noise blanking ms (20)\n");
        fprintf(stderr, "  -D/-M          Dit filter/Dah max (0.5/3.0)\n");
        fprintf(stderr, "  -C/-W          Char/Word break (2.5/5.0)\n");
        return(0);
      default:
        fprintf(stderr, "Unrecognized option '%s'\n", argv[j]); return(2);
    }
  }

  inFile = inName? fopen(inName, "rb") : stdin;
  if(!inFile) { fprintf(stderr, "Failed opening '%s'\n", inName); return(1); }
  outFile = outName? fopen(outName, "wb") : stdout;
  if(!outFile) { fprintf(stderr, "Failed opening '%s'\n", outName); if(inFile!=stdin) fclose(inFile); return(1); }

  unsigned int inputStep = sampleRate / bandwidth;
  unsigned int numChannels = inputStep / 2;

  fftwf_complex *fftOut = new fftwf_complex[inputStep];
  short *dataIn  = new short[inputStep];
  float *dataBuf = new float[inputStep];
  float *fftIn   = new float[inputStep];
  fftwf_plan fft = fftwf_plan_dft_r2c_1d(inputStep, fftIn, fftOut, FFTW_ESTIMATE);

  out       = new Csdr::Ringbuffer<unsigned char> *[numChannels];
  outReader = new Csdr::RingbufferReader<unsigned char> *[numChannels];
  cwDecoder = new Csdr::BufferedModule<float, unsigned char> *[numChannels];
  outState  = new unsigned int[numChannels];
  snr       = new float[numChannels];

  for(j=0 ; j<numChannels ; ++j)
  {
    out[j]       = new Csdr::Ringbuffer<unsigned char>(printChars*4);
    outReader[j] = new Csdr::RingbufferReader<unsigned char>(out[j]);
    cwDecoder[j] = new Csdr::BufferedModule<float, unsigned char>(
        new Csdr::CwDecoder<float>(sampleRate, showCw,
            hysteresisHigh, hysteresisLow, adaptRate, nbTimeMs,
            ditFilter, dahMax, charBreak, wordBreak, launchDelay),
        printChars*4);
    cwDecoder[j]->setWriter(out[j]);
    outState[j] = ' ';
    snr[j] = 0.0;
  }

  for(avgPower=4.0 ; ; )
  {
    if(!use16bit)
    {
      if(fread(dataBuf, sizeof(float), inputStep, inFile) != inputStep) break;
    }
    else
    {
      if(fread(dataIn, sizeof(short), inputStep, inFile) != inputStep) break;
      for(j=0 ; j<inputStep ; ++j)
        dataBuf[j] = (float)dataIn[j] / 32768.0;
    }

    double hk = 2.0 * M_PI / (inputStep-1);
    for(j=0 ; j<inputStep ; ++j)
      fftIn[j] = dataBuf[j] * (0.54 - 0.46 * cos(j * hk));

    fftwf_execute(fft);

    for(j=0 ; j<numChannels ; ++j)
      fftOut[j][0] = fftOut[j][1] = sqrt(fftOut[j][0]*fftOut[j][0] + fftOut[j][1]*fftOut[j][1]);

    memset(scales, 0, sizeof(scales));
    for(j=0, maxPower=0.0 ; j<numChannels ; ++j)
    {
      float v = fftOut[j][0];
      int scale = floor(log(v));
      scale = scale<0? 0 : scale+1>=16? 15 : scale+1;
      maxPower = fmax(maxPower, v);
      scales[scale].power += v;
      scales[scale].count++;
    }

    for(i=0, n=0, accPower=0.0 ; i<15 ; ++i)
    {
      for(k=i, j=i+1 ; j<16 ; ++j)
        if(scales[j].count>scales[k].count) k = j;
      if(k!=i)
      {
        float v = scales[k].power;
        j = scales[k].count;
        scales[k] = scales[i];
        scales[i].power = v;
        scales[i].count = j;
      }
      accPower += scales[i].power;
      n += scales[i].count;
      if(n>=numChannels/2) break;
    }

    accPower /= n;
    avgPower += (accPower - avgPower) * inputStep / sampleRate / 3;

    for(j=0 ; j<numChannels ; ++j)
    {
      float power = fftOut[j][0];
      accPower = power >= avgPower*thresWeight? 1.0 : 0.0;

      power = fmax(power / avgPower, 1.0);
      snr[j] += (power - snr[j]) * (power >= snr[j]? 0.25 : 0.05);

      Csdr::Ringbuffer<float> *in = cwDecoder[j]->buf();
      if(in->writeable()>=inputStep)
      {
        float *dst = in->getWritePointer();
        for(i=0 ; i<inputStep ; ++i) dst[i] = accPower;
        in->advance(inputStep);
        while(cwDecoder[j]->canProcess()) cwDecoder[j]->process();
        printOutput(outFile, j, j * bandwidth, printChars);
      }
    }
  }

  for(j=0 ; j<numChannels ; ++j)
    printOutput(outFile, j, j * bandwidth, 1);

  if(outFile!=stdout) fclose(outFile);
  if(inFile!=stdin)   fclose(inFile);

  fftwf_destroy_plan(fft);
  delete [] fftOut;
  delete [] fftIn;
  delete [] dataBuf;
  delete [] dataIn;

  for(j=0 ; j<numChannels ; ++j)
  {
    delete outReader[j];
    delete out[j];
    delete cwDecoder[j];
  }

  delete [] out;
  delete [] outReader;
  delete [] cwDecoder;
  delete [] outState;
  delete [] snr;

  return(0);
}
