"""Record DAX-IQ from a FlexRadio for the voice skimmer: one panadapter and DAX-IQ channel per band,
complex float32 (I, Q interleaved) at 192 kHz per band, plus a .json sidecar (centre_hz, start_utc).
live_voice.py --follow reads a recording as it grows.

usage: flex_record.py RADIO_IP OUTDIR SECONDS CH:FREQ_HZ [CH:FREQ_HZ ...]   (e.g. auto:14246000 auto:7200000)
CH "auto" takes the first DAX-IQ channel (1-4) the radio doesn't refuse: one held by another station
(SmartSDR, DAX, another program) stays theirs. A 192 kHz stream takes 8 of a radio's 16 DAX-IQ units,
so two at most. Uses flex_iq.FlexIQReceiver, so the radio sees one SparkGap station (flex_station.py).
"""
import json
import logging
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flex_iq import FlexIQReceiver   # noqa: E402

RATE = 192000

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('flex_record')


def main():
    if len(sys.argv) < 5:
        raise SystemExit(__doc__)
    radio, outdir, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
    specs = [(s.split(':')[0], int(s.split(':')[1])) for s in sys.argv[4:]]
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%SZ', time.gmtime())
    rxs, threads, files, counts = [], [], [], []
    used = set()
    for k, (want, freq) in enumerate(specs):
        for ch in ([int(want)] if want != 'auto' else [c for c in (1, 2, 3, 4) if c not in used]):
            rx = FlexIQReceiver(radio, freq_hz=freq, sample_rate=RATE, daxiq_channel=ch,
                                udp_port=7791 + k)
            try:
                rx.start()
                break
            except RuntimeError as e:
                log.info('DAX-IQ %d not available (%s); trying the next', ch, e)
        else:
            raise SystemExit('no free DAX-IQ channel for %d Hz' % freq)
        used.add(ch)
        path = os.path.join(outdir, '%s_%dkHz_ch%d.cf32' % (stamp, freq // 1000, ch))
        f = open(path, 'wb')
        n = [0]

        def cb(_idx, iq, f=f, n=n):
            a = np.asarray(iq, dtype=np.float32).reshape(-1, 2)
            f.write(a.tobytes())
            n[0] += len(a)
        t = threading.Thread(target=rx.receive, args=(cb,), kwargs={'duration': seconds}, daemon=True)
        t.start()
        rxs.append(rx)
        threads.append(t)
        files.append((path, f, freq, ch))
        counts.append(n)
        json.dump({'centre_hz': freq, 'rate': RATE, 'channel': ch, 'start_utc': stamp, 'format': 'cf32 I,Q'},
                  open(path + '.json', 'w'))
    for t in threads:
        t.join(seconds + 30)
    for rx in rxs:
        rx.stop()
    for (path, f, freq, ch), n in zip(files, counts):
        f.close()
        log.info('%s: %d samples = %.1f s', path, n[0], n[0] / RATE)


if __name__ == '__main__':
    main()
