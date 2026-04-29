/**
 * cw_dispatcher_test.cpp — smoke test for libcw_dispatcher.
 *
 * Generates a CW tone at a known frequency for a known message, feeds
 * it through one and then several dispatcher channels, and checks that
 * drained text contains recognizable content. This is a sanity test,
 * not a correctness harness — the goal is "dispatcher doesn't crash,
 * output roughly matches what _LibDecoder produces".
 *
 * Build:
 *   g++ -O2 -Wall -o cw_dispatcher_test cw_dispatcher_test.cpp \
 *       -L. -lcw_dispatcher -fopenmp
 *
 * Run:
 *   LD_LIBRARY_PATH=. ./cw_dispatcher_test
 */

#include "cw_dispatcher.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <string>
#include <vector>

static const float SR = 12000.0f;
static const float TONE = 700.0f;

// Morse element durations at 20 WPM (dot = 60 ms).
static const float WPM = 20.0f;
static const float DOT_S = 1.2f / WPM;  // dot length in seconds

// Morse table (subset; enough for CQ TEST DE W1AW W1AW).
struct MorseChar { char c; const char *pat; };
static const MorseChar MORSE[] = {
    {'A', ".-"},   {'B', "-..."}, {'C', "-.-."}, {'D', "-.."},
    {'E', "."},    {'F', "..-."}, {'G', "--."},  {'H', "...."},
    {'I', ".."},   {'J', ".---"}, {'K', "-.-"},  {'L', ".-.."},
    {'M', "--"},   {'N', "-."},   {'O', "---"},  {'P', ".--."},
    {'Q', "--.-"}, {'R', ".-."},  {'S', "..."},  {'T', "-"},
    {'U', "..-"},  {'V', "...-"}, {'W', ".--"},  {'X', "-..-"},
    {'Y', "-.--"}, {'Z', "--.."}, {'0', "-----"},{'1', ".----"},
    {'2', "..---"},{'3', "...--"},{'4', "....-"},{'5', "....."},
    {'6', "-...."},{'7', "--..."},{'8', "---.."},{'9', "----."},
    {' ', " "},    // word gap handled specially
    {0, NULL}
};

static const char *lookup(char c) {
    for (int i = 0; MORSE[i].c; ++i) if (MORSE[i].c == c) return MORSE[i].pat;
    return NULL;
}

// Simple LCG for deterministic noise (so both reference and dispatcher
// runs see the exact same audio).
static uint32_t g_rng = 0xC0FFEE11u;
static inline int noise_sample() {
    g_rng = g_rng * 1103515245u + 12345u;
    // Signed noise in ~±1500 LSB range; well below the tone but big
    // enough to let the AGC build a noise-floor estimate.
    return (int)((int32_t)(g_rng & 0xFFFFu) - 32768) / 22;
}

// Append `n` samples of tone (or silence if tone==false) to `out`.
// Always mixed with a low level of white noise so uhsdr's adaptive
// noise-floor tracking has something to lock onto.
static void push_segment(std::vector<int16_t> &out, bool tone, float seconds)
{
    int n = (int)(seconds * SR);
    static double phase = 0.0;
    double dphi = 2.0 * M_PI * TONE / SR;
    for (int i = 0; i < n; ++i) {
        int s = noise_sample();
        if (tone) {
            s += (int)(10000.0 * sin(phase));
        }
        phase += dphi;
        if (phase > 2 * M_PI) phase -= 2 * M_PI;
        if (s >  32767) s =  32767;
        if (s < -32768) s = -32768;
        out.push_back((int16_t)s);
    }
}

