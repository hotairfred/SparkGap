#!/bin/bash
# Pull RBN daily spot data from data.reversebeacon.net
# Usage: rbn_pull.sh [YYYYMMDD]  (defaults to yesterday)
# Data lands in rbn_data/ as both .zip and .csv

RBNDIR="$(dirname "$0")/rbn_data"
mkdir -p "$RBNDIR"

if [ -n "$1" ]; then
    DATE="$1"
else
    DATE=$(date -d yesterday +%Y%m%d)
fi

URL="https://data.reversebeacon.net/rbn_history/${DATE}.zip"
ZIP="$RBNDIR/${DATE}.zip"
CSV="$RBNDIR/${DATE}.csv"

if [ -f "$CSV" ]; then
    echo "$CSV already exists ($(wc -l < "$CSV") records)"
    exit 0
fi

echo "Pulling RBN data for $DATE..."
curl -sf "$URL" -o "$ZIP"
if [ $? -ne 0 ]; then
    echo "Failed to download $URL (may not be available yet)"
    rm -f "$ZIP"
    exit 1
fi

unzip -o -d "$RBNDIR" "$ZIP"
rm -f "$ZIP"

RECORDS=$(wc -l < "$CSV")
echo "Done: $CSV ($RECORDS records)"
