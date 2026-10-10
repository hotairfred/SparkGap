#!/usr/bin/env python3
"""Scripted CW contest "cell" generator: one runner + its callers, as receiver
audio plus a truth file, in the sparkgap-cell/1 format (see tools/sim/README.md).

Not a realistic operator model -- Morse Runner CE cells (robot runner) are the
realistic source. This gives the band composer and the truth scorer something
to run on, with exact ground truth, until those exist.

Keying is machine-perfect (contest CW is keyer/macro generated); each station
has its own WPM, pitch and level, plus optional slow QSB. A CWT-style flow:

    runner: CQ CWT <R>           (or, after a QSO, TU <R> which doubles as a CQ)
    1-N callers answer with their call (some overlap) after a short reaction time
    runner: <C> <name> <nr>      (answers one caller, the loudest)
    caller: TU <name> <nr>       (exchange; "TU <name> <nr>")
    runner: TU <R>               -> next callers, or a fresh CQ after a quiet gap

usage:
    cellgen.py OUT_BASENAME --runner K1ABC [--minutes 10] [--seed 1] [--wpm 28]
               [--callers-per-qso 1-3] [--pitch 600] [--qsb]
"""
import argparse
import json
import random
import wave

import numpy as np

RATE = 12000                    # 192000 / 16: the composer upsamples exactly
RISE_S = 0.005                  # raised-cosine key edges

MORSE = {
    'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.', 'G': '--.',
    'H': '....', 'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..', 'M': '--', 'N': '-.',
    'O': '---', 'P': '.--.', 'Q': '--.-', 'R': '.-.', 'S': '...', 'T': '-', 'U': '..-',
    'V': '...-', 'W': '.--', 'X': '-..-', 'Y': '-.--', 'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-', '5': '.....',
    '6': '-....', '7': '--...', '8': '---..', '9': '----.', '/': '-..-.', '?': '..--..',
}
NAMES = ['BOB', 'JIM', 'TOM', 'DAVE', 'MIKE', 'JOHN', 'BILL', 'ED', 'AL', 'RON', 'JOE',
         'DAN', 'RICK', 'GARY', 'KEN', 'STEVE', 'MARK', 'PAUL', 'FRED', 'ANN', 'SUE']


def key_envelope(text, wpm):
    """Machine-perfect keying envelope (0..1) at RATE for text at wpm (PARIS)."""
    dit = int(round(1.2 / wpm * RATE))
    on = []
    for wi, word in enumerate(text.split()):
        if wi:
            on.append((0, 7 * dit))
        for ci, ch in enumerate(word):
            if ci:
                on.append((0, 3 * dit))
            for ei, el in enumerate(MORSE.get(ch, '')):
                if ei:
                    on.append((0, dit))
                on.append((1, dit if el == '.' else 3 * dit))
    env = np.concatenate([np.full(n, v, np.float32) for v, n in on]) if on else np.zeros(0, np.float32)
    # raised-cosine key edges: smooth with a Hann kernel of 2*RISE_S
    r = max(1, int(RISE_S * RATE))
    k = np.hanning(2 * r + 1).astype(np.float32)
    return np.clip(np.convolve(env, k / k.sum(), mode='same'), 0.0, 1.0)


def text_seconds(text, wpm):
    return len(key_envelope(text, wpm)) / RATE


class Station:
    def __init__(self, call, role, wpm, pitch, level_db, name, nr, qsb, rng):
        self.call, self.role, self.wpm, self.pitch = call, role, wpm, pitch
        self.level_db, self.name, self.nr, self.qsb = level_db, name, nr, qsb
        self.phase = rng.uniform(0, 2 * np.pi)

    def amp(self):
        return 10 ** (self.level_db / 20.0)

    def info(self):
        d = dict(call=self.call, role=self.role, pitch_hz=round(self.pitch, 1),
                 level_db=round(self.level_db, 1), wpm=self.wpm, fist={'machine': True},
                 qsb=self.qsb, name=self.name, nr=self.nr)
        return d


def render(stations, msgs, duration_s):
    """msgs: (t0, station, text). Returns mono audio + message list with t1."""
    n = int(duration_s * RATE)
    out = np.zeros(n, np.float32)
    t = np.arange(n) / RATE
    done = []
    for t0, st, text in msgs:
        env = key_envelope(text, st.wpm)
        a = int(t0 * RATE)
        b = min(n, a + len(env))
        if b <= a:
            continue
        seg = env[:b - a] * st.amp()
        if st.qsb:
            seg = seg * (1.0 - st.qsb['depth'] * 0.5 * (1 + np.sin(
                2 * np.pi * t[a:b] / st.qsb['period_s'] + st.phase))).astype(np.float32)
        out[a:b] += seg * np.sin(2 * np.pi * st.pitch * t[a:b]).astype(np.float32)
        done.append(dict(t0=round(t0, 3), t1=round(b / RATE, 3), call=st.call, text=text))
    return out, done


