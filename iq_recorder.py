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
    touch /home/sparkgap/iqrec/RECORD    # start (C receiver: capture now, files open at the next minute)
    rm    /home/sparkgap/iqrec/RECORD    # stop  (the minute in hand is written, then files close)
An empty RECORD records every band. A RECORD that names bands records only those, so a capture
leaves out the bands that are closed at that hour (WARC bands in a contest, 160/80 m by day):
    echo "40m 20m 15m 10m" > /home/sparkgap/iqrec/RECORD
Bands are named as in the config ("40m", "160m"; "40" works too) or by centre kHz ("7090"). The list
is read once a minute; a band dropped from it while recording writes its minute in hand and closes.
Under MIN_FREE_BYTES (2 GB) free on the disk, the recorder removes RECORD itself and stops.
Each session writes one stereo (I,Q) WAV per band:
    <dir>/<UTC stamp>_<band kHz>kHz_<rate>_<bits>bit.wav
The stamp is the time of the session's first sample (the first minute's
t_first), not the time it was written, so replays can be scored by time.
A WAV header holds 32-bit sizes (4 GiB of audio, about 62 minutes at
192 kHz 24-bit stereo), so a band that reaches MAX_DATA_BYTES continues in
..._p2.wav, ..._p3.wav: contiguous, nothing dropped.
Gaps between consecutive swaps (t_first discontinuities) are logged. The
capture buffer behind a swap holds 65 s and drops what does not fit, so a
minute snapshot the skimmer skips (the previous FT8/RTTY cycle still running)
loses up to 55 s of that band; it shows up here as a logged gap.