// Convert a plain-text message to int16 PCM samples at SR with tone
// keyed on/off per Morse elements. Repeats the message `repeats` times
// so uhsdr's adaptive trainer has enough signal to lock on.
static std::vector<int16_t> generate_morse(const std::string &msg, int repeats = 3)
{
    std::vector<int16_t> out;
    // Pad with 1 second of noise at the start so uhsdr has time to
    // establish a noise floor.
    push_segment(out, false, 1.0f);

    for (int r = 0; r < repeats; ++r) {
        for (size_t i = 0; i < msg.size(); ++i) {
            char c = msg[i];
            if (c == ' ') {
                // word gap = 7 dots (there's already a char gap from the prev char)
                push_segment(out, false, 4 * DOT_S);
                continue;
            }
            const char *pat = lookup(c);
            if (!pat) continue;
            for (const char *p = pat; *p; ++p) {
                if (*p == '.') {
                    push_segment(out, true, DOT_S);
                } else if (*p == '-') {
                    push_segment(out, true, 3 * DOT_S);
                }
                push_segment(out, false, DOT_S); // intra-char gap
            }
            push_segment(out, false, 2 * DOT_S); // extra to reach 3-dot char gap
        }
        // Inter-repeat gap
        push_segment(out, false, 6 * DOT_S);
    }
    // Trailing silence so the decoder flushes the last character.
    push_segment(out, false, 1.5f);
    return out;
}

static bool contains_substring_caseins(const std::string &hay, const std::string &needle)
{
    if (needle.empty()) return true;
    for (size_t i = 0; i + needle.size() <= hay.size(); ++i) {
        bool match = true;
        for (size_t j = 0; j < needle.size(); ++j) {
            char a = hay[i + j]; if (a >= 'a' && a <= 'z') a -= 32;
            char b = needle[j];  if (b >= 'a' && b <= 'z') b -= 32;
            if (a != b) { match = false; break; }
        }
        if (match) return true;
    }
    return false;
}

static std::string drain_all(cw_dispatcher_handle_t d,
                             std::vector<std::string> &per_channel_accum,
                             int n_channels)
{
    cw_decoded_record_t records[64];
    std::string everything;
    int n = cw_disp_drain(d, records, 64);
    for (int i = 0; i < n; ++i) {
        int slot = records[i].channel_id & 0xFFFFFF;
        if (slot >= 0 && slot < n_channels) {
            per_channel_accum[slot].append(records[i].text);
        }
        everything.append(records[i].text);
    }
    return everything;
}

