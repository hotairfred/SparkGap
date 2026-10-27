"""Live voice (SSB) skimmer: wideband IQ in, runner spots out on a DX-cluster telnet port.

usage: live_voice.py IQ CENTRE_KHZ LO_KHZ HI_KHZ --call CALL-SSID [--follow] [--start S] [--dur S]
                     [--speed X] [--port 7373] [--spots FILE] [--fcc FILE] [--no-lockon]
                     [--transcripts DIR]

IQ is 192 kHz IQ that voice_detect.read_block reads (.cf32, or 16/24-bit WAV). With --follow it is a
file being written (flex_record.py's output) and is read as it grows; without, it is replayed at
--speed times real time (1 = live pace, for the keep-up test; 0 = as fast as it goes).
Needs faster-whisper and, for live use, a CUDA GPU (voice/README.md).

Each 10 s block: voice_detect's 2 s window hits -> tracks (a carrier that keeps a voice). Each
track's audio is kept at 16 kHz; when its speaker pauses (no hit for OVER_GAP_S) the over goes to
Whisper (one decode per window, no retries). The strict rule (voice_spot.runner_hits, cut-off and
FCC checks) runs on the last 10 min of that carrier's transcripts; a call with two runner copies
(three for a 1x1) is spotted. Once spotted, a carrier is only sampled (LOCK_CHECK_S every
LOCK_PERIOD_S) until it goes quiet. Transcripts live in memory for TRANSCRIPT_KEEP_S and are then
dropped; only spots are written (--spots), unless --transcripts DIR asks for them (for measuring).
"""
import argparse
import os
import queue
import socket
import sys
import threading
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import voice_detect as vd   # noqa: E402
import voice_spot as vs     # noqa: E402

FS, AUDIO_FS = vd.FS, vd.AUDIO_FS
BLOCK_S = 4.0               # two of voice_detect's 2 s windows
MIX_PAD_S = 0.25            # track audio: FFT overlap discarded at each block edge (audio runs this late)
DEC = FS // AUDIO_FS
STALL_S = 30.0              # --follow: no growth for this long = the recording has ended
# A track's audio is sent CONTINUOUSLY, gaps included, as offline (Whisper's VAD drops the silence):
# the detector sees a weak voice only now and then, so cutting around its hits threw speech away
# (K4M, 10-21). Pieces go at the first detector pause after SEND_MIN_S, at OVER_MAX_S regardless
# (Whisper's encoder runs 30 s windows, so long pieces pack them), and the rest when the track has
# had no voice for OVER_GAP_S.
SEND_MIN_S = float(os.environ.get("LV_SEND_MIN", 60.0))
OVER_MAX_S = float(os.environ.get("LV_OVER_MAX", 90.0))
PAUSE_S = 2.0
OVER_GAP_S = 60.0
OVER_PAD_S = 1.0
TRACK_END_S = 120.0         # a track with no hit for this long is closed
LOCK_PERIOD_S = float(os.environ.get('LV_LOCK_PERIOD', 300.0))
LOCK_CHECK_S = 30.0
# Overload (Fred 10-21): a piece more than MAX_AGE_S behind live is dropped, and its track is owed a
# fresh look: its next CHECK_S of live audio goes to the front of the queue. Order of service: owed
# checks, then new (unlocked) stations, then the routine checks of locked runners.
MAX_AGE_S = float(os.environ.get('LV_MAX_AGE', 600.0))   # 10-24 replay of a CQ WW hour: 28 calls at 600 s, 24 at
                                                         # 120 s (lag only grows when the GPU is short anyway)
CHECK_S = 30.0
PRI_OWED, PRI_NEW, PRI_LOCKED = 0, 1, 2
TRANSCRIPT_KEEP_S = 900.0   # transcripts kept (in memory) this long, then dropped
RESPOT_S = 600.0
FAIL_STOP = 20          # this many pieces in a row that Whisper can't decode: stop, loudly
CARRIER_OFFSET_HZ = 100.0   # our carrier estimate runs 0-200 Hz inside the dial (10-20 check)
PROMPT = ("CQ contest, QRZ, thank you, five nine, Kilo, November, Whiskey, Alpha, Bravo, Charlie, Delta, "
          "Echo, Foxtrot, Golf, Hotel, India, Juliet, Lima, Mike, Oscar, Papa, Quebec, Romeo, Sierra, Tango, "
          "Uniform, Victor, X-ray, Yankee, Zulu.")


