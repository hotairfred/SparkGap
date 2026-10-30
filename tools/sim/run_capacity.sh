#!/bin/bash
# Capacity run on one computer: hpsdr_proxy.py replays a scene in real time to a SparkGap on the C
# receiver path (the live path, with one decode thread per band), next to a quiet second band, since
# the C path only runs with two bands or more. Prints CPU after a 3 minute warm-up, the drops in the
# Health lines, and the scene's score (score_truth.py, the RBN port only).
#
# usage: tools/sim/run_capacity.sh SCENE.wav CENTRE_KHZ TRUTH.json CONFIG.json OUTDIR [OVERRIDES_JSON]
#   e.g. tools/sim/run_capacity.sh dense/dense_7030kHz_192000_24bit.wav 7030 dense/dense.truth.json \
#            my.json runs/cap600 '{"itila_max_bins": 600}'
# CONFIG is your SparkGap config; the run gets its own copy with the proxy as the radio
# (127.0.0.1:$PROXY_PORT, listening on $LISTEN_PORT), the two bands on 2 receivers, telnet on 17300/17301, both recorders
# and FT8 off, then OVERRIDES on top. Run it from the checkout. The proxy shares the computer, so for
# CPU numbers it is fairest on a machine with a core to spare (it was on another host on #47).
set -eu
SCENE=$1 CENTRE=$2 TRUTH=$3 CONFIG=$4 OUT=$5 OVR=${6:-'{}'}
PROXY_PORT=${PROXY_PORT:-1034} LISTEN_PORT=${LISTEN_PORT:-1035} QUIET_KHZ=${QUIET_KHZ:-3590}
PY=${PYTHON:-python3}
mkdir -p "$OUT"
SECS=$($PY -c "import wave,sys; w=wave.open(sys.argv[1]); print(int(w.getnframes()/w.getframerate()))" "$SCENE")

QUIET="$OUT/quiet_${QUIET_KHZ}kHz_192000_24bit.wav"     # the second band: noise only, as long as the scene
if [ ! -f "$QUIET" ]; then
  echo "{\"center_khz\": $QUIET_KHZ, \"rate\": 192000, \"duration_s\": $SECS, \"seed\": 1, \"noise_dbfs\": -50, \"cells\": []}" \
    > "$OUT/quiet.json"
  $PY tools/sim/compose.py "$OUT/quiet.json" "$OUT/quiet" > /dev/null
fi

$PY - "$CONFIG" "$OUT/config.json" "$CENTRE" "$QUIET_KHZ" "$PROXY_PORT" "$LISTEN_PORT" "$OVR" <<'PY'
import json, sys
src, dst, centre, quiet, pport, lport, ovr = sys.argv[1:]
c = json.load(open(src))
c.update(sdr_ip='127.0.0.1', sdr_port=int(pport), hpsdr_listen_port=int(lport), max_receivers=2,
         bands=[int(float(centre) * 1000), int(float(quiet) * 1000)],
         telnet_port=17300, telnet_local_port=17301, enable_ft8=False, record_iq_dir='', record_wav='')
c.update(json.loads(ovr))
json.dump(c, open(dst, 'w'), indent=1)
PY

$PY hpsdr_proxy.py --port "$PROXY_PORT" --wav "$SCENE" --wav "$QUIET" --n-rx 2 --negate-q \
    > "$OUT/proxy.log" 2>&1 &
PROXY=$!
trap 'kill $PROXY 2>/dev/null || true' EXIT
until grep -q "waiting for consumers" "$OUT/proxy.log"; do
  kill -0 $PROXY 2>/dev/null || { cat "$OUT/proxy.log"; exit 1; }
  sleep 1
done

OMP_NUM_THREADS=1 timeout -k 10 $((SECS + 50)) $PY sparkgap.py --config "$OUT/config.json" > "$OUT/sparkgap.log" 2>&1 &
TO=$!
sleep 2
SG=$(pgrep -P $TO | head -1)                            # SparkGap itself, under timeout
: > "$OUT/cpu.txt"
while kill -0 $SG 2>/dev/null; do                       # its user + system ticks, children included
  echo "$(date +%s) $(awk '{print $14 + $15 + $16 + $17}' /proc/$SG/stat 2>/dev/null)" >> "$OUT/cpu.txt"
  sleep 30
done
wait $TO || true

$PY - "$OUT" <<'PY'
import os, re, sys
out = sys.argv[1]
tck = os.sysconf('SC_CLK_TCK')
pts = [(int(t), int(v) / tck) for t, v in (l.split() for l in open(os.path.join(out, 'cpu.txt')) if len(l.split()) == 2)]
seg = [p for p in pts if p[0] - pts[0][0] >= 180]
if len(seg) > 1:
    print('CPU after warm-up: %.2f cores (of %d)' % ((seg[-1][1] - seg[0][1]) / (seg[-1][0] - seg[0][0]), os.cpu_count()))
log = open(os.path.join(out, 'sparkgap.log'), errors='replace').read().splitlines()
health = [l for l in log if 'INFO Health:' in l]
if health:
    print('last Health:', health[-1].split('Health: ')[1])
    print('env_drops max %d' % max(int(m) for l in health for m in re.findall(r'env_drops=(\d+)', l)))
with open(os.path.join(out, 'rbn_port.log'), 'w') as f:      # score_truth counts LOCAL SPOT lines too
    f.write('\n'.join(l for l in log if 'LOCAL SPOT' not in l))
PY
$PY tools/sim/score_truth.py "$OUT/rbn_port.log" "$TRUTH"
