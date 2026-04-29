#!/bin/bash
# Run bmorse speed sweep — wait for speed 25 to finish, then run remaining speeds 4 at a time
# Usage: nohup ./run_speed_sweep.sh &

SKIMMER="./bmorse-skimmer"
INPUT="/tmp/cwt_15min.wav"
OUTDIR="/home/fred/csdr-skimmer"

echo "=== BMORSE SPEED SWEEP (C++) ===" >&2
echo "Waiting for speed 25 to finish (PID $1)..." >&2

# Wait for speed 25 if PID provided
if [ -n "$1" ]; then
    while kill -0 "$1" 2>/dev/null; do
        sleep 30
    done
    echo "Speed 25 complete." >&2
fi

# Run remaining speeds, 4 at a time
SPEEDS="15 18 20 22 28 30 33 35 38 40 45"
echo "Running speeds: $SPEEDS (4 concurrent)" >&2

run_speed() {
    local spd=$1
    echo "  Starting speed $spd WPM..." >&2
    $SKIMMER -s $spd $INPUT > ${OUTDIR}/bmorse_cpp_s${spd}.txt 2>${OUTDIR}/bmorse_cpp_s${spd}_err.txt
    echo "  Speed $spd DONE ($(wc -c < ${OUTDIR}/bmorse_cpp_s${spd}.txt) bytes output)" >&2
}

export -f run_speed
export SKIMMER INPUT OUTDIR

echo "$SPEEDS" | tr ' ' '\n' | xargs -P4 -I{} bash -c 'run_speed {}'

echo "" >&2
echo "=== ALL SPEEDS COMPLETE ===" >&2

# Merge all results
echo "Merging results..." >&2
cat ${OUTDIR}/bmorse_cpp_s*.txt > ${OUTDIR}/bmorse_cpp_all_merged.txt
echo "Total merged output: $(wc -c < ${OUTDIR}/bmorse_cpp_all_merged.txt) bytes" >&2
echo "Output files:" >&2
ls -lh ${OUTDIR}/bmorse_cpp_s*.txt >&2