# ---------------------------------------------------------------- source

class Source:
    """Blocks of IQ from a file: replayed at `speed` x real time, or followed while it grows."""

    def __init__(self, path, start_s=0.0, speed=1.0, follow=False):
        self.path, self.t, self.speed, self.follow = path, start_s, speed, follow
        self.wall0 = time.time()

    def available_s(self):
        return vd.total_seconds(self.path)

    def next_block(self, dur):
        """(t, iq) of the next block, waiting until it exists (follow) or is due (replay); None at the end."""
        grew, last_have = time.time(), None
        while True:
            have = self.available_s()
            if have != last_have:
                grew, last_have = time.time(), have
            # follow: a file that stopped growing for STALL_S has ended (the recorder finished
            # or its stream died); without this the runner waited forever for a block that
            # would never come (10-22: a 45 s test, not a multiple of the 4 s block)
            if have >= self.t + dur:
                break
            if not self.follow or time.time() - grew >= STALL_S:
                if have - self.t < 1.0:
                    return None
                dur = have - self.t
                break
            time.sleep(1.0)
        if not self.follow and self.speed > 0:
            due = self.wall0 + (self.t + dur - self.t0) / self.speed
            if due > time.time():
                time.sleep(due - time.time())
        iq = vd.read_block(self.path, self.t, dur)
        t, self.t = self.t, self.t + len(iq) / FS
        return t, iq

    def start_clock(self):
        self.t0, self.wall0 = self.t, time.time()


# ---------------------------------------------------------------- tracks and overs

class Track:
    def __init__(self, sb, carrier, t):
        self.sb, self.carriers, self.hits = sb, [carrier], [t]
        self.confirmed = False
        self.audio = []             # (t0, complex baseband at 16 kHz) chunks since the over began
        self.over_start = None
        self.last_hit = t

    @property
    def carrier(self):
        return float(np.median(self.carriers[-20:]))


class Tracks:
    """voice_detect.build_tracks, incremental."""

    def __init__(self):
        self.live = []

    def add_hits(self, hits):
        for t, c, sb, _score in sorted(hits):
            for tr in self.live:
                if tr.sb == sb and abs(tr.carrier - c) <= vd.JOIN_HZ:
                    tr.carriers.append(c)
                    tr.hits.append(t)
                    tr.last_hit = t
                    if not tr.confirmed and len(tr.hits) >= vd.SPAWN_HITS and tr.hits[-1] - tr.hits[-vd.SPAWN_HITS] <= vd.SPAWN_S:
                        tr.confirmed = True
                    if tr.over_start is None:
                        tr.over_start = t - OVER_PAD_S
                    break
            else:
                tr = Track(sb, c, t)
                tr.over_start = t - OVER_PAD_S
                self.live.append(tr)

    def close_old(self, now):
        self.live = [tr for tr in self.live if now - tr.last_hit <= TRACK_END_S]


def sideband_audio(z, sb):
    """voice_detect.cut_audio's sideband filter and level, for one over."""
    Z = np.fft.fft(z)
    fr = np.fft.fftfreq(len(z), 1 / AUDIO_FS)
    keep = ((fr > 200) & (fr < 3000)) if sb == 'USB' else ((fr > -3000) & (fr < -200))
    Z[~keep] = 0
    a = np.real(np.fft.ifft(Z))
    return np.clip(a / (np.percentile(np.abs(a), 99.5) + 1e-12) * 0.5, -1, 1).astype(np.float32)


# ---------------------------------------------------------------- spots out

class Telnet:
    """A minimal DX-cluster telnet server: asks for a call, then streams 'DX de' lines."""

    def __init__(self, port, call):
        self.call, self.clients, self.lock = call, [], threading.Lock()
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(('', port))
        self.srv.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            c, _ = self.srv.accept()
            try:
                c.sendall(b'Please enter your call: ')
                c.settimeout(30)
                c.recv(64)
                c.settimeout(None)
                c.sendall(('Hello, %s voice skimmer (experimental, SSB runners only)\r\n' % self.call).encode())
                with self.lock:
                    self.clients.append(c)
            except OSError:
                c.close()

    def spot(self, khz, call, comment):
        line = 'DX de %-10s%9.1f  %-12s %-30s %sZ\r\n' % (self.call + ':', khz, call, comment[:30],
                                                         time.strftime('%H%M', time.gmtime()))
        with self.lock:
            for c in list(self.clients):
                try:
                    c.sendall(line.encode())
                except OSError:
                    self.clients.remove(c)
        return line.strip()


