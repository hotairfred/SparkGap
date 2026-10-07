"""All-band IQ recorder for the C receiver path (benchmark captures).

The production multi-band path (libhpsdr_fast C worker) never calls the
Python IQ callback that `record_wav` hooks, so `record_wav` records nothing
there. This recorder taps the per-band minute snapshot the FT8/RTTY path
already takes (`hpsdr_ft8_swap_read`): every swap returns ALL samples of that
band since the previous swap, so appending each swap gives a contiguous
recording of every band.

Config:
    "record_iq_dir": "/home/sparkgap/iqrec",   # enables the recorder
    "record_iq_bits": 24                       # 16 or 24 (default 24)

Recording only happens while the file <record_iq_dir>/RECORD exists, so a
capture can be started and stopped without restarting the skimmer:
    touch /home/sparkgap/iqrec/RECORD    # start (files open at the next minute)
    rm    /home/sparkgap/iqrec/RECORD    # stop  (files close at the next minute)
Each session writes one stereo (I,Q) WAV per band:
    <dir>/<UTC stamp>_<band kHz>kHz_<rate>_<bits>bit.wav
A WAV header holds 32-bit sizes (4 GiB of audio, about 62 minutes at
192 kHz 24-bit stereo), so a band that reaches MAX_DATA_BYTES continues in
..._p2.wav, ..._p3.wav: contiguous, nothing dropped.
Gaps between consecutive swaps (t_first discontinuities) are logged.

Writing happens on a background thread so the decode loop never waits on
the disk. 8 bands x 192 kHz x 24-bit = ~9 MB/s (~33 GB/hour).
"""
import datetime
import logging
import os
import queue
import threading
import wave

import numpy as np

log = logging.getLogger('sparkgap')

GAP_TOL_S = 0.05
MAX_DATA_BYTES = 4_000_000_000    # under the WAV format's 2**32 - 1 byte limit


def pack_iq(i, q, bits, in_scale=8388608.0):
    """float I/Q with full scale ±in_scale -> interleaved little-endian PCM.

    The FT8 swap buffers hold the Pitaya's raw 24-bit sample values as
    floats (hpsdr_fast.c: b->i[n] = (float)iv), i.e. in_scale = 2^23; a
    float32 represents those integers exactly, so 24-bit output is lossless."""
    full = (1 << (bits - 1)) - 1
    x = np.empty(2 * len(i), dtype=np.float64)
    x[0::2] = i
    x[1::2] = q
    v = np.clip(np.rint(x * ((full + 1) / in_scale)), -full - 1, full).astype('<i4')
    if bits == 16:
        return v.astype('<i2').tobytes()
    return v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()     # 24-bit


class BandRecorder:
    def __init__(self, rec_dir, bits=24, rate=192000, max_queue=32, in_scale=8388608.0):
        if bits not in (16, 24):
            raise ValueError('record_iq_bits must be 16 or 24')
        self.dir, self.bits, self.rate, self.in_scale = rec_dir, bits, rate, in_scale
        os.makedirs(rec_dir, exist_ok=True)
        self._q = queue.Queue(maxsize=max_queue)
        self._writers = {}            # band_khz -> (wave, next_expected_t, data bytes, part)
        self.max_data_bytes = MAX_DATA_BYTES
        self._session = None          # UTC stamp of the open session
        self.dropped = 0
        threading.Thread(target=self._run, name='iq-recorder', daemon=True).start()
        log.info("IQ recorder ready: %s (%d-bit); touch %s to record",
                 rec_dir, bits, os.path.join(rec_dir, 'RECORD'))

    def active(self):
        return os.path.exists(os.path.join(self.dir, 'RECORD'))

    def submit(self, band_khz, t_first, fi, fq, n):
        """Called once per band per minute swap. fi/fq must not be reused by
        the caller afterwards (the FT8 path allocates them fresh each swap)."""
        item = ('data', band_khz, t_first, fi, fq, n) if self.active() else ('stop',)
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self.dropped += 1
            log.warning("IQ recorder queue full: dropped %s kHz minute (disk too slow?)",
                        band_khz)

    def _open(self, band_khz, part=1):
        if self._session is None:
            self._session = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%SZ')
            log.info("IQ recording started: session %s in %s", self._session, self.dir)
        path = os.path.join(self.dir, '%s_%skHz_%d_%dbit%s.wav'
                            % (self._session, band_khz, self.rate, self.bits,
                               '' if part == 1 else '_p%d' % part))
        w = wave.open(path, 'wb')
        w.setnchannels(2)
        w.setsampwidth(self.bits // 8)
        w.setframerate(self.rate)
        return w

    def _close_all(self):
        for w, _t, _n, _p in self._writers.values():
            w.close()
        if self._writers:
            log.info("IQ recording stopped: session %s (%d band files)",
                     self._session, len(self._writers))
        self._writers, self._session = {}, None

    def _run(self):
        while True:
            item = self._q.get()
            try:
                if item[0] == 'stop':
                    self._close_all()
                    continue
                _k, band_khz, t_first, fi, fq, n = item
                w, expect, nbytes, part = self._writers.get(band_khz, (None, None, 0, 1))
                data = pack_iq(fi[:n], fq[:n], self.bits, self.in_scale)
                if w is None:
                    w = self._open(band_khz)
                else:
                    if expect is not None and abs(t_first - expect) > GAP_TOL_S:
                        log.warning("IQ recording gap on %s kHz: %.3f s", band_khz, t_first - expect)
                    if nbytes + len(data) > self.max_data_bytes:     # WAV 4 GiB limit
                        w.close()
                        part += 1
                        nbytes = 0
                        w = self._open(band_khz, part)
                        log.info("IQ recording %s kHz: continuing in part %d", band_khz, part)
                w.writeframes(data)
                self._writers[band_khz] = (w, t_first + n / self.rate, nbytes + len(data), part)
            except Exception as e:                 # never kill the recorder thread
                log.error("IQ recorder error: %s", e)
            finally:
                self._q.task_done()

    def close(self, wait=True):
        """Close any open files (waits for queued minutes to be written)."""
        self._q.put(('stop',))
        if wait:
            self._q.join()
