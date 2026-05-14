#!/usr/bin/env python3
"""Compare libitila.so's C timing-cost vs itila_cw.py's Python cost on the
same audio. Both implementations should produce ~equivalent numbers; any
material divergence (>20% or sign-flip on the gating threshold) means the
C port has a bug to chase before trusting the production gate.

Methodology: scan a frequency range over a known WAV, for each freq with
a successful Python decode also feed the same envelope to the C library
and read its cost via itila_get_last_cost. Tabulate.
"""
import sys, os, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import ctypes as ct
import itila_cw

LIB = ct.CDLL('./libitila.so')
LIB.itila_create.restype = ct.c_void_p
LIB.itila_create.argtypes = [ct.c_int, ct.c_double]
LIB.itila_feed.restype = ct.c_char_p
LIB.itila_feed.argtypes = [ct.c_void_p, ct.POINTER(ct.c_double),
                            ct.c_int, ct.c_double, ct.c_double]
LIB.itila_free.argtypes = [ct.c_void_p]
LIB.itila_get_last_cost.restype = ct.c_double
LIB.itila_get_last_cost.argtypes = [ct.c_void_p]
LIB.itila_get_wpm.restype = ct.c_double
LIB.itila_get_wpm.argtypes = [ct.c_void_p]

WAV = '/mnt/atlas/skimmer/recordings/B1_seg2_15-30min_7090kHz.wav'
KEY = '/mnt/atlas/skimmer/recordings/B1_seg2_cq_key_56.txt'
CENTER = 7090.0
BAND_MIN = 7020.0
BAND_MAX = 7080.0
STEP = 0.1
START_SEC = 0.0
END_SEC = 120.0  # 2 minutes

with open(KEY) as f:
    gold = {c.strip().upper() for c in f.read().replace('\n',',').split(',') if c.strip()}

print(f"Loading IQ chunk {START_SEC}-{END_SEC}s ...")
iq_cache = itila_cw.load_iq_wav(WAV, start_sec=START_SEC, end_sec=END_SEC)

print("Scanning + comparing C vs Python costs:")
print(f"  {'freq':>7s}  {'wpm':>5s}  {'py_cost':>8s}  {'c_cost':>8s}  {'delta':>7s}  {'calls':<30s}")
print(f"  {'----':>7s}  {'---':>5s}  {'-------':>8s}  {'------':>8s}  {'-----':>7s}  {'-----':<30s}")

h_c = LIB.itila_create(200, 100.0)
freqs = np.arange(BAND_MIN, BAND_MAX, STEP)

rows = []
for freq in freqs:
    try:
        env, _ = itila_cw.read_iq_wav(WAV, CENTER, freq, iq_cache=iq_cache, lpf_hz=100)
    except Exception:
        continue
    # Python decode + cost
    py_result = itila_cw.decode_channel(env, CENTER, freq,
                                          evidence_threshold=10.0, verbose=False, lpf_hz=100)
    if not py_result or not py_result.get('callsigns'):
        continue
    py_cost = py_result.get('timing_cost', 999.0)
    py_wpm  = py_result.get('wpm', 0)
    calls   = ','.join(py_result['callsigns'][:3])

    # C decode + cost on the SAME envelope
    env_c = np.ascontiguousarray(env, dtype=np.float64)
    ptr = env_c.ctypes.data_as(ct.POINTER(ct.c_double))
    LIB.itila_feed(h_c, ptr, ct.c_int(len(env_c)),
                   ct.c_double(freq), ct.c_double(10.0))
    c_cost = LIB.itila_get_last_cost(h_c)

    delta = c_cost - py_cost if (py_cost < 998 and c_cost < 998) else float('nan')
    rows.append((freq, py_wpm, py_cost, c_cost, delta, calls,
                 any(c in gold for c in py_result['callsigns'])))

    print(f"  {freq:7.2f}  {py_wpm:5.1f}  {py_cost:8.3f}  {c_cost:8.3f}  "
          f"{delta:+7.3f}  {calls[:30]:<30s}{'  ★' if any(c in gold for c in py_result['callsigns']) else ''}")

LIB.itila_free(h_c)

# Summary
print(f"\n{len(rows)} comparison points")
diffs = [r[4] for r in rows if r[2] < 998 and r[3] < 998]
abs_diffs = [abs(d) for d in diffs]
if abs_diffs:
    print(f"  |C_cost - Py_cost|:  mean={sum(abs_diffs)/len(abs_diffs):.3f}  "
          f"max={max(abs_diffs):.3f}  median={sorted(abs_diffs)[len(abs_diffs)//2]:.3f}")

# Gate-divergence check: at threshold 30, do C and Python agree on keep/drop?
agree = 0; disagree = []
for freq, wpm, pc, cc, d, calls, is_gold in rows:
    if pc >= 998 or cc >= 998:
        continue
    py_keep = pc <= 30
    c_keep = cc <= 30
    if py_keep == c_keep:
        agree += 1
    else:
        disagree.append((freq, pc, cc, calls, is_gold))

total = agree + len(disagree)
if total:
    print(f"\nGate (cost<=30) agreement: {agree}/{total} ({100*agree/total:.1f}%)")
    if disagree:
        print(f"  Disagreements ({len(disagree)}):")
        for freq, pc, cc, calls, is_gold in disagree[:10]:
            print(f"    {freq:7.2f}  py={pc:6.2f} c={cc:6.2f}  {calls[:30]}{'  ★' if is_gold else ''}")