# ---------------------------------------------------------------- the runner

class Runner:
    def __init__(self, a):
        self.a = a
        self.centre, self.lo, self.hi = a.centre * 1000, a.lo * 1000, a.hi * 1000
        self.tracks = Tracks()
        self.q = queue.PriorityQueue()     # (priority, -t0 newest first, n, item)
        self.qn = 0
        self.owed = set()                  # carriers (0.5 kHz) whose stale piece was dropped
        self.segs = {}              # carrier (0.5 kHz) -> [(abs t, seg)] transcripts, last TRANSCRIPT_KEEP_S
        self.info = {}              # carrier (0.5 kHz) -> [(abs t, calls heard, dominant mentions, contest words)]
        self.hits = []              # (khz, t, call, ctx) runner copies, last TRANSCRIPT_KEEP_S
        self.spotted = {}           # (base, round kHz) -> t
        self.sent = []              # (base, kHz, t) spotted in the last RESPOT_S: vs.same_spot()
        self.fail_run = 0           # pieces in a row Whisper failed on (FAIL_STOP)
        self.ready = threading.Event()   # set once the transcriber is loaded
        self.locks = {}             # carrier (0.5 kHz) -> t locked
        self.last_check = {}        # carrier (0.5 kHz) -> audio t of its last check
        self.fcc = set(open(a.fcc).read().split()) if a.fcc else None
        self.telnet = Telnet(a.port, a.call) if a.port else None
        self.spot_log = open(a.spots, 'a') if a.spots else None
        self.debug = a.transcripts                       # off by default: transcripts stay in memory
        if self.debug:
            os.makedirs(self.debug, exist_ok=True)       # appended to, never cleared
        self.stats = {'overs': 0, 'overs_skipped': 0, 'audio_s': 0.0, 'whisper_s': 0.0, 'spots': 0, 'dropped': 0, 'owed': 0}
        self.lag = 0.0
        self.prev = []              # the last 3 blocks' spectra (12 s), to give a newly confirmed track its first words
        self.tail = None            # the previous block's last MIX_PAD_S * 2 of IQ (FFT overlap)
        self.audio_now = lambda: 0.0
        self.newest_t = 0.0         # end of the newest block the detector has handled

    # --- detection side (main thread)
    def spectrum(self, t, iq):
        """One FFT per block for every track's audio (10-24: mixing and resampling the whole band once per
        track cost 65 ms per track per block, 5.9 s per 4 s block at 90 contest tracks). The FFT covers the
        block plus the 2 * MIX_PAD_S before it (carried over); each track's slice is read out MIX_PAD_S late,
        so neither edge wraps and consecutive blocks join exactly."""
        pad = int(2 * MIX_PAD_S * FS)
        if self.tail is None:
            self.tail = np.zeros(pad, np.complex64)
        x = np.concatenate([self.tail, iq.astype(np.complex64)])
        n = len(x) // DEC * DEC
        x = x[len(x) - n:]                                   # drop from the front, keep the block's end
        self.tail = x[-pad:]
        n0 = int(round(t * FS)) + len(iq) - n                # absolute index of x[0]
        out0 = (n0 + pad // 2) / FS                          # the slice starts MIX_PAD_S into x
        return np.fft.fft(x), n, n0, out0, len(iq) // DEC

    def mix(self, tr, spec):
        """tr's complex baseband at AUDIO_FS from a block spectrum: its carrier at 0 Hz, as before."""
        Z, n, n0, _out0, m_out = spec
        m = n // DEC
        kc = int(round((tr.carrier - self.centre) * n / FS))
        rel = np.arange(-(m // 2), m - m // 2)
        W = np.zeros(m, np.complex64)
        W[rel % m] = Z[(kc + rel) % n]
        y = np.fft.ifft(W) * (m / n) * np.exp(-2j * np.pi * kc * n0 / n)   # phase from sample 0, not n0
        s0 = int(round(MIX_PAD_S * AUDIO_FS))
        return y[s0:s0 + m_out].astype(np.complex64)

    def block(self, t, iq):
        db, f = vd.spectra(iq, self.centre, self.lo, self.hi)
        self.tracks.add_hits(vd.window_hits(db, f, vd.floor_db(db), self.lo, self.hi, t))
        end = t + len(iq) / FS
        spec = self.spectrum(t, iq)
        for tr in self.tracks.live:
            if tr.over_start is None or not tr.confirmed:
                continue
            if not tr.audio:
                for pspec in self.prev:                               # its first words
                    if pspec[3] + pspec[4] / AUDIO_FS > tr.over_start:
                        tr.audio.append((pspec[3], self.mix(tr, pspec)))
            tr.audio.append((spec[3], self.mix(tr, spec)))
            quiet = end - tr.last_hit
            length = end - tr.over_start
            ck = round(tr.carrier / 500) / 2
            if ck in self.owed and length >= CHECK_S:                 # a fresh look, owed after a drop
                self.owed.discard(ck)
                self.finish_over(tr, end, ended=False, pri=PRI_OWED)
                continue
            if quiet >= OVER_GAP_S:                                   # gone quiet: send what's left
                self.finish_over(tr, min(end, tr.last_hit + 10.0), ended=True)
            elif length >= OVER_MAX_S or (length >= SEND_MIN_S and quiet >= PAUSE_S):
                self.finish_over(tr, end, ended=False)                # the next piece starts here
        self.tracks.close_old(end)
        self.prev = (self.prev + [spec])[-3:]
        self.newest_t = end

    def finish_over(self, tr, t_end, ended=True, pri=None):
        t0 = tr.over_start
        parts = [(ct, z) for ct, z in tr.audio]
        tr.audio, tr.over_start = [], (None if ended else t_end)     # a long over goes on in pieces
        if not parts or t_end - t0 < 1.0:
            return
        z = np.concatenate([p for _ct, p in parts])
        c0 = parts[0][0]
        s0, s1 = int(max(0, t0 - c0) * AUDIO_FS), int(max(0, t_end - c0) * AUDIO_FS)
        z = z[s0:s1]
        if len(z) < AUDIO_FS:
            return
        ck = round(tr.carrier / 500) / 2
        lk = self.locks.get(ck)
        if pri is None:
            pri = PRI_NEW
            if lk is not None and not self.a.no_lockon:
                # a spotted runner: one CHECK_S slice per LOCK_PERIOD_S, the rest skipped
                if t_end - self.last_check.get(ck, -1e9) < LOCK_PERIOD_S:
                    self.stats['overs_skipped'] += 1
                    return
                z = z[-int(CHECK_S * AUDIO_FS):]
                t0 = t_end - len(z) / AUDIO_FS
                pri = PRI_LOCKED
        if lk is not None:
            self.last_check[ck] = t_end
        self.qn += 1
        self.q.put((pri, -t0, self.qn, (tr.carrier, tr.sb, t0, t_end, sideband_audio(z, tr.sb))))

    # --- Whisper side (worker thread)
    def load_whisper(self):
        """(pipe, transcribe kwargs): faster-whisper large-v3-turbo, int8 on the GPU."""
        from faster_whisper import WhisperModel
        model = WhisperModel('large-v3-turbo', device='cuda', compute_type='int8')
        if self.a.batch:                       # a piece's speech segments decoded together on the GPU
            from faster_whisper import BatchedInferencePipeline
            return BatchedInferencePipeline(model), {'batch_size': self.a.batch}
        return model, {}

    def fatal(self, why):
        """Stop the whole process, loudly: a runner that can't transcribe spots nothing."""
        print('FATAL: %s; stopping' % why, flush=True)
        os._exit(3)

    def whisper_loop(self, pipe=None, extra=None):
        if pipe is None:
            pipe, extra = self.load_whisper()
        extra = extra or {}
        self.ready.set()
        while True:
            entry = self.q.get()
            if entry[3] is None:
                return
            carrier, sb, t0, t_end, audio = entry[3]
            # stale = Whisper is this far behind the newest audio the detector has handled (not the
            # wall clock: a detector that runs behind would make every piece look stale, 10-21 at 8x)
            if self.newest_t - t_end > MAX_AGE_S:
                self.stats['dropped'] += 1
                self.stats['owed'] += 1
                self.owed.add(round(carrier / 500) / 2)
                continue
            w0 = time.time()
            # faster-whisper's word alignment can raise on an odd piece (10-24 22:31Z IndexError in
            # find_alignment killed this thread); retry without word times, then skip the piece
            segs = None
            for wts in (True, False):
                try:
                    out, _info = pipe.transcribe(audio, language='en', vad_filter=True, beam_size=5, **extra,
                                                 initial_prompt=PROMPT, condition_on_previous_text=False,
                                                 word_timestamps=wts, temperature=[0.0])
                    segs = [[round(t0 + s.start, 2), round(t0 + s.end, 2), s.text.strip(), round(s.avg_logprob, 3),
                             [[w.word, round(w.probability, 3)] for w in (s.words or [])]] for s in out]
                    break
                except Exception as e:
                    print('whisper failed on %.1f kHz %.0f s (word times %s): %r' % (carrier / 1000, t0, wts, e),
                          flush=True)
                    self.stats['errors'] = self.stats.get('errors', 0) + 1
            if segs is None:
                self.fail_run += 1
                if self.fail_run >= FAIL_STOP:     # every piece failing (10-26: no libcublas) is no run at all
                    self.fatal('whisper failed on %d pieces in a row' % self.fail_run)
                    return
                continue
            if segs:
                self.fail_run = 0               # a silent piece decodes without the GPU: no proof it works
            self.stats['whisper_s'] += time.time() - w0
            self.stats['audio_s'] += len(audio) / AUDIO_FS
            self.stats['overs'] += 1
            self.lag = self.audio_now() - t_end                # s from the over's end to its transcript
            if self.debug:
                with open(os.path.join(self.debug, 'overs.txt'), 'a') as f:
                    f.write('%.1f kHz %.0f-%.0f s: %s\n' % (carrier / 1000, t0, t_end, ' | '.join(x[2] for x in segs)))
                with open(os.path.join(self.debug, 'segs.jsonl'), 'a') as f:
                    import json
                    f.write(json.dumps([carrier / 1000, t0, segs]) + '\n')
            self.rule(carrier / 1000.0, t0, segs)

    def rule(self, khz, t0, segs):
        ck = round(khz * 2) / 2
        now = t0
        keep = self.segs.setdefault(ck, [])
        keep += segs
        keep[:] = [s for s in keep if now - s[0] <= TRANSCRIPT_KEEP_S]
        info = self.info.setdefault(ck, [])
        info += [(sg[0],) + tuple(vs.seg_info(sg[2])) for sg in segs]       # read each segment once
        info[:] = [x for x in info if now - x[0] <= TRANSCRIPT_KEEP_S]
        ctx = [s for s in keep if s[0] >= t0 - 60]          # the CQ-in-the-last-30-s check needs context
        new = [h for h in vs.runner_hits(ctx) if h[0] >= t0 - 0.01]
        if self.debug and new:
            with open(os.path.join(self.debug, 'hits.txt'), 'a') as f:
                f.writelines('%.1f %.0f %s %s\n' % (khz, t, c, w) for t, c, w in new)
        self.hits += [(khz, t, call, why) for t, call, why in new]
        self.hits = [h for h in self.hits if now - h[1] <= TRANSCRIPT_KEEP_S]
        groups = []
        for hk, t, call, _why in sorted(self.hits, key=lambda h: h[1]):
            b = call.split('/')[0]
            for g in groups:
                if g['base'] == b and abs(g['khz'] - hk) <= vs.POOL_KHZ and t - g['last'] <= vs.POOL_S:
                    g['n'] += 1
                    g['last'] = t
                    g['ts'].append(t)
                    break
            else:
                groups.append({'base': b, 'call': call, 'khz': hk, 'first': t, 'last': t, 'n': 1, 'ts': [t]})
        heard_n = {}                                     # carrier -> call -> segments naming it (kept window)
        for cell, xs in self.info.items():
            hn = heard_n.setdefault(cell, {})
            for x in xs:
                for b in x[1]:
                    hn[b] = hn.get(b, 0) + 1
        for g in groups:
            if g['n'] < vs.copies_needed(g['base'], self.fcc):
                continue
            weaker = (vs.outvoted(g, groups, self.fcc, heard_n) if vs.VARIANTS else vs.cut_off(g, groups, self.fcc))
            if weaker or not vs.us_call_ok(g['base'], self.fcc) or vs.LOCKON and vs.caller_on_owned(g, groups):
                continue
            self.spot(g, now)
        if vs.DOMINANT:                                  # the call that owns this carrier (vs.dominant)
            ms = [(x[0], b) for x in info for b in x[2]]
            cts = [x[0] for x in info if x[3]]
            newest = max((x[0] for x in info), default=now) + 1e-3     # the window ends at the newest segment
            d = vs.dominant(ms, cts, newest - vs.DOM_S, self.fcc)
            if d is not None:
                c, n, runner_up = d
                g = {'base': c, 'call': c, 'khz': khz, 'first': newest - vs.DOM_S, 'last': newest, 'n': n}
                if not vs.outvoted(g, groups, self.fcc, heard_n):
                    self.spot(g, now)

    def spot(self, g, now):
        key = (g['base'], round(g['khz']))
        if now - self.spotted.get(key, -1e9) < RESPOT_S or vs.same_spot(self.sent, g['base'], g['khz'], now, RESPOT_S):
            return
        self.spotted[key] = now
        self.sent = [x for x in self.sent if now - x[2] < RESPOT_S] + [(g['base'], g['khz'], now)]
        self.locks.setdefault(round(g['khz'] * 2) / 2, now)
        self.emit(g)

    def emit(self, g):
        side = 1 if g['khz'] >= 10000 else -1                 # USB above 10 MHz
        khz = g['khz'] + side * CARRIER_OFFSET_HZ / 1000
        self.stats['spots'] += 1
        line = (self.telnet.spot(khz, g['call'], 'SSB CQ %d copies' % g['n']) if self.telnet
                else 'DX de %s %9.1f %s' % (self.a.call, khz, g['call']))
        print('SPOT lag %.0fs  %s' % (self.lag, line), flush=True)
        if self.spot_log:
            self.spot_log.write('%s\t%.1f\t%s\t%d\t%.0f\n' % (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                                                            khz, g['call'], g['n'], self.lag))
            self.spot_log.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('iq')
    ap.add_argument('centre', type=float)
    ap.add_argument('lo', type=float)
    ap.add_argument('hi', type=float)
    ap.add_argument('--follow', action='store_true')
    ap.add_argument('--start', type=float, default=0.0)
    ap.add_argument('--dur', type=float, default=None)
    ap.add_argument('--speed', type=float, default=1.0)
    ap.add_argument('--port', type=int, default=7373)
    ap.add_argument('--call', required=True, help='the telnet spotter call, e.g. N0CALL-4 (an SSID of its own)')
    ap.add_argument('--spots', default=None)
    ap.add_argument('--fcc', default=None, help='active US calls (fcc_calls.py); without it US calls are not checked')
    ap.add_argument('--no-lockon', action='store_true')
    ap.add_argument('--batch', type=int, default=0, help='batch a piece\'s speech segments on the GPU (0 = off)')
    ap.add_argument('--transcripts', default=None, help='also keep every transcript here (off: memory only)')
    a = ap.parse_args()
    if not a.fcc:
        print('live_voice: no --fcc list: US calls are not checked against the FCC database', file=sys.stderr)
    r = Runner(a)
    src = Source(a.iq, a.start, a.speed, a.follow)
    w = threading.Thread(target=r.whisper_loop, daemon=True)
    w.start()
    if not r.ready.wait(300):                               # the model loads before the clock starts
        r.fatal('the transcriber did not load in 5 minutes')
    src.start_clock()
    if a.follow:
        r.audio_now = src.available_s                     # lag behind the recording
    elif a.speed > 0:
        r.audio_now = lambda: src.t0 + (time.time() - src.wall0) * a.speed
    else:
        r.audio_now = lambda: src.t                         # as fast as it goes: lag behind the detector
    last_report = time.time()
    while True:
        if a.dur and src.t >= a.start + a.dur:
            break
        b = src.next_block(BLOCK_S)
        if b is None:
            break
        t, iq = b
        r.block(t, iq)
        if time.time() - last_report >= 60:
            s = r.stats
            print('[%6.0f s] tracks %d, overs %d (%d skipped by lock-on), queue %d, lag %.0f s, dropped %d, whisper %.0f s for %.0f s audio, spots %d'
                  % (src.t, len(r.tracks.live), s['overs'], s['overs_skipped'], r.q.qsize(), r.lag, s['dropped'], s['whisper_s'],
                     s['audio_s'], s['spots']), flush=True)
            last_report = time.time()
    for tr in r.tracks.live:
        if tr.over_start is not None and tr.confirmed:
            r.finish_over(tr, tr.last_hit + OVER_PAD_S)
    r.q.put((99, 0, 0, None))
    w.join()
    s = r.stats
    print('DONE: overs %d (%d skipped by lock-on), dropped %d (each owed a fresh look), whisper %.0f s for %.0f s of audio, spots %d'
          % (s['overs'], s['overs_skipped'], s['dropped'], s['whisper_s'], s['audio_s'], s['spots']), flush=True)


if __name__ == '__main__':
    main()
