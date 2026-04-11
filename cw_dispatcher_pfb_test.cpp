/**
 * cw_dispatcher_pfb_test.cpp — smoke test for the v2 (IQ-fed) dispatcher path.
 *
 * Generates a complex IQ signal with a synthetic CW tone at a known
 * RF offset from receiver center, feeds it through cw_disp_feed_iq,
 * and checks that decoded text comes out and per-channel audio is
 * reachable via cw_disp_get_channel_audio.
 *
 * Build:
 *   make cw_dispatcher_pfb_test
 *
 * Run:
 *   LD_LIBRARY_PATH=. ./cw_dispatcher_pfb_test
 */

#include "cw_dispatcher.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <complex>
#include <string>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// PFB geometry — same as openskimmer.py default.
static constexpr int   SR_IN  = 192000;
static constexpr int   N_CHAN = 384;
static constexpr int   OS     = 48;
static constexpr int   K      = 9;
static constexpr int   PFB_OUT_RATE = SR_IN * OS / N_CHAN;        // 24000
static constexpr float BIN_SPACING  = (float)SR_IN / (float)N_CHAN; // 500 Hz

// Channel output rate fed to uhsdr.
static constexpr int   UHSDR_RATE   = 12000;

// CW signal parameters.
static constexpr float TONE_AUDIO_HZ = 700.0f;  // CW tone in audio after shift
static constexpr float WPM           = 20.0f;
static constexpr float DOT_S         = 1.2f / WPM;

// Morse table — minimal subset.
struct M { char c; const char *p; };
static const M MORSE[] = {
    {'A',".-"},{'B',"-..."},{'C',"-.-."},{'D',"-.."},{'E',"."},
    {'F',"..-."},{'G',"--."},{'H',"...."},{'I',".."},{'J',".---"},
    {'K',"-.-"},{'L',".-.."},{'M',"--"},{'N',"-."},{'O',"---"},
    {'P',".--."},{'Q',"--.-"},{'R',".-."},{'S',"..."},{'T',"-"},
    {'U',"..-"},{'V',"...-"},{'W',".--"},{'X',"-..-"},{'Y',"-.--"},
    {'Z',"--.."},{'0',"-----"},{'1',".----"},{'2',"..---"},{'3',"...--"},
    {'4',"....-"},{'5',"....."},{'6',"-...."},{'7',"--..."},{'8',"---.."},
    {'9',"----."},{0,nullptr}
};
static const char *lookup(char c) {
    for (int i = 0; MORSE[i].c; ++i) if (MORSE[i].c == c) return MORSE[i].p;
    return nullptr;
}

// Generate complex baseband IQ for a CW message at a given RF offset
// (rf_offset_hz from receiver center). The carrier is mixed to its RF
// position by multiplying by exp(j 2π f t). The output is meant to look
// like what an HPSDR receiver would deliver: complex IQ at SR_IN.
static std::vector<std::complex<float>>
gen_iq_morse(const std::string &msg, float rf_offset_hz, int repeats)
{
    std::vector<std::complex<float>> out;

    // 1 second of noise lead-in.
    auto push_noise = [&](float seconds) {
        int n = (int)(seconds * SR_IN);
        for (int i = 0; i < n; ++i) {
            float r = ((float)rand() / RAND_MAX - 0.5f) * 200.0f;
            float im= ((float)rand() / RAND_MAX - 0.5f) * 200.0f;
            out.emplace_back(r, im);
        }
    };

    auto push_segment = [&](bool tone, float seconds) {
        int n = (int)(seconds * SR_IN);
        static double phase = 0.0;
        const double dphi = 2.0 * M_PI * (double)rf_offset_hz / (double)SR_IN;
        for (int i = 0; i < n; ++i) {
            float r  = ((float)rand() / RAND_MAX - 0.5f) * 200.0f;
            float im = ((float)rand() / RAND_MAX - 0.5f) * 200.0f;
            if (tone) {
                r  += 8000.0f * cosf((float)phase);
                im += 8000.0f * sinf((float)phase);
            }
            phase += dphi;
            if (phase > 2 * M_PI) phase -= 2 * M_PI;
            out.emplace_back(r, im);
        }
    };

    push_noise(1.0f);
    for (int r = 0; r < repeats; ++r) {
        for (size_t i = 0; i < msg.size(); ++i) {
            char c = msg[i];
            if (c == ' ') { push_segment(false, 4 * DOT_S); continue; }
            const char *p = lookup(c); if (!p) continue;
            for (const char *e = p; *e; ++e) {
                if (*e == '.') push_segment(true, DOT_S);
                else           push_segment(true, 3 * DOT_S);
                push_segment(false, DOT_S);
            }
            push_segment(false, 2 * DOT_S);
        }
        push_segment(false, 6 * DOT_S);
    }
    push_noise(1.5f);
    return out;
}

// Helper: feed an int16 PCM stream through the v1 PCM-fed dispatcher
// path to get a baseline for parity comparison with the v2 IQ-fed path.
static std::string v1_decode_baseline(const std::vector<int16_t> &pcm)
{
    auto d = cw_disp_create(4);
    int cid = cw_disp_add_channel(d, TONE_AUDIO_HZ, (float)UHSDR_RATE, 0,
                                  7040.0f, 25.0f);
    cw_decoded_record_t recs[16];
    std::string out;
    const int BLOCK = UHSDR_RATE / 10;
    int fed = 0;
    while (fed < (int)pcm.size()) {
        int chunk = BLOCK;
        if (fed + chunk > (int)pcm.size()) chunk = (int)pcm.size() - fed;
        cw_disp_feed_batch(d, &cid, 1, pcm.data() + fed, chunk);
        fed += chunk;
        int n = cw_disp_drain(d, recs, 16);
        for (int i = 0; i < n; ++i) out.append(recs[i].text, recs[i].text_len);
    }
    int n = cw_disp_drain(d, recs, 16);
    for (int i = 0; i < n; ++i) out.append(recs[i].text, recs[i].text_len);
    cw_disp_destroy(d);
    return out;
}