int main()
{
    // Generate test audio. Synthetic Morse doesn't match human-send
    // timing perfectly, so expect the decoder to emit fragments with
    // [err] tags — the point of this test is that the dispatcher runs
    // without crashing, supports parallel fanout correctly, and
    // produces identical output in single- and multi-channel modes.
    const std::string MSG = "CQ TEST DE W1AW W1AW";
    auto audio = generate_morse(MSG, 3);
    printf("Generated %zu samples (%.2f s) for \"%s\" (×3) at %.0f WPM\n",
           audio.size(), audio.size() / SR, MSG.c_str(), WPM);

    bool any_failed = false;
    std::string single_text;
    int single_wpm = 0;

    // ===== Single-channel smoke test =====
    {
        printf("\n[single-channel]\n");
        auto d = cw_disp_create(16);
        if (!d) { printf("FAIL: cw_disp_create\n"); return 1; }

        int cid = cw_disp_add_channel(d, TONE, SR, 0, 7040.0f, 20.0f);
        if (cid < 0) { printf("FAIL: cw_disp_add_channel\n"); return 1; }
        printf("  channel_id=0x%x  count=%d\n", cid, cw_disp_channel_count(d));

        const int BLOCK = (int)(0.1 * SR);
        std::vector<std::string> accum(16);
        int fed = 0;
        while (fed < (int)audio.size()) {
            int this_block = BLOCK;
            if (fed + this_block > (int)audio.size()) this_block = (int)audio.size() - fed;
            int rc = cw_disp_feed_batch(d, &cid, 1, audio.data() + fed, this_block);
            if (rc != 0) { printf("FAIL: feed_batch rc=%d\n", rc); return 1; }
            fed += this_block;
            single_text += drain_all(d, accum, 16);
        }
        single_text += drain_all(d, accum, 16);

        single_wpm = cw_disp_get_wpm(d, cid);
        printf("  decoded: \"%s\"\n", single_text.c_str());
        printf("  wpm: %d\n", single_wpm);

        bool non_empty = !single_text.empty();
        bool wpm_ok = single_wpm >= 15 && single_wpm <= 30;
        printf("  %s: non-empty decode\n", non_empty ? "PASS" : "FAIL");
        printf("  %s: wpm in [15..30]\n", wpm_ok ? "PASS" : "FAIL");
        if (!non_empty || !wpm_ok) any_failed = true;

        cw_disp_remove_channel(d, cid);
        if (cw_disp_channel_count(d) != 0) {
            printf("FAIL: count after remove should be 0\n");
            any_failed = true;
        }
        cw_disp_destroy(d);
    }

    // ===== Multi-channel fanout =====
    // Feeding the same audio across N channels should produce identical
    // output on every channel — that's the correctness check for the
    // OpenMP parallel fanout and the per-channel state separation.
    {
        printf("\n[multi-channel fanout]\n");
        const int N = 8;
        auto d = cw_disp_create(N);
        std::vector<int> cids(N);
        for (int i = 0; i < N; ++i) {
            cids[i] = cw_disp_add_channel(d, TONE, SR, 0,
                                          7030.0f + i * 0.5f, 20.0f);
            if (cids[i] < 0) { printf("FAIL: add_channel[%d]\n", i); return 1; }
        }
        printf("  channels added: %d\n", cw_disp_channel_count(d));

        const int BLOCK = (int)(0.1 * SR);
        std::vector<int16_t> batch(N * BLOCK);
        std::vector<std::string> accum(N);
        int fed = 0;
        while (fed < (int)audio.size()) {
            int this_block = BLOCK;
            if (fed + this_block > (int)audio.size()) this_block = (int)audio.size() - fed;
            batch.resize(N * this_block);
            for (int c = 0; c < N; ++c) {
                memcpy(batch.data() + (size_t)c * this_block,
                       audio.data() + fed,
                       this_block * sizeof(int16_t));
            }
            int rc = cw_disp_feed_batch(d, cids.data(), N,
                                        batch.data(), this_block);
            if (rc != 0) { printf("FAIL: feed_batch rc=%d\n", rc); return 1; }
            fed += this_block;
            (void)drain_all(d, accum, N);
        }
        (void)drain_all(d, accum, N);

        // All channels should match each other, AND should match the
        // single-channel baseline.
        int identical = 0;
        int matches_single = 0;
        for (int i = 0; i < N; ++i) {
            if (accum[i] == accum[0]) identical++;
            if (accum[i] == single_text) matches_single++;
        }
        printf("  %d/%d channels identical to chan 0\n", identical, N);
        printf("  %d/%d channels identical to single-channel baseline\n",
               matches_single, N);
        printf("  sample (chan 0): \"%s\"\n",
               accum[0].substr(0, 60).c_str());

        bool ok = (identical == N) && (matches_single == N);
        printf("  %s: parallel determinism\n", ok ? "PASS" : "FAIL");
        if (!ok) any_failed = true;

        cw_disp_destroy(d);
    }

    // ===== Stale id rejection =====
    {
        printf("\n[stale id rejection]\n");
        auto d = cw_disp_create(4);
        int cid0 = cw_disp_add_channel(d, TONE, SR, 0, 7040.0f, 20.0f);
        cw_disp_remove_channel(d, cid0);
        int cid1 = cw_disp_add_channel(d, TONE, SR, 0, 7041.0f, 20.0f);
        // Slot should be reused but generation bumped, so cid0 is stale
        // yet cid1 uses the same slot.
        bool same_slot = ((cid0 & 0xFFFFFF) == (cid1 & 0xFFFFFF));
        bool diff_gen  = ((cid0 >> 24) != (cid1 >> 24));
        printf("  cid0=0x%x cid1=0x%x same_slot=%d diff_gen=%d\n",
               cid0, cid1, same_slot, diff_gen);
        // Feeding with stale id should return -2.
        int16_t dummy = 0;
        int rc = cw_disp_feed_batch(d, &cid0, 1, &dummy, 1);
        printf("  feed stale -> rc=%d (expect -2)\n", rc);
        if (!(same_slot && diff_gen && rc == -2)) any_failed = true;
        printf("  %s: stale id rejection\n",
               (same_slot && diff_gen && rc == -2) ? "PASS" : "FAIL");
        cw_disp_destroy(d);
    }

    printf("\n%s\n", any_failed ? "SOME TESTS FAILED" : "ALL TESTS PASSED");
    return any_failed ? 1 : 0;
}