def script_cell(runner_call, minutes, rng, call_pool, wpm=28, pitch=600.0, cpq=(1, 3),
                qsb=False, contest='CWT'):
    """Build stations + timed messages for a CWT-style cell."""
    dur = minutes * 60.0
    rq = dict(period_s=rng.uniform(6, 20), depth=rng.uniform(0.3, 0.8)) if qsb else None
    runner = Station(runner_call, 'runner', wpm, pitch, 0.0, rng.choice(NAMES),
                     str(rng.randint(1, 35000)), rq, rng)
    stations, msgs, qsos = [runner], [], []
    def kinds(kind, t0, st, text):
        msgs.append((t0, st, text, kind))
        return t0 + text_seconds(text, st.wpm)

    t = rng.uniform(0.5, 3.0)
    first = True
    while t < dur - 20:
        cq = f'CQ {contest} {runner.call}' if first else f'TU {runner.call}'
        t = kinds('cq' if first else 'tu', t, runner, cq)
        first = False
        k = rng.randint(*cpq) if rng.random() > 0.15 else 0
        if k == 0:                              # nobody: quiet gap, then a full CQ
            t += rng.uniform(2.0, 4.0)
            first = True
            continue
        callers = []
        for _ in range(k):
            c = call_pool.pop() if call_pool else f'K{rng.randint(0, 9)}{rng.choice("ABCDEFGHJK")}{rng.choice("ABCDEFGHJK")}'
            st = Station(c, 'caller', rng.choice([22, 24, 25, 26, 28, 30, 32]),
                         pitch + rng.uniform(-150, 150), rng.uniform(-12, 0), rng.choice(NAMES),
                         str(rng.randint(1, 35000)), None, rng)
            stations.append(st)
            callers.append(st)
        t_ans = t + rng.uniform(0.3, 1.2)
        ends = [kinds('call', t_ans + rng.uniform(0, 0.4), c, c.call) for c in callers]
        t = max(ends) + rng.uniform(0.3, 0.8)
        pick = max(callers, key=lambda c: c.level_db)          # loudest gets worked
        qs = t_ans
        t = kinds('exchange', t, runner, f'{pick.call} {runner.name} {runner.nr}') + rng.uniform(0.3, 0.9)
        t = kinds('exchange', t, pick, f'TU {pick.name} {pick.nr}') + rng.uniform(0.3, 0.8)
        qsos.append(dict(runner=runner.call, caller=pick.call, t0=round(qs, 3), t1=round(t, 3),
                         completed=True))
    return stations, msgs, qsos


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out', help='output basename (writes .wav + .json)')
    ap.add_argument('--runner', required=True)
    ap.add_argument('--minutes', type=float, default=10)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--wpm', type=int, default=28)
    ap.add_argument('--pitch', type=float, default=600.0)
    ap.add_argument('--callers-per-qso', default='1-3')
    ap.add_argument('--qsb', action='store_true')
    ap.add_argument('--calls', help='file of caller callsigns to draw from (one per line)')
    a = ap.parse_args()
    rng = random.Random(a.seed)
    pool = []
    if a.calls:
        pool = [l.strip().upper() for l in open(a.calls) if l.strip() and not l.startswith('#')]
        rng.shuffle(pool)
    lo, hi = (int(x) for x in a.callers_per_qso.split('-'))
    stations, msgs, qsos = script_cell(a.runner.upper(), a.minutes, rng, pool, a.wpm, a.pitch,
                                       (lo, hi), a.qsb)
    audio, done = render(stations, [(t0, st, text) for t0, st, text, _k in msgs], a.minutes * 60.0)
    kinds = {(round(t0, 3), st.call): k for t0, st, _x, k in msgs}
    for d in done:
        d['kind'] = kinds.get((d['t0'], d['call']), 'other')
    peak = float(np.abs(audio).max()) or 1.0
    pcm = (audio / peak * 0.5 * 32767).astype('<i2')
    w = wave.open(a.out + '.wav', 'wb')
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(pcm.tobytes()); w.close()
    truth = dict(format='sparkgap-cell/1', generator='tools/sim/cellgen.py', seed=a.seed,
                 sample_rate=RATE, duration_s=a.minutes * 60.0, contest='CWT',
                 noise_included=False, audio_peak_scale=round(0.5 / peak, 6),
                 stations=[s.info() for s in stations], messages=done, qsos=qsos)
    json.dump(truth, open(a.out + '.json', 'w'), indent=1)
    print(f'{a.out}: {len(stations)} stations, {len(done)} messages, {len(qsos)} QSOs')


if __name__ == '__main__':
    main()