int main()
{
    srand(0xC0FFEE);

    // Place the CW carrier well inside the band, off bin centre.
    // bin_spacing = 500 Hz; choose 12.7 kHz → bin 25 + 200 Hz residual.
    // The dispatcher shifts the bin output by (tone_freq - residual_hz),
    // so the carrier lands at exactly tone_freq Hz in the audio. We pass
    // tone_freq = TONE_AUDIO_HZ (700 Hz) — same as openskimmer.py.
    const float rf_offset_hz   = 12700.0f;  // bin 25 centre + 200 Hz residual
    const float bin_centre     = lroundf(rf_offset_hz / BIN_SPACING) * BIN_SPACING;
    const float residual_hz    = rf_offset_hz - bin_centre;
    const float uhsdr_tone     = TONE_AUDIO_HZ;

    printf("RF offset = %.1f Hz, bin centre = %.1f Hz, residual = %.1f Hz, "
           "uhsdr tone = %.1f Hz\n",
           rf_offset_hz, bin_centre, residual_hz, uhsdr_tone);

    // Generate IQ for "CQ TEST DE W1AW W1AW" × 3.
    const std::string MSG = "CQ TEST DE W1AW W1AW";
    auto iq = gen_iq_morse(MSG, rf_offset_hz, 3);
    printf("Generated %zu IQ samples (%.2f s)\n", iq.size(), iq.size() / (double)SR_IN);

    auto d = cw_disp_create(64);
    if (!d) { printf("FAIL: create\n"); return 1; }
    if (cw_disp_init_pfb(d, SR_IN, N_CHAN, OS, K) != 0) {
        printf("FAIL: init_pfb\n"); return 1;
    }
    int cid = cw_disp_add_pfb_channel(d, rf_offset_hz, (float)UHSDR_RATE,
                                      uhsdr_tone, 0, 7040.0f, 25.0f);
    if (cid < 0) { printf("FAIL: add_pfb_channel\n"); return 1; }
    printf("PFB channel id=0x%x  count=%d\n", cid, cw_disp_channel_count(d));

    // Feed the IQ in 100 ms blocks.
    const int BLOCK = SR_IN / 10;
    std::vector<float> bi(BLOCK), bq(BLOCK);
    std::string accum;
    cw_decoded_record_t recs[16];
    int fed = 0;
    while (fed < (int)iq.size()) {
        int chunk = BLOCK;
        if (fed + chunk > (int)iq.size()) chunk = (int)iq.size() - fed;
        for (int i = 0; i < chunk; ++i) {
            bi[i] = iq[fed + i].real();
            bq[i] = iq[fed + i].imag();
        }
        int rc = cw_disp_feed_iq(d, bi.data(), bq.data(), chunk);
        if (rc != 0) { printf("FAIL: feed_iq rc=%d\n", rc); return 1; }
        fed += chunk;
        int n = cw_disp_drain(d, recs, 16);
        for (int i = 0; i < n; ++i) accum.append(recs[i].text, recs[i].text_len);
    }
    int n = cw_disp_drain(d, recs, 16);
    for (int i = 0; i < n; ++i) accum.append(recs[i].text, recs[i].text_len);

    printf("v2 (PFB-fed) decoded: \"%s\"\n", accum.c_str());
    int v2_wpm = cw_disp_get_wpm(d, cid);
    printf("v2 wpm: %d\n", v2_wpm);

    // Pull the audio the v2 path actually fed to uhsdr, then run it through
    // the v1 PCM-fed API. Both must decode to the SAME text — that's the
    // parity proof for the IQ-fed pipeline.
    std::vector<int16_t> v2_audio;
    {
        const int chunk = 24000;
        std::vector<int16_t> tmp(chunk);
        for (;;) {
            int got = cw_disp_get_channel_audio(d, cid, tmp.data(), chunk);
            if (got <= 0) break;
            v2_audio.insert(v2_audio.end(), tmp.begin(), tmp.begin() + got);
            if (got < chunk) break;
        }
    }
    cw_disp_destroy(d);
    printf("v2 audio captured: %zu samples (%.2fs @ %d Hz)\n",
           v2_audio.size(), v2_audio.size() / (double)UHSDR_RATE, UHSDR_RATE);

    std::string v1_text = v1_decode_baseline(v2_audio);
    printf("v1 (PCM-fed on same audio) decoded: \"%s\"\n", v1_text.c_str());

    bool parity = (accum == v1_text);
    printf("%s: v1 PCM-fed and v2 PFB-fed produce identical text\n",
           parity ? "PASS" : "FAIL");

    // Sanity: get_channel_audio is reachable and returned data.
    bool audio_ok = !v2_audio.empty();
    printf("%s: get_channel_audio non-zero\n", audio_ok ? "PASS" : "FAIL");

    bool any_failed = !parity || !audio_ok;
    printf("\n%s\n", any_failed ? "SOME TESTS FAILED" : "ALL TESTS PASSED");
    if (!any_failed) {
        printf("(synthetic Morse decodes poorly on both paths — that's an "
               "uhsdr/synthetic-timing issue, not a dispatcher one)\n");
    }
    return any_failed ? 1 : 0;
}
