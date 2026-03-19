#!/bin/bash
# Brute force multi-pass CW decoder — all variants × bandwidths × thresholds × inputs
# Usage: ./bruteforce.sh <input.wav> [sample_rate]

INPUT="$1"
SR="${2:-48000}"
SKIMMER="./csdr-cwskimmer-multi"
FILTER="python3 spot_filter.py"
OUTDIR="/tmp/bruteforce"
MASTER="MASTER.SCP"

if [ -z "$INPUT" ]; then
    echo "Usage: $0 <input.wav> [sample_rate]" >&2
    exit 1
fi

mkdir -p "$OUTDIR"

# Prepare input representations
echo "=== Preparing inputs ===" >&2
BASE=$(basename "$INPUT" .wav)

# Extract mono channels from stereo
sox "$INPUT" "${OUTDIR}/${BASE}_mono_I.wav" remix 1 2>/dev/null
sox "$INPUT" "${OUTDIR}/${BASE}_mono_Q.wav" remix 2 2>/dev/null
sox "$INPUT" "${OUTDIR}/${BASE}_magnitude.wav" remix 1v0.5 2v0.5 2>/dev/null

INPUTS=("$INPUT")
[ -f "${OUTDIR}/${BASE}_mono_I.wav" ] && INPUTS+=("${OUTDIR}/${BASE}_mono_I.wav")
[ -f "${OUTDIR}/${BASE}_mono_Q.wav" ] && INPUTS+=("${OUTDIR}/${BASE}_mono_Q.wav")
[ -f "${OUTDIR}/${BASE}_magnitude.wav" ] && INPUTS+=("${OUTDIR}/${BASE}_magnitude.wav")

echo "  Inputs: ${#INPUTS[@]} representations" >&2

# Parameters
VARIANTS=(0 1 2 3 4 5 6 7)
BANDWIDTHS=(50 60 75 80 90 100 110 120 125 135 150 175 200 250 300)
THRESHOLDS=(3.0 4.0 5.0 6.0 7.0 8.0 10.0 12.0 15.0)

TOTAL=$(( ${#VARIANTS[@]} * ${#BANDWIDTHS[@]} * ${#THRESHOLDS[@]} * ${#INPUTS[@]} ))
echo "=== Starting $TOTAL passes ===" >&2
echo "  Variants: ${VARIANTS[*]}" >&2
echo "  Bandwidths: ${BANDWIDTHS[*]}" >&2
echo "  Thresholds: ${THRESHOLDS[*]}" >&2

ALLRAW="$OUTDIR/all_raw.txt"
> "$ALLRAW"

COUNT=0
VALIDATED_PREV=0

for inp in "${INPUTS[@]}"; do
    INP_TAG=$(basename "$inp" .wav)
    for var in "${VARIANTS[@]}"; do
        for bw in "${BANDWIDTHS[@]}"; do
            for thr in "${THRESHOLDS[@]}"; do
                COUNT=$((COUNT + 1))

                # Run decoder
                cat "$inp" | $SKIMMER -r "$SR" -i -b "$bw" -t "$thr" -v "$var" -n 16 >> "$ALLRAW" 2>/dev/null

                # Progress every 100 passes
                if [ $((COUNT % 100)) -eq 0 ]; then
                    VALIDATED=$(cat "$ALLRAW" | $FILTER 2>/dev/null | grep "^SPOT:" | awk '{print $6}' | sort -u | wc -l)
                    echo "  Pass $COUNT/$TOTAL: $VALIDATED unique validated calls (+$((VALIDATED - VALIDATED_PREV)) since last)" >&2
                    VALIDATED_PREV=$VALIDATED
                fi
            done
        done
    done
done

# Final merge through filter
echo "" >&2
echo "=== Final Results ===" >&2
echo "  Total passes: $COUNT" >&2
echo "  Raw decode lines: $(wc -l < "$ALLRAW")" >&2

# Extract and validate
cat "$ALLRAW" | $FILTER 2>/dev/null | grep "^SPOT:" | awk '{print $6}' | sort -u > "$OUTDIR/validated.txt"
FINAL=$(wc -l < "$OUTDIR/validated.txt")
echo "  Validated unique calls: $FINAL" >&2

# Compare with CW Skimmer
if [ -f cwskimmer_spots.txt ]; then
    GOLD=$(grep -oP '[A-Z0-9]{1,2}\d{1,2}[A-Z]{1,3}' cwskimmer_spots.txt | sort -u)
    MATCH=$(comm -12 "$OUTDIR/validated.txt" <(echo "$GOLD") | wc -l)
    echo "  Match CW Skimmer: $MATCH" >&2
fi

cat "$OUTDIR/validated.txt"