Writing happens on a background thread so the decode loop never waits on
the disk. 8 bands x 192 kHz x 24-bit = ~9 MB/s (~33 GB/hour). The queue holds
MAX_QUEUE band-minutes (69 MB each at 24-bit, about one minute of 8 bands); a
disk slower than that drops whole minutes (logged) instead of growing memory.
"""
import datetime
import logging
import os
import queue
import shutil
import threading
import wave

import numpy as np

log = logging.getLogger('sparkgap')

GAP_TOL_S = 0.05
MAX_DATA_BYTES = 4_000_000_000    # under the WAV format's 2**32 - 1 byte limit
MAX_QUEUE = 8                     # band-minutes waiting for the disk (~550 MB at 24-bit)
_BANDS_KHZ = ((1800, 2000, "160m"), (3500, 4000, "80m"), (5250, 5450, "60m"), (7000, 7300, "40m"),
              (10100, 10150, "30m"), (14000, 14350, "20m"), (18068, 18168, "17m"), (21000, 21450, "15m"),
              (24890, 24990, "12m"), (28000, 29700, "10m"), (50000, 54000, "6m"))
BAND_SLACK_KHZ = 120              # a receiver centred just outside a band edge (1886, 10118) still counts


def band_name(band_khz):
    """The amateur band of a receiver centred at band_khz ("40m"), or None."""
    return next((name for lo, hi, name in _BANDS_KHZ
                 if lo - BAND_SLACK_KHZ <= band_khz <= hi + BAND_SLACK_KHZ), None)


def parse_bands(text):
    """The bands a RECORD file names: None (every band) when it names none, else a set of band
    names ("40m") and centre kHz ("7090")."""
    out = set()
    for tok in text.replace(",", " ").split():
        tok = tok.strip().lower()
        if tok.endswith("m") and tok[:-1].isdigit():
            out.add(tok)
        elif tok.isdigit():
            out.add(tok + "m" if int(tok) <= 160 else str(int(tok)))
    return out or None
CLOSE_TIMEOUT_S = 10.0            # close() gives up waiting on a stalled disk after this
MIN_FREE_BYTES = 2 * 2**30        # a recording stops when the disk has less free than this


def low_disk(path):
    """The free bytes at path when under MIN_FREE_BYTES, else None (also when it cannot be read)."""
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return None
    return free if free < MIN_FREE_BYTES else None


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


def drop_cache(path):
    """Let Linux drop a recording's written pages from its file cache; under WSL that cache is
    Windows memory (cdub89/SparkGap#32)."""
    if not hasattr(os, 'posix_fadvise'):
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except OSError:
        pass


class BandRecorder:
    def __init__(self, rec_dir, bits=24, rate=192000, max_queue=MAX_QUEUE, in_scale=8388608.0):
        if bits not in (16, 24):
            raise ValueError('record_iq_bits must be 16 or 24')
        self.dir, self.bits, self.rate, self.in_scale = rec_dir, bits, rate, in_scale
        os.makedirs(rec_dir, exist_ok=True)
        self._q = queue.Queue(maxsize=max_queue)
        self._writers = {}            # band_khz -> (wave, next_expected_t, data bytes, part)
        self._paths = {}              # band_khz -> the file it writes
        self.max_data_bytes = MAX_DATA_BYTES
        self._session = None          # UTC stamp of the open session
        self._bands = set()           # bands recording: each writes its minute in hand when RECORD goes
        self.dropped = 0
        threading.Thread(target=self._run, name='iq-recorder', daemon=True).start()
        log.info("IQ recorder ready: %s (%d-bit); touch %s to record",
                 rec_dir, bits, os.path.join(rec_dir, 'RECORD'))
        if self.active():   # a restart does not quietly resume a recording someone forgot
            log.warning("IQ recording on at start: %s is there; remove it to stop",
                        os.path.join(rec_dir, 'RECORD'))

    def largest_bytes(self):
        """The audio bytes in the largest band file open now (each file stops at the WAV limit), 0 before
        the first minute is written."""
        return max((n for _w, _t, n, _p in list(self._writers.values())), default=0)

    def active(self):
        return os.path.exists(os.path.join(self.dir, 'RECORD'))

    def wants(self, band_khz):
        """RECORD is there and names this band, or names none (every band)."""
        try:
            with open(os.path.join(self.dir, 'RECORD')) as f:
                bands = parse_bands(f.read(4096))
        except OSError:
            return False
        return bands is None or band_name(band_khz) in bands or str(int(band_khz)) in bands

    def submit(self, band_khz, t_first, fi, fq, n):
        """Called once per band per minute swap. fi/fq must not be reused by
        the caller afterwards (the FT8 path allocates them fresh each swap)."""
        data = ('data', band_khz, t_first, fi, fq, n)
        free = low_disk(self.dir) if self.active() else None
        if free is not None:   # RECORD removed: the stop below writes the minute in hand and closes
            try:
                os.remove(os.path.join(self.dir, 'RECORD'))
            except OSError:
                pass
            log.error("IQ recording stopped: %.1f GB free in %s (stops under %d GB)",
                      free / 2**30, self.dir, MIN_FREE_BYTES // 2**30)
        if self.wants(band_khz):
            self._bands.add(band_khz)
            items = [data]
        elif band_khz in self._bands:    # RECORD removed (or the band taken off it): this minute is still
            self._bands.discard(band_khz)                        # written, then the files close
            items = [data, ('close', band_khz)] if self._bands else [data, ('stop',)]
        elif self.active():              # RECORD names other bands: this one is not recorded
            return
        else:
            self._bands.clear()
            items = [('stop',)]
        for item in items:
            try:
                self._q.put_nowait(item)
            except queue.Full:
                self.dropped += 1
                log.warning("IQ recorder queue full: dropped %s kHz minute (disk too slow?)",
                            band_khz)

    def _open(self, band_khz, t_first, part=1):
        if self._session is None:
            start = (datetime.datetime.fromtimestamp(t_first, datetime.timezone.utc) if t_first > 0
                     else datetime.datetime.now(datetime.timezone.utc))
            self._session = start.strftime('%Y%m%d_%H%M%SZ')
            log.info("IQ recording started: session %s in %s", self._session, self.dir)
        path = os.path.join(self.dir, '%s_%skHz_%d_%dbit%s.wav'
                            % (self._session, band_khz, self.rate, self.bits,
                               '' if part == 1 else '_p%d' % part))
        w = wave.open(path, 'wb')
        self._paths[band_khz] = path
        w.setnchannels(2)
        w.setsampwidth(self.bits // 8)
        w.setframerate(self.rate)
        return w

    def _close_all(self):
        for band_khz, (w, _t, _n, _p) in self._writers.items():
            w.close()
            drop_cache(self._paths[band_khz])
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
                if item[0] == 'close':          # one band taken off RECORD's list; the session goes on
                    got = self._writers.pop(item[1], None)
                    if got:
                        got[0].close()
                        drop_cache(self._paths[item[1]])
                        log.info("IQ recording %s kHz: stopped (no longer on RECORD's list)", item[1])
                    continue
                _k, band_khz, t_first, fi, fq, n = item
                w, expect, nbytes, part = self._writers.get(band_khz, (None, None, 0, 1))
                data = pack_iq(fi[:n], fq[:n], self.bits, self.in_scale)
                if w is None:
                    w = self._open(band_khz, t_first)
                else:
                    if expect is not None and abs(t_first - expect) > GAP_TOL_S:
                        log.warning("IQ recording gap on %s kHz: %.3f s", band_khz, t_first - expect)
                    if nbytes + len(data) > self.max_data_bytes:     # WAV 4 GiB limit
                        w.close()
                        part += 1
                        nbytes = 0
                        w = self._open(band_khz, t_first, part)
                        log.info("IQ recording %s kHz: continuing in part %d", band_khz, part)
                w.writeframes(data)
                self._writers[band_khz] = (w, t_first + n / self.rate, nbytes + len(data), part)
                drop_cache(self._paths[band_khz])   # a minute written: out of the page cache
            except Exception as e:                 # never kill the recorder thread
                log.error("IQ recorder error: %s", e)
            finally:
                self._q.task_done()

    def close(self, wait=True, timeout=CLOSE_TIMEOUT_S):
        """Close any open files, waiting up to timeout seconds for queued minutes to
        be written; a stalled disk does not block shutdown longer than that."""
        try:
            self._q.put(('stop',), timeout=timeout)
        except queue.Full:
            log.warning("IQ recorder: queue full at close, files left open")
            return
        if wait:
            with self._q.all_tasks_done:
                if not self._q.all_tasks_done.wait_for(lambda: self._q.unfinished_tasks == 0, timeout):
                    log.warning("IQ recorder: disk still busy after %.0f s at close", timeout)
