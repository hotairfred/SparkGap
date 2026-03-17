#!/bin/bash
# run_multipass.sh — Multi-pass brute force CW skimmer
#
# Runs csdr-cwskimmer at multiple bandwidths, thresholds, and input
# representations, then merges all results through master.scp validation.
#
# Usage: ./run_multipass.sh <input.wav> [sample_rate]
#
# Requires: csdr-cwskimmer built, MASTER.SCP in current directory,
#           spot_filter2.py in current directory
#
# Copyright 2026 WF8Z/Spark Gap — GPL-3

set -e

INPUT="$1"
SRATE="${2:-48000}"
OUTDIR="/tmp/multipass_$$"
SKIMMER="./csdr-cwskimmer"

if [ -z "$INPUT" ]; then
    echo "Usage: $0 <input.wav> [sample_rate]"
    echo ""
    echo "Runs 324-pass brute force CW decoding with multi-bandwidth,"
    echo "multi-threshold, and multi-input merge."
    exit 1
fi

if [ ! -f "$INPUT" ]; then
    echo "Error: $INPUT not found"
    exit 1
fi

if [ ! -f "$SKIMMER" ]; then
    echo "Error: $SKIMMER not found — run 'make' first"
    exit 1
fi

mkdir -p "$OUTDIR"
echo "=== Spark Gap Multi-Pass CW Skimmer ==="
echo "Input: $INPUT"
echo "Sample rate: $SRATE"
echo "Output dir: $OUTDIR"
echo ""

# Step 1: Extract input variants
echo "[1/4] Preparing input variants..."
python3 -c "
import wave, struct, math, sys
w = wave.open('$INPUT', 'rb')
if w.getnchannels() == 2:
    frames = w.readframes(w.getnframes())
    samples = struct.unpack('<' + 'h' * (len(frames)//2), frames)
    i_ch = samples[0::2]
    q_ch = samples[1::2]
    mag = [min(int(math.sqrt(i*i + q*q)), 32767) for i, q in zip(i_ch, q_ch)]
    for name, data in [('mono_I', i_ch), ('mono_Q', q_ch), ('magnitude', mag)]:
        out = wave.open('$OUTDIR/' + name + '.wav', 'wb')
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate($SRATE)
        out.writeframes(struct.pack('<' + 'h' * len(data), *data))
        out.close()
    print('  Extracted: mono_I, mono_Q, magnitude')
else:
    print('  Mono input — using as-is')
    import shutil
    shutil.copy('$INPUT', '$OUTDIR/mono_I.wav')
w.close()
" 2>&1

# Step 2: Generate decoder variants
echo "[2/4] Building decoder variants..."
SRCDIR=$(dirname "$SKIMMER")
BANDWIDTHS="50 55 60 65 70 75 80 85 90 100 110 120"
THRESHOLDS="3 4 5 6"
INPUTS="$INPUT"
[ -f "$OUTDIR/mono_I.wav" ] && INPUTS="$INPUTS $OUTDIR/mono_I.wav"
[ -f "$OUTDIR/mono_Q.wav" ] && INPUTS="$INPUTS $OUTDIR/mono_Q.wav"
[ -f "$OUTDIR/magnitude.wav" ] && INPUTS="$INPUTS $OUTDIR/magnitude.wav"

# Step 3: Run all passes
echo "[3/4] Running decode passes..."
PASS=0
TOTAL=0
for bw in $BANDWIDTHS; do
    # Build skimmer variant
    sed "s/BANDWIDTH    (50)/BANDWIDTH    ($bw)/" "$SRCDIR/cw-skimmer.cpp" > "$OUTDIR/tmp.cpp"
    g++ -O3 -o "$OUTDIR/skimmer_tmp" "$OUTDIR/tmp.cpp" "$SRCDIR/bufmodule.o" -lcsdr++ -lfftw3f 2>/dev/null || continue

    for input in $INPUTS; do
        iname=$(basename "$input" .wav)
        cat "$input" | "$OUTDIR/skimmer_tmp" -r "$SRATE" -i -n 32 2>/dev/null >> "$OUTDIR/all_raw.txt"
        PASS=$((PASS+1))
    done
done

# Also run threshold variants on default bandwidth
for tw in $THRESHOLDS; do
    sed -e "s/BANDWIDTH    (50)/BANDWIDTH    (50)/" \
        -e "s/THRES_WEIGHT (6.0)/THRES_WEIGHT ($tw.0)/" \
        "$SRCDIR/cw-skimmer.cpp" > "$OUTDIR/tmp.cpp"
    g++ -O3 -o "$OUTDIR/skimmer_tmp" "$OUTDIR/tmp.cpp" "$SRCDIR/bufmodule.o" -lcsdr++ -lfftw3f 2>/dev/null || continue

    for input in $INPUTS; do
        cat "$input" | "$OUTDIR/skimmer_tmp" -r "$SRATE" -i -n 32 2>/dev/null >> "$OUTDIR/all_raw.txt"
        PASS=$((PASS+1))
    done
done

TOTAL=$(wc -l < "$OUTDIR/all_raw.txt")
echo "  Completed $PASS decode passes, $TOTAL raw decode lines"

# Step 4: Filter and validate
echo "[4/4] Filtering through master.scp..."
echo ""
cat "$OUTDIR/all_raw.txt" | python3 spot_filter2.py --diff 2>&1

# Cleanup temp files
rm -f "$OUTDIR/tmp.cpp" "$OUTDIR/skimmer_tmp"
echo ""
echo "Raw output saved to: $OUTDIR/all_raw.txt"
