// SparkGap Web: the browser client. It connects to a machine's server (sparkgap_server.py), or to the copy that served it.
"use strict";

const PROTOCOL = "sparkgap.v1";
const CODE_PROTOCOL = "sparkgap.code.";  // the join code, offered as a subprotocol (a browser cannot set headers)
const FEED_VERSION = 1;
const MAX_BINS = 16_384;
const DEFAULT_PORT = 7320;
const HELP_URL = "https://github.com/hotairfred/SparkGap/blob/main/docs/WEBUI.md";  // the guide, from Settings
const SCAN_TIMEOUT_S = 3;
const SERVER_PORT = 7320;  // a machine's server: it runs that machine's copies and passes their feeds through
const SERVER_PROTOCOL = "sparkgap.server.v1";
const SERVER_LOG_KEPT = 200;  // server log lines kept for its console
const VALIDATION = ["permissive", "normal", "aggressive"];  // "off" stays a config setting, not offered here
const CALL_SOURCES = [["scp", "SCP"], ["rbn", "RBN"], ["rbn+skcc", "RBN + SKCC"]];
const RULE_FLAGS = [
  ["spot_rule_split_runs", "Split runs", "Words sent with no gap are split before the rule reads them: CQCQDE becomes CQ CQ DE (hand-sent CW)."],
  ["spot_rule_contest_words", "Contest words", "CWT, SST or MST right before a call opens a CQ group as CQ does: runners who send CWT K5AX without the CQ."],
  ["spot_rule_cq_repeats", "CQ repeats", "Back-to-back CQs from one call count as separate copies, so a runner is spotted sooner."],
];
const HINTS = {
  validation: "A confidence score decides which calls are spotted: permissive spots more, aggressive fewer (docs/ITILA2.md).",
  call_source: "Calls known from this list score higher in validation: SCP, calls RBN skimmers heard in the last days, or those plus the SKCC roster.",
};
const CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
const CODE_LENGTH = 6;
const SPECTRUM_HZ = 60;
const HISTORY_COLS = 2_400;
const PLAYOUT_MARGIN_S = 0.20;
const PENDING_MAX = 120;  // rows waiting to be drawn; a hidden tab draws none, so older ones go
const SPOT_KEEP_S = 600;
const SIGNAL_KEEP_S = 180;
const RECONNECT_S = [1, 2, 5, 10, 20];
const LOCKOUT_S = 600;
const DB_FLOOR = 4.0;
const DB_TOP = 32.0;
const TRACE_MIN_PX = 5;
const PEAK_BINS = 2;  // zoomed in: a signal is drawn this many bins tall at its centre (a carrier's width),
const PEAK_MIN_PX = 7;  // a trace zoomed out: TRACE_MIN_PX plus a carrier's spread
const PEAK_MAX_PX = 12;  // kept from PEAK_MIN_PX to this: it neither shrinks nor balloons on zooming in
const MIN_SPAN = 0.05;  // the deepest zoom: this fraction of the band
const PEAK_FROM_PX = 1;  // from this many pixels a bin, peaks are drawn instead of stretched bins
const FREQ_MATCH_KHZ = 0.15;
const SEARCH_MAX = 8;
const NO_FIT = 999.0;
const DECODED = ["both", "wide", "narrow", "lowest"];  // which readings of each window the panel shows
const ZOOM_STEPS = [["Full", 1], ["Half", 2], ["Quarter", 4]];  // the zoom button: name and fraction of the band
const ZOOM_MATCH = 0.05;  // a view within 5% of a step's span is at that step
const DETAIL_MATCH_KHZ = 0.06;
const CALL_RE = /^(?:[A-Z]{1,2}|[A-Z]\d|\d[A-Z])\d[A-Z]{1,4}$/;
const HEARD_S = 90.0;
const GUESS_WINDOWS = 4;
const PEAK_LOOK_S = 10.0;
const PEAK_MIN_DB = 8.0;
const TRACE_DUTY = 0.15;
// close codes the feed gives a browser instead of an HTTP refusal
const CLOSE_WRONG_CODE = 4401, CLOSE_LOCKED_OUT = 4429, CLOSE_FULL = 4503, CLOSE_POLICY = 1008;
const STORE = "sparkgap.web.";

const $ = id => document.getElementById(id);
const now = () => Date.now() / 1000;
const esc = s => s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const baseCall = word => word.split("/").reduce((a, b) => (b.length > a.length ? b : a), "");

function load(key, fallback) {
  try { const v = localStorage.getItem(STORE + key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(STORE + key, JSON.stringify(value)); } catch { /* private window: kept for this visit only */ }
}

// ---- one SparkGap feed: connection, decoded state and the waterfall history ----

class Instance {
  constructor(url, token) {
    this.url = url;
    this.token = token;
    this.state = "connecting";
    this.error = "";
    this.hello = {};
    this.status = {};
    this.windows = [];
    this.spots = new Map();    // call -> latest spot
    this.signals = new Map();  // khz -> latest window
    this.recent = new Map();   // khz -> last texts
    this.heardList = [];
    this.selKhz = null;
    this.selCall = "";
    this.since = 0;  // when this console last connected (status page)
    this.reconnects = 0;
    this.goKhz = null;  // a search match on another band: centred once that band's rows arrive
    this.band = 0;  // which of hello.bands the waterfall shows; a SparkGap may run several
    this.firstHz = 0; this.hzPerBin = 1; this.nBins = 0; this.dbUnit = 0.25;
    this.ring = null;  // HISTORY_COLS columns of nBins levels, column-major
    this.head = 0; this.cols = 0; this.total = 0;
    this.pending = [];  // [play time, row]
    this.clockOffset = null;
    this.rowPeriod = 1 / SPECTRUM_HZ;
    this.lastPlay = 0;
    this.frac = 0;
    this.retry = 0;
    this.closed = false;
    this.refused = "";
    this.ws = null;
    this.source = null;
    this.on = () => {};  // (what) for the page: "state", "hello", "status", "ladder", "detail"
    this.expireTimer = setInterval(() => this.expire(), 1000);
  }

  open() {
    this.refused = "";
    clearTimeout(this.retryTimer);
    const protocols = [PROTOCOL];
    if (this.token) protocols.push(CODE_PROTOCOL + this.token);
    this.setState("connecting");
    let ws;
    try {
      ws = new WebSocket(this.url, protocols);
    } catch (e) {
      this.error = String(e.message || e);
      this.dropped();
      return;
    }
    this.ws = ws;
    ws.binaryType = "arraybuffer";
    let opened = false;
    ws.onopen = () => {
      opened = true;
      if (this.since) this.reconnects++;
      this.since = now();
      this.retry = 0;
      this.error = "";
      ws.send(JSON.stringify({ type: "subscribe", spectrum_hz: SPECTRUM_HZ }));
      this.setState("live");
    };
    ws.onmessage = e => (typeof e.data === "string" ? this.onText(e.data) : this.onBinary(e.data));
    ws.onclose = e => {
      if (this.ws !== ws || this.closed) return;
      if (e.code === CLOSE_WRONG_CODE) {
        // a retry with the same code would only count towards a lockout
        this.refused = "code"; this.error = "wrong join code";
        this.setState("refused");
      } else if (e.code === CLOSE_LOCKED_OUT) {
        this.refused = "lockout"; this.error = "locked out after wrong join codes; retrying in 10 min";
        this.retryTimer = setTimeout(() => this.open(), LOCKOUT_S * 1000);
        this.setState("refused");
      } else {
        this.error = e.code === CLOSE_FULL ? "too many clients"
          : e.code === CLOSE_POLICY ? "dropped by the server (too slow)"
          : opened ? "" : "no answer";
        this.dropped();
      }
    };
  }

  dropped() {
    const delay = RECONNECT_S[Math.min(this.retry, RECONNECT_S.length - 1)];
    this.retry++;
    this.setState("dropped");
    this.retryTimer = setTimeout(() => this.open(), delay * 1000);
  }

  close() {
    this.closed = true;
    clearTimeout(this.retryTimer);
    clearInterval(this.expireTimer);
    if (this.source) this.source.stop();
    if (this.ws) this.ws.close();
  }

  setState(s) { this.state = s; this.on("state"); }

  // ---- messages ----

  onText(text) {
    let m;
    try { m = JSON.parse(text); } catch { return; }
    this.message(m);
  }

  message(m) {
    if (m.type === "hello") {
      this.hello = m;
      if (!this.bands[this.band]) this.band = 0;
      this.windows = []; this.spots.clear(); this.signals.clear(); this.recent.clear();
      this.on("hello");
    } else if (m.type === "snapshot") {
      for (const w of m.windows || []) this.addWindow(w);
      for (const s of m.spots || []) this.spots.set(s.call, s);
      if (m.status) { this.status = m.status; this.on("status"); }
      this.on("ladder");
    } else if (m.type === "window") {
      this.addWindow(m);
      if (this.matchesSelection(m.khz)) this.on("detail");
    } else if (m.type === "spot") {
      const first = !this.spots.has(m.call);
      this.spots.set(m.call, m);
      if (first) this.on("ladder");
      if (this.matchesSelection(m.khz)) this.on("detail");
    } else if (m.type === "status") {
      this.status = m;
      this.on("status");
    }
  }

  addWindow(w) {
    // SparkGap decodes each window twice, through a narrow (~100 Hz) and a wide (~200 Hz) channel
    // filter, narrow first. The wide reading goes on top (it held the confirmed call more often),
    // unless it has no fit (cost 999) and the narrow one has.
    for (const prev of this.windows.slice(-8).reverse()) {
      if (Math.abs(prev.khz - w.khz) < 0.02 && Math.abs(prev.utc - w.utc) < 1.0 && !prev.alt) {
        let alt = { text: prev.text, cost: prev.cost, wpm: prev.wpm };
        Object.assign(prev, w);
        prev.rate = 200; alt.rate = 100;
        if ((prev.cost ?? 0) >= NO_FIT && (alt.cost ?? NO_FIT) < NO_FIT) {
          const top = { text: alt.text, cost: alt.cost, wpm: alt.wpm, rate: alt.rate };
          alt = { text: prev.text, cost: prev.cost, wpm: prev.wpm, rate: prev.rate };
          Object.assign(prev, top);
        }
        prev.alt = alt;
        this.remember(w.khz, w.text);
        return;
      }
    }
    this.windows.push(w);
    if (this.windows.length > 3000) this.windows.splice(0, this.windows.length - 3000);
    this.signals.set(w.khz, w);
    this.remember(w.khz, w.text);
  }

  remember(khz, text) {
    const r = this.recent.get(khz) || [];
    r.push(text);
    if (r.length > GUESS_WINDOWS) r.shift();
    this.recent.set(khz, r);
  }

  onBinary(buf) {
    if (buf.byteLength < 30) return;
    const d = new DataView(buf);
    const v = d.getUint32(0, true), utc = d.getFloat64(4, true), first = d.getFloat64(12, true);
    const hz = d.getFloat32(20, true), unit = d.getFloat32(24, true), n = d.getUint16(28, true);
    if (v !== FEED_VERSION || !(n > 0 && n <= MAX_BINS) || buf.byteLength !== 30 + n) return;
    if (![utc, first, hz, unit].every(Number.isFinite) || hz <= 0 || unit <= 0) return;
    this.row(utc, first, hz, unit, new Uint8Array(buf.slice(30)));
  }

  row(utc, first, hz, unit, row) {
    const b = this.bands[this.band];
    if (this.bands.length > 1 && b && Math.abs(first - (b.center_khz * 1000 - b.span_khz * 500)) > hz) return;  // another band's row
    if (row.length !== this.nBins || first !== this.firstHz || hz !== this.hzPerBin || unit !== this.dbUnit) {
      this.firstHz = first; this.hzPerBin = hz; this.nBins = row.length; this.dbUnit = unit;
      this.ring = new Uint8Array(this.nBins * HISTORY_COLS);
      this.head = this.cols = this.total = 0;
      this.pending = [];
      this.clockOffset = null;
      this.on("hello");
    }
    const off = now() - utc;
    if (this.clockOffset === null || off < this.clockOffset) this.clockOffset = off;  // server clock and network delay
    this.pending.push([utc + this.clockOffset + PLAYOUT_MARGIN_S, row]);
    if (this.pending.length > PENDING_MAX) this.pending.splice(0, this.pending.length - PENDING_MAX);
  }

  // move due rows into the ring; true when the picture moved
  advance() {
    const t = now();
    let moved = false;
    while (this.pending.length && this.pending[0][0] <= t && this.ring) {
      const [play, row] = this.pending.shift();
      if (this.lastPlay) {
        const gap = play - this.lastPlay;
        if (gap > 0 && gap < 1) this.rowPeriod += (gap - this.rowPeriod) * 0.05;
      }
      this.lastPlay = play;
      this.ring.set(row, this.head * this.nBins);
      this.head = (this.head + 1) % HISTORY_COLS;
      this.cols = Math.min(this.cols + 1, HISTORY_COLS);
      this.total++;
      moved = true;
    }
    if (this.pending.length > 60) this.pending.splice(0, this.pending.length - 10);  // far behind: skip ahead
    const frac = this.lastPlay ? Math.max(0, Math.min(1, (t - this.lastPlay) / this.rowPeriod)) : 0;
    if (frac !== this.frac) { this.frac = frac; moved = true; }
    return moved;
  }

  // ---- derived state ----

  expire() {
    this.refreshHeard();
    const t = now();
    let changed = false;
    for (const [c, s] of this.spots) if (t - s.utc > SPOT_KEEP_S) { this.spots.delete(c); changed = true; }
    for (const [k, s] of this.signals) {
      if (t - s.utc > SIGNAL_KEEP_S) { this.signals.delete(k); this.recent.delete(k); changed = true; }
    }
    if (changed) this.on("ladder");
  }

  matchesSelection(khz) { return this.selKhz !== null && Math.abs(khz - this.selKhz) <= FREQ_MATCH_KHZ; }

  // host and band from hello; empty until the first hello. No address: that is for Settings.
  get bands() { return this.hello.bands || []; }
  bandAt(khz) { return this.bands.findIndex(b => Math.abs(khz - b.center_khz) <= b.span_khz / 2); }
  setBand(n) {
    if (n === this.band || !this.bands[n]) return;
    this.band = n;
    this.nBins = 0;  // the next row of the new band starts a fresh history
    this.ring = null; this.pending = []; this.head = this.cols = this.total = 0;
    this.selKhz = null; this.selCall = "";
    this.on("hello");
  }

  // a config named <instance>.local.json names the server after the instance, else the host
  get baseName() {
    const cfg = String((this.hello.run || {}).config || "").split("/").pop().match(/^(.+)\.local\.json$/);
    return cfg ? cfg[1] : this.hello.host;
  }

  // two copies with one name are told apart by port
  get name() {
    const base = this.baseName;
    const twin = base && app.instances.some(i => i !== this && i.baseName === base);
    return twin ? `${base} :${urlParts(this.url)[1]}` : base;
  }

  get station() {
    const b = this.bands[this.band] || this.bands[0];
    const band = b ? bandLabel(b) : "";
    return [this.name, band].filter(x => x).join(" · ");
  }

  get title() { return this.station || this.url.split("//").pop().split("/")[0]; }

  get lowKhz() { return this.nBins ? this.firstHz / 1000 : 0; }
  get highKhz() { return this.nBins ? (this.firstHz + this.nBins * this.hzPerBin) / 1000 : 0; }

  // calls (spots) and call-less active signals inside the view, highest frequency first
  ladder(lo, hi) {
    const items = [], taken = [];
    for (const [call, s] of this.spots) {
      if (s.khz >= lo && s.khz <= hi) { items.push({ call, khz: s.khz, wpm: s.wpm, spot: true }); taken.push(s.khz); }
    }
    const t = now();
    for (const [khz, w] of this.signals) {
      if (khz < lo || khz > hi || t - w.utc > HEARD_S) continue;
      if (taken.some(k => Math.abs(khz - k) <= FREQ_MATCH_KHZ)) continue;
      const guess = this.guessCall(khz);
      if (guess) { items.push({ call: guess, khz, wpm: w.wpm, spot: false }); taken.push(khz); }
    }
    const sel = this.selKhz;
    if (sel !== null && sel >= lo && sel <= hi && taken.every(k => Math.abs(sel - k) > FREQ_MATCH_KHZ)) {
      items.push({ call: "", khz: sel, wpm: (this.signals.get(sel) || {}).wpm, spot: false });
    }
    return items.sort((a, b) => b.khz - a.khz);
  }

  // the call-shaped word seen most often (at least twice) in khz's recent windows
  guessCall(khz) {
    const n = new Map();
    for (const [k, texts] of this.recent) {
      if (Math.abs(k - khz) > FREQ_MATCH_KHZ) continue;
      for (const text of texts) {
        for (const word of text.split(/\s+/)) {
          const base = baseCall(word);
          if (CALL_RE.test(base)) n.set(base, (n.get(base) || 0) + 1);
        }
      }
    }
    let best = "", most = 1;
    for (const [c, k] of n) if (k > most) { best = c; most = k; }
    return best;
  }

  refreshHeard() {
    const t = now(), spots = [...this.spots.values()].map(s => s.khz), out = [];
    for (const [k, w] of this.signals) {
      if (t - w.utc > HEARD_S) continue;
      const spotted = spots.some(s => Math.abs(k - s) <= FREQ_MATCH_KHZ);
      if (spotted || this.keyed(k) || this.guessCall(k)) out.push({ khz: k, spot: spotted });
    }
    this.heardList = out;
  }

  // matches for the header search: a frequency (kHz or MHz), or calls containing the text
  search(query) {
    const q = query.trim().toUpperCase();
    if (!q) return [];
    const v = Number(q);
    if (Number.isFinite(v) && /^[\d.]+$/.test(q)) {
      const khz = v < 100 ? v * 1000 : v;
      return [{ call: "", khz, ago: "", spot: false, label: `${khz.toFixed(2)} kHz` }];
    }
    const found = new Map();
    for (const [call, s] of this.spots) if (call.includes(q)) found.set(call, { call, khz: s.khz, utc: s.utc, spot: true });
    for (let i = this.windows.length - 1; i >= 0; i--) {
      const w = this.windows[i];
      for (const word of w.text.split(/\s+/)) {
        const base = baseCall(word);
        if (base.includes(q) && CALL_RE.test(base) && !found.has(base)) found.set(base, { call: base, khz: w.khz, utc: w.utc, spot: false });
      }
    }
    const t = now();
    const out = [...found.values()].sort((a, b) =>
      (a.spot === b.spot ? 0 : a.spot ? -1 : 1) || ((a.call !== q) - (b.call !== q)) || (b.utc - a.utc));
    return out.slice(0, SEARCH_MAX).map(m => {
      const ago = Math.floor(t - m.utc);
      return { call: m.call, khz: m.khz, spot: m.spot, label: m.call, ago: ago < 120 ? `${ago}s` : `${Math.floor(ago / 60)}m` };
    });
  }

  select(khz, call) { this.selKhz = khz; this.selCall = call; this.on("detail"); }

  // a decoded signal near khz if there is one, else the strongest trace near it, else -1
  nearestSignal(khz, tol) {
    const cands = [...[...this.spots.values()].map(s => s.khz), ...this.signals.keys()];
    if (cands.length) {
      const best = cands.reduce((a, b) => (Math.abs(b - khz) < Math.abs(a - khz) ? b : a));
      if (Math.abs(best - khz) <= Math.max(tol, FREQ_MATCH_KHZ)) return best;
    }
    const peak = this.peak(khz, tol);
    return peak ? peak[0] : -1;
  }

  // columns of the last PEAK_LOOK_S, oldest first
  recentCols() {
    const n = Math.min(this.cols, Math.floor(PEAK_LOOK_S / this.rowPeriod)), out = [];
    for (let i = 0; i < n; i++) out.push(((this.head - n + i) % HISTORY_COLS + HISTORY_COLS) % HISTORY_COLS);
    return out;
  }

  // a trace at khz above PEAK_MIN_DB for at least TRACE_DUTY of the last PEAK_LOOK_S
  keyed(khz) {
    if (!this.ring || !this.cols) return false;
    const b = Math.floor((khz * 1000 - this.firstHz) / this.hzPerBin);
    const b0 = Math.max(0, b - 1), b1 = Math.min(this.nBins, b + 2);
    if (b1 <= b0) return false;
    const cols = this.recentCols(), need = PEAK_MIN_DB / this.dbUnit;
    let on = 0;
    for (const c of cols) {
      const o = c * this.nBins;
      for (let i = b0; i < b1; i++) if (this.ring[o + i] >= need) { on++; break; }
    }
    return cols.length > 0 && on / cols.length >= TRACE_DUTY;
  }

  // [khz, dB] of the strongest bin within tol of khz over the last PEAK_LOOK_S
  peak(khz, tol) {
    if (!this.ring || !this.cols) return null;
    const b0 = Math.max(0, Math.floor(((khz - tol) * 1000 - this.firstHz) / this.hzPerBin));
    const b1 = Math.min(this.nBins, Math.ceil(((khz + tol) * 1000 - this.firstHz) / this.hzPerBin));
    if (b1 <= b0) return null;
    const cols = this.recentCols();
    let best = -1, level = 0;
    for (let i = b0; i < b1; i++) {
      let m = 0;
      for (const c of cols) m = Math.max(m, this.ring[c * this.nBins + i]);
      if (m > level) { level = m; best = i; }
    }
    const db = level * this.dbUnit;
    if (best < 0 || db < PEAK_MIN_DB) return null;
    return [(this.firstHz + (best + 0.5) * this.hzPerBin) / 1000, db];
  }

  marked(text, call) {
    return esc(text).split(" ").map(w => (call && w === call ? `<b>${w}</b>` : w)).join(" ");
  }

  get detail() {
    if (this.selKhz === null) return null;
    const k = this.selKhz;
    let call = this.selCall;
    if (!call) for (const [c, s] of this.spots) if (Math.abs(s.khz - k) <= FREQ_MATCH_KHZ) { call = c; break; }
    call = call || this.guessCall(k);
    const wins = this.windows.filter(w => Math.abs(w.khz - k) <= DETAIL_MATCH_KHZ).slice(-40);
    const last = wins[wins.length - 1] || {};
    const rows = wins.reverse().map(w => ({
      utc: new Date(w.utc * 1000).toISOString().substr(11, 8),
      html: this.marked(w.text, call), cost: w.cost, rate: w.rate,
      altHtml: w.alt ? this.marked(w.alt.text, call) : "", altCost: w.alt ? w.alt.cost : null, altRate: w.alt ? w.alt.rate : null,
    }));
    // speed and level of the call shown: the spot's own when it was spotted, else the newest window
    // that copied the call (the newest at this frequency may be noise after the station stopped)
    const spot = call ? this.spots.get(call) : null;
    const src = spot || (call && wins.find(w => w.text.split(" ").includes(call))) || last;
    let snr = src.snr ?? null;
    if (snr === null) { const p = this.peak(k, FREQ_MATCH_KHZ); snr = p ? p[1] : null; }
    const note = rows.length ? "" : "No decoded text at this frequency yet. SparkGap skips the FT8 and digital segments, "
      + "and starts a decoder only on a steady signal above its SNR threshold. Text appears here if it does.";
    return { khz: k, spotKhz: spot ? spot.khz : null, call, spotted: !!spot, wpm: src.wpm ?? null, snr, windows: rows, note };
  }
}

class Waterfall {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: false });
    this.inst = null;
    this.lo = 0; this.hi = 0;
    this.sel = -1;
    this.lut = new Uint32Array(256);
    this.accent = "#f3a640"; this.surface = "#11171d";
    this.buf = document.createElement("canvas");  // coloured columns, a ring
    this.bctx = this.buf.getContext("2d", { alpha: false });
    this.cpos = 0; this.seen = 0; this.dirty = true;
    this.map = null;
    this.onview = () => {};
  }

  setInstance(inst) {
    if (inst === this.inst) return;
    this.inst = inst;
    this.lo = this.hi = 0;
    this.fit();
  }

  fit() {
    const inst = this.inst;
    if (inst && inst.nBins && inst.goKhz) {
      const span = this.hi > this.lo ? this.hi - this.lo : 30;
      this.setView(inst.goKhz - span / 2, inst.goKhz + span / 2);
      inst.goKhz = null;
    } else if (inst && inst.nBins && !(inst.lowKhz <= this.lo && this.lo < this.hi && this.hi <= inst.highKhz)) {
      const mid = (inst.lowKhz + inst.highKhz) / 2;
      this.setView(mid - 15, mid + 15);
    }
    this.dirty = true;
  }

  setView(lo, hi) {
    const inst = this.inst;
    if (inst && inst.nBins) {
      const full = inst.highKhz - inst.lowKhz, span = Math.min(Math.max(hi - lo, full * MIN_SPAN), full);
      lo = Math.min(Math.max(lo, inst.lowKhz), inst.highKhz - span);
      hi = lo + span;
    }
    if (lo === this.lo && hi === this.hi) return;
    this.lo = lo; this.hi = hi;
    this.dirty = true;
    this.onview();
  }

  pan(d) { this.setView(this.lo + d, this.hi + d); }
  khzAt(y) { return this.hi - y / this.canvas.clientHeight * (this.hi - this.lo); }

  setColors() {
    const cs = getComputedStyle(document.documentElement);
    const stops = [0, 1, 2, 3, 4, 5, 6].map(i => {
      const h = cs.getPropertyValue(`--wf-${i}`).trim().replace("#", "");
      return [0, 2, 4].map(j => parseInt(h.slice(j, j + 2), 16));
    });
    for (let v = 0; v < 256; v++) {
      const p = v / 255 * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(p)), f = p - i;
      const c = [0, 1, 2].map(k => Math.round(stops[i][k] + (stops[i + 1][k] - stops[i][k]) * f));
      this.lut[v] = (255 << 24 | c[2] << 16 | c[1] << 8 | c[0]) >>> 0;  // ImageData is RGBA in memory
    }
    this.accent = cs.getPropertyValue("--wf-mark").trim();
    this.surface = cs.getPropertyValue("--wf-0").trim();
    this.dirty = true;
  }

  // pixel row -> bins: interpolate when a bin spans pixels, max over bins otherwise
  buildMap(inst, h) {
    const binsInView = (this.hi - this.lo) * 1000 / inst.hzPerBin, pxPerBin = h / Math.max(binsInView, 1e-9);
    const grow = Math.max(0, Math.floor(Math.ceil(TRACE_MIN_PX / pxPerBin) / 2));  // widen narrow traces
    const a = new Int32Array(h), b = new Int32Array(h), frac = new Float32Array(h);
    for (let y = 0; y < h; y++) {
      const fTop = (this.hi - y / h * (this.hi - this.lo)) * 1000, fBot = (this.hi - (y + 1) / h * (this.hi - this.lo)) * 1000;
      if (pxPerBin >= 1) {
        const pos = (fTop - (fTop - fBot) / 2 - inst.firstHz) / inst.hzPerBin - 0.5;
        const i0 = Math.floor(pos);
        a[y] = Math.min(Math.max(i0, 0), inst.nBins - 1);
        b[y] = Math.min(Math.max(i0 + 1, 0), inst.nBins - 1);
        frac[y] = Math.min(Math.max(pos - i0, 0), 1);
      } else {
        a[y] = Math.min(Math.max(Math.floor((fBot - inst.firstHz) / inst.hzPerBin), 0), inst.nBins - 1);
        b[y] = Math.min(Math.max(Math.ceil((fTop - inst.firstHz) / inst.hzPerBin), a[y] + 1), inst.nBins);
      }
    }
    const peak = pxPerBin >= PEAK_FROM_PX;  // bin index of row y's centre: top - (y + 0.5) / pxPerBin
    const top = (this.hi * 1000 - inst.firstHz) / inst.hzPerBin - 0.5;
    const peakPx = Math.min(PEAK_MAX_PX, Math.max(PEAK_MIN_PX, Math.round(PEAK_BINS * pxPerBin)));
    this.map = { interp: pxPerBin >= 1, a, b, frac, grow: peak ? 0 : grow, peak, top, pxPerBin, peakPx };
    this.v = new Float32Array(inst.nBins);
    this.wide = new Float32Array(inst.nBins);
    this.pxCol = new Float32Array(h);
  }

  // ring column -> pixels (Uint32 RGBA) for one canvas column
  colour(inst, col, out, stride, x) {
    const n = inst.nBins, ring = inst.ring, o = col * n, m = this.map;
    let v = this.v;
    for (let i = 0; i < n; i++) v[i] = ring[o + i] * inst.dbUnit;
    if (m.grow) {
      const w = this.wide;
      for (let i = 0; i < n; i++) {
        let x2 = v[i];
        for (let k = 1; k <= m.grow; k++) {
          if (i >= k && v[i - k] > x2) x2 = v[i - k];
          if (i + k < n && v[i + k] > x2) x2 = v[i + k];
        }
        w[i] = x2;
      }
      v = w;
    }
    const scale = 255 / (DB_TOP - DB_FLOOR), h = m.a.length;
    if (m.peak) {  // as the console: the background without its signals, then each peak peakPx tall at its centre
      const low = this.wide, col = this.pxCol;
      for (let i = 0; i < n; i++) {
        let x2 = v[i];
        for (let k = 1; k <= 2; k++) {
          if (i >= k && v[i - k] < x2) x2 = v[i - k];
          if (i + k < n && v[i + k] < x2) x2 = v[i + k];
        }
        low[i] = x2;
      }
      for (let y = 0; y < h; y++) col[y] = low[m.a[y]] * (1 - m.frac[y]) + low[m.b[y]] * m.frac[y];
      for (let i = 1; i < n - 1; i++) {
        const a = v[i - 1], b = v[i], c = v[i + 1];
        if (!(b >= a && b > c && b > DB_FLOOR)) continue;
        const den = a - 2 * b + c, d = den < 0 ? Math.min(0.5, Math.max(-0.5, 0.5 * (a - c) / den)) : 0;
        const yc = Math.round((m.top - (i + d)) * m.pxPerBin - 0.5);
        for (let y = yc - (m.peakPx >> 1); y < yc - (m.peakPx >> 1) + m.peakPx; y++) if (y >= 0 && y < h && b > col[y]) col[y] = b;
      }
      for (let y = 0; y < h; y++) out[y * stride + x] = this.lut[Math.min(255, Math.max(0, (col[y] - DB_FLOOR) * scale)) | 0];
      return;
    }
    for (let y = 0; y < h; y++) {
      let px;
      if (m.interp) px = v[m.a[y]] * (1 - m.frac[y]) + v[m.b[y]] * m.frac[y];
      else { px = 0; for (let i = m.a[y]; i < m.b[y]; i++) if (v[i] > px) px = v[i]; }
      const idx = Math.min(255, Math.max(0, (px - DB_FLOOR) * scale)) | 0;
      out[y * stride + x] = this.lut[idx];
    }
  }

  rebuild(inst, w, h) {
    const n = w + 3;
    this.buf.width = n; this.buf.height = h;
    const img = this.bctx.createImageData(n, h), px = new Uint32Array(img.data.buffer);
    px.fill(this.lut[0]);
    this.cpos = 0;
    this.seen = inst.total;
    this.dirty = false;
    this.col = this.bctx.createImageData(1, h);
    this.colPx = new Uint32Array(this.col.data.buffer);
    if (inst.ring) {
      this.buildMap(inst, h);
      const k = Math.min(inst.cols, n);
      for (let c = 0; c < k; c++) this.colour(inst, ((inst.head - k + c) % HISTORY_COLS + HISTORY_COLS) % HISTORY_COLS, px, n, n - k + c);
    }
    this.bctx.putImageData(img, 0, 0);
  }

  frame(bubbles) {
    const inst = this.inst, c = this.canvas;
    const w = c.clientWidth, h = c.clientHeight;
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; this.dirty = true; }
    const ctx = this.ctx;
    if (!inst) { ctx.fillStyle = this.css(this.lut[0]); ctx.fillRect(0, 0, w, h); return; }
    inst.advance();
    if (w < 4 || h < 4 || !inst.ring || this.hi <= this.lo) { ctx.fillStyle = this.css(this.lut[0]); ctx.fillRect(0, 0, w, h); return; }
    if (this.dirty || this.buf.height !== h || this.buf.width !== w + 3) this.rebuild(inst, w, h);
    else if (inst.total > this.seen) {
      const n = this.buf.width, k = Math.min(inst.total - this.seen, n);
      for (let j = 0; j < k; j++) {
        this.colour(inst, ((inst.head - k + j) % HISTORY_COLS + HISTORY_COLS) % HISTORY_COLS, this.colPx, 1, 0);
        this.bctx.putImageData(this.col, this.cpos, 0);
        this.cpos = (this.cpos + 1) % n;
      }
      this.seen = inst.total;
    }
    // oldest column at cpos; the newest column's right edge slides from w + 1 to w
    const n = this.buf.width, x0 = w + (1 - inst.frac) - n, a = n - this.cpos;
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(this.buf, this.cpos, 0, a, h, x0, 0, a, h);
    if (this.cpos) ctx.drawImage(this.buf, 0, 0, this.cpos, h, x0 + a, 0, this.cpos, h);
    if (this.sel > 0) {
      const y = Math.round((this.hi - this.sel) / (this.hi - this.lo) * h) + 0.5;
      ctx.strokeStyle = this.accent; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); ctx.setLineDash([]);
    }
    // bubbles at the leading edge: every signal SparkGap is decoding, filled once spotted
    ctx.lineWidth = 1.5;
    for (const b of bubbles) {
      if (b.khz < this.lo || b.khz > this.hi) continue;
      const y = (this.hi - b.khz) / (this.hi - this.lo) * h;
      ctx.beginPath(); ctx.arc(w - 9, y, 4.25, 0, 2 * Math.PI);
      ctx.fillStyle = b.spot ? this.accent : this.surface; ctx.fill();
      ctx.strokeStyle = this.accent; ctx.stroke();
    }
  }

  css(abgr) { return `rgb(${abgr & 255},${abgr >> 8 & 255},${abgr >> 16 & 255})`; }
}

// ---- the page ----

const app = {
  servers: [],  // {address, token}: a machine's server and its join code; its copies come from its seat
  tab: "servers",  // the dialog's tab: "server" or "<server index>:<copy name>"
  pending: {},  // "<server address>|<copy name>" -> settings picked but not yet applied
  mode: load("mode", "info"),  // the dialog shows Settings or Info
  want: load("current", ""),  // the copy shown, by name, wherever its port is today
  instances: [],
  current: 0,
  selectedKhz: -1,
  editing: -2,  // -2 no form, -1 adding, else the row being edited
  note: "",  // what the last search for servers found
  decoded: "lowest",
  matches: [],
};
const wf = new Waterfall($("wf"));

function inst() { return app.instances[app.current] || null; }

// "host" is an server on SERVER_PORT; "host:port" one on another port
function hostPort(address) {
  const m = address.trim().match(/^(.*?)(?::(\d+))?$/);
  return [m[1], m[2] ? Number(m[2]) : 0];
}
function serverUrl(address, path = "/server") {
  const [host, port] = hostPort(address);
  return `${location.protocol === "https:" ? "wss" : "ws"}://${host}:${port || SERVER_PORT}${path}`;
}
function urlParts(url) {
  try { const u = new URL(url); return [u.hostname.replace(/^\[|\]$/g, ""), Number(u.port) || DEFAULT_PORT]; } catch { return ["", DEFAULT_PORT]; }
}
function normalizeCode(code) { return code.toUpperCase().replace(/[ -]/g, ""); }
function checkServer(address, code) {
  const m = address.trim().match(/^([^\s/:#?@]+)(?::(\d+))?$/);
  if (!m) return "address: the machine's VPN or local IP address (host:port only for an server off " + SERVER_PORT + ")";
  if (m[2] && !(Number(m[2]) > 0 && Number(m[2]) < 65_536)) return "port: 1 to 65535";
  const c = normalizeCode(code);
  if (c.length !== CODE_LENGTH || [...c].some(ch => !CODE_ALPHABET.includes(ch))) {
    return `join code: the server's ${CODE_LENGTH} letters and digits (in its server.json, and its log at first start)`;
  }
  return "";
}
function shortUrl(url) { return url.replace(/^wss?:\/\//, "").replace(/\/feed$/, ""); }

// one try of a feed: "ok" once its hello arrives (a browser sees a refusal only as a close after the open),
// else "code", "lockout", "full" or "none"
function probeFeed(url, token) {
  return new Promise(resolve => {
    let ws = null, done = false;
    const finish = result => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      if (ws) ws.close();
      resolve(result);
    };
    const timer = setTimeout(() => finish("none"), SCAN_TIMEOUT_S * 1000);
    try {
      ws = new WebSocket(url, token ? [PROTOCOL, CODE_PROTOCOL + token] : [PROTOCOL]);
    } catch {
      finish("none");
      return;
    }
    ws.onmessage = e => {
      try { if (typeof e.data === "string" && JSON.parse(e.data).type === "hello") finish("ok"); } catch { /* not a feed */ }
    };
    ws.onclose = e => finish({ [CLOSE_WRONG_CODE]: "code", [CLOSE_LOCKED_OUT]: "lockout", [CLOSE_FULL]: "full" }[e.code] || "none");
  });
}

// the server answers on its port with hello {server: true}; a wrong code closes as a feed's does
function probeLink(address, token) {
  return new Promise(resolve => {
    let ws = null, done = false;
    const finish = r => { if (done) return; done = true; clearTimeout(timer); if (ws) ws.close(); resolve(r); };
    const timer = setTimeout(() => finish("none"), SCAN_TIMEOUT_S * 1000);
    try { ws = new WebSocket(serverUrl(address), [SERVER_PROTOCOL, CODE_PROTOCOL + token]); } catch { finish("none"); return; }
    ws.onmessage = e => { try { const m = JSON.parse(e.data); if (m.type === "hello" && m.server) finish("server"); } catch { /* not an server */ } };
    ws.onclose = e => finish({ [CLOSE_WRONG_CODE]: "code", [CLOSE_LOCKED_OUT]: "lockout" }[e.code] || "none");
  });
}

// a machine's server: its seat (every instance, running or not), its copies' feeds, and requests to it
class ServerLink {
  constructor(server) {
    this.server = server;
    this.ws = null;
    this.hello = null;
    this.seat = null;
    this.state = "connecting";
    this.next = 1;
    this.waiting = new Map();  // request id -> resolve
    this.retry = 0;
    this.closed = false;
  }
  open() {
    let ws;
    try { ws = new WebSocket(serverUrl(this.server.address), [SERVER_PROTOCOL, CODE_PROTOCOL + this.server.token]); } catch { this.dropped(); return; }
    this.ws = ws;
    ws.onmessage = e => { try { this.message(JSON.parse(e.data)); } catch { /* not JSON */ } };
    ws.onclose = e => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.state = e.code === CLOSE_WRONG_CODE ? "wrong join code" : e.code === CLOSE_LOCKED_OUT ? "locked out" : "no answer";
      this.dropped();
    };
  }
  dropped() {
    for (const resolve of this.waiting.values()) resolve({ ok: false, error: "server connection lost" });
    this.waiting.clear();
    renderSettings();
    // a wrong code is never retried: every try counts toward the server's lockout; Edit fixes it
    if (this.closed || this.state === "wrong join code") return;
    const wait = this.state === "locked out" ? LOCKOUT_S : RECONNECT_S[Math.min(this.retry++, RECONNECT_S.length - 1)];
    this.timer = setTimeout(() => this.open(), wait * 1000);
  }
  close() { this.closed = true; clearTimeout(this.timer); if (this.ws) this.ws.close(); }
  message(m) {
    if (m.type === "hello") { this.hello = m; this.state = "live"; this.retry = 0; renderConns(); }
    else if (m.type === "seat") {
      this.seat = m;
      const names = new Set((m.instances || []).map(i => i.name));
      dropPending(this.server.address, name => !names.has(name));
      serverCopies(this.server); renderSettings();
    }
    else if (m.type === "server_result" && this.waiting.has(m.id)) { this.waiting.get(m.id)(m); this.waiting.delete(m.id); }
    else if (m.type === "log" && Array.isArray(m.lines)) {
      this.log = (m.reset ? [] : this.log || []).concat(m.lines).slice(-SERVER_LOG_KEPT);
      if (app.mode === "info" && app.tab === "servers") renderSettings();
    }
  }
  request(cmd, name, settings) {
    return new Promise(resolve => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) { resolve({ ok: false, error: "server not connected" }); return; }
      const id = this.next++;
      this.waiting.set(id, resolve);
      this.ws.send(JSON.stringify({ type: "server", id, cmd, name, ...(settings ? { settings } : {}) }));
    });
  }
}

// an server's running copies, each opened through it with the server's code
function serverCopies(s) {
  const urls = (s.link.seat.instances || []).filter(i => i.state === "running" && i.feed_port)
    .map(i => serverUrl(s.address, "/feed/" + encodeURIComponent(i.name)));
  reconcileUrls(s, urls);
}
// the server's name for a copy opened through it, or "" for a copy found by searching ports
function copyName(i) {
  const m = i && i.server && i.server.link && i.url.match(/\/feed\/([^/]+)$/);
  return m ? decodeURIComponent(m[1]) : "";
}

function saveServers() {
  save("servers", app.servers.map(({ address, token }) => ({ address, token, kind: "server" })));
}

function addInstance(url, token, at, server = null) {
  const i = new Instance(url, token);
  i.server = server;
  i.on = what => changed(i, what);
  if (at === undefined) app.instances.push(i); else app.instances.splice(at, 0, i);
  i.open();
  return i;
}

function changed(i, what) {
  // a copy that comes back on another port is followed by name, but never away from a live one
  if (what === "hello" && i.name === app.want && i !== inst() && !(inst() && inst().state === "live")) {
    setCurrent(app.instances.indexOf(i)); return;
  }
  if (what === "hello" && i === inst() && !app.want) { app.want = i.name; save("current", i.name); }
  if (what === "state") { renderConns(); if (i === inst()) renderState(); return; }
  if (i !== inst()) { if (what === "hello") { renderConns(); renderBands(); } return; }
  if (what === "hello") { wf.fit(); renderBands(); renderState(); renderStation(); renderSettings(); renderConns(); renderLadder(); }
  else if (what === "status") { renderStatus(); if (app.mode === "info") renderSettings(); }
  else if (what === "ladder") renderLadder();
  else if (what === "detail") { renderDetail(); renderStatus(); }
}

function setCurrent(n) {
  app.current = Math.max(0, Math.min(n, app.instances.length - 1));
  const i = inst();
  if (i && i.hello.run) { app.want = i.name; save("current", i.name); }
  app.selectedKhz = -1;
  wf.sel = -1;
  wf.setInstance(inst());
  renderAll();
}


// the saved servers; kind "agent": saved before servers were called servers
function storedServers() {
  const saved = load("servers", []) || [];
  return saved.filter(s => s && ["server", "agent"].includes(s.kind) && s.address).map(s => ({ address: s.address, token: s.token || "" }));
}

// one saved before servers (a copy's own address and code) is dropped, and the note names it
function loadServers() {
  const saved = load("servers", []) || [];
  app.servers = storedServers();
  const dropped = saved.filter(s => s && s.address && !app.servers.some(sv => sv.address === s.address)).map(s => s.address);
  if (dropped.length || load("connections", null)) {
    app.migrated = `Removed, saved by an older version${dropped.length ? `: ${dropped.join(", ")}` : ""}. Add each SparkGap server again with its server's join code.`;
  }
  save("connections", null);
  saveServers();
  for (const s of app.servers) { s.link = new ServerLink(s); s.link.open(); }
}

// the saved list is the truth: another tab may have added, changed or removed a server since
// this one loaded, and writing this tab's old list back would undo that
function syncServers() {
  const stored = storedServers();
  for (const sv of [...app.servers]) {
    const s = stored.find(x => x.address === sv.address);
    if (!s || s.token !== sv.token) dropServer(sv);
  }
  for (const s of stored) {
    if (app.servers.some(sv => sv.address === s.address)) continue;
    s.link = new ServerLink(s);
    app.servers.push(s);
    s.link.open();
  }
  app.servers.sort((a, b) => stored.findIndex(x => x.address === a.address) - stored.findIndex(x => x.address === b.address));
}

function dropServer(sv) {
  if (sv.link) sv.link.close();
  dropPending(sv.address, () => true);
  app.instances.filter(i => i.server === sv).forEach(removeInstance);
  app.servers.splice(app.servers.indexOf(sv), 1);
}

// a page served by a copy (its feed's "web": true, no server) shows that copy and nothing else
async function startHere() {
  if (!/^https?:$/.test(location.protocol)) return false;
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/feed`, token = load("here", "");
  const r = await probeFeed(url, token);
  if (r === "none") return false;  // an server's page, or no SparkGap: the server list
  app.here = { url };
  addInstance(url, token);
  return true;
}

function reconcileUrls(s, ok) {
  const was = inst();
  app.instances.filter(i => i.server === s && !ok.includes(i.url) && i.state !== "live").forEach(removeInstance);
  for (const url of ok) if (!app.instances.some(i => i.url === url)) addInstance(url, s.token, undefined, s);
  if (inst() !== was || (!inst() && app.instances.length)) setCurrent(app.current);
  renderAll();
}

function removeInstance(i) {
  const n = app.instances.indexOf(i);
  if (n < 0) return;
  app.instances.splice(n, 1)[0].close();
  if (app.current >= app.instances.length || app.current > n) app.current = Math.max(0, app.current - 1);
}

function pick(khz, call) {
  app.selectedKhz = khz;
  wf.sel = khz;
  const i = inst();
  if (i) i.select(khz, call);
  renderLadder();
}

// centre the waterfall on a match (keeping the span) and select it
function goTo(m) {
  const b = inst() ? inst().bandAt(m.khz) : -1;
  if (b >= 0 && b !== inst().band) { inst().goKhz = m.khz; setBand(b); }  // the match is on another band
  const span = wf.hi - wf.lo;
  wf.setView(m.khz - span / 2, m.khz + span / 2);
  pick(m.khz, m.call);  // a typed frequency is taken as typed
  $("search").blur();
  $("matches").hidden = true;
}

// ---- rendering ----

function renderAll() {
  renderBands(); renderState(); renderStation(); renderLadder(); renderDetail(); renderStatus(); renderSettings(); renderConns(); renderView();
}

function renderState() {
  const i = inst(), dot = $("stateDot"), cs = getComputedStyle(document.documentElement);
  if (!i) {
    dot.style.background = cs.getPropertyValue("--faint");
    $("stateText").textContent = "No feed";
    $("wfmsg").textContent = "No feed configured";
    return;
  }
  dot.style.background = cs.getPropertyValue(i.state === "live" ? "--good" : i.state === "connecting" ? "--accent" : "--bad");
  const word = { live: "Live", connecting: "Connecting", refused: "Refused" }[i.state] || "Reconnecting";
  // the station by the state only when no picker shows it
  $("stateText").textContent = word + (i.station && $("bandSel").hidden ? "  " + i.station : "")
    + (i.state !== "live" && i.error ? "  ·  " + i.error : "");
  let msg = "";
  if (i.state !== "live") msg = i.station ? "Connecting to " + i.station : "Connecting";
  else if (!i.highKhz) msg = "Waiting for spectrum";
  $("wfmsg").textContent = msg;
}

// ---- status page: radio, SparkGap, decoding and this console, for troubleshooting ----

const NEAR_WAV_LIMIT = 3.5 * 2 ** 30;  // record_wav files break past 4 GiB
const LOST_WARN = 0.001;  // packets lost above this share of those received are worth a look
const dash = v => (v === null || v === undefined || v === "" ? "-" : String(v));
const utcTime = t => (t ? new Date(t * 1000).toISOString().slice(11, 19) + "Z" : "-");
function duration(s) {
  if (!(s >= 0)) return "-";
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  return h ? `${h}h ${m}m` : `${m}m ${Math.floor(s % 60)}s`;
}
const gb = b => (b === null || b === undefined ? "-" : b >= 2 ** 30 ? (b / 2 ** 30).toFixed(2) + " GiB" : (b / 2 ** 20).toFixed(0) + " MiB");

// text selected inside el: a redraw would drop it, so Status holds still until it is cleared
function selecting(el) {
  const sel = getSelection();
  return !!sel && !sel.isCollapsed && el.contains(sel.anchorNode);
}

// clipboard: the API needs a secure page; plain http falls back to the browser's copy command
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(() => true, () => false);
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.append(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return Promise.resolve(ok);
}

// a copy's status from its own feed, as HTML; a: what its server says of it (top calls), or null
function statusHtml(i, a) {
  const s = (i && i.status) || {}, h = (i && i.hello) || {}, run = h.run || {}, r = s.radio || {};
  const clock = s.clock_synced === true ? "NTP synchronised" : s.clock_synced === false ? "not synchronised" : "unknown";
  const num = v => (v === null || v === undefined ? "-" : Number(v).toLocaleString("en-US"));
  // one row: label left, value right; mono for numbers; sub is a muted second line (a file's folder)
  // o.key: a value that signals trouble (full weight); o.ref: reference detail (muted); o.bad: red
  const row = (k, v, o = {}) => `<div class="srow"><span class="k">${esc(k)}</span><span class="v${o.mono ? " mono" : ""}${o.bad ? " bad" : ""}${o.key ? " key" : ""}${o.ref ? " ref" : ""}${o.act ? " act" : ""}"`
    + `${o.title ? ` title="${esc(o.title)}"` : ""}${o.act ? ` data-act="${o.act}"` : ""}>${esc(dash(v))}${o.sub ? `<span class="sub">${esc(o.sub)}</span>` : ""}</span></div>`;
  const file = p => { const n = String(p).lastIndexOf("/"); return n < 0 ? [p, ""] : [p.slice(n + 1), p.slice(0, n)]; };
  const fileRow = (k, p, none, o = {}) => {
    if (!p) return row(k, none, o);
    const [name, dir] = file(p);
    return row(k, name, Object.assign({ sub: dir, title: p }, o));
  };
  const pill = (text, cls) => `<span class="pill ${cls}">${esc(text)}</span>`;
  const card = (title, rows, wide, tag) => `<section class="scard${wide ? " wide" : ""}"><h3><span>${title}</span>${tag || ""}</h3>${rows.join("")}</section>`;

  const live = ["live", "replaying"].includes(r.state);
  const head = `<div class="infohead"><span class="sname">${esc(i.station || "")}</span>`
    + (r.state ? `<span class="pill ${live ? "ok" : "warn"}">${esc(String(r.state).toUpperCase())}</span>` : "") + "</div>";
  const tile = (k, v, sub, bad, primary) => `<div class="stat${bad ? " bad" : ""}${primary ? " primary" : ""}"><div class="label">${k}</div><div class="mono">${esc(dash(v))}</div>`
    + `${sub ? `<div class="tsub">${esc(sub)}</div>` : ""}</div>`;
  const tiles = [tile("UPTIME", duration(s.uptime_s)),
    tile("SPOTS", num(s.spots), s.spots_hour !== undefined ? `${s.spots_hour} last hour` : "", false, true),
    tile("CALLS", num(s.calls), "", false, true), tile("WINDOWS", num(s.windows), "", false, true),
    tile("BINS", num(s.bins), s.bins_peak !== undefined ? `peak ${s.bins_peak}` : ""),
    tile("CPU", s.cpu !== undefined && s.cpu !== null ? s.cpu + "%" : "-", "of a core" + (s.rss_mb ? ` · ${s.rss_mb} MB` : ""))];

  const lossy = r.packets > 0 && r.lost / r.packets > LOST_WARN;
  const radio = [row("Type", r.type === "flex" ? "FlexRadio" : r.type === "hpsdr" ? "HPSDR" : r.type === "file" ? "File replay" : r.type, { ref: true }),
    r.type === "file" ? fileRow("File", r.address, "-", { ref: true }) : row("Address", r.address, { mono: true, ref: true })];
  if ("station" in r) radio.push(row("Station", r.station, { ref: true }), row("DAX-IQ channel", r.daxiq_channel, { mono: true, ref: true }));
  const mhz = khz => (khz / 1000).toFixed(3);
  (h.bands || []).forEach((b, n) => {
    const tag = h.bands.length > 1 ? ` ${bandLabel(b)}` : "", d = (s.decode_khz || [])[n];
    radio.push(row("IQ" + tag, `${mhz(b.center_khz)} MHz, ${b.span_khz} kHz`, { mono: true, ref: true }));
    if (d) radio.push(row("Decoding" + tag, `${mhz(d[0])} to ${mhz(d[1])} MHz`, { mono: true, ref: true }));
  });
  if ("packets" in r) radio.push(row("Packets", num(r.packets), { mono: true, ref: true }), row("Lost", num(r.lost), { mono: true, key: true, bad: lossy }));
  if ("pkt_per_s" in r) radio.push(row("Packets a second", num(r.pkt_per_s), { mono: true, key: true }));
  if ("reconnects" in r) radio.push(row("Reconnects", r.reconnects ? `${r.reconnects}, last ${utcTime(r.last_reconnect)}` : "none", { mono: !!r.reconnects, key: true, bad: r.reconnects > 0 }));
  if ("station" in r) radio.push(row("Other stations", (r.others || []).join(", ") || "none", { key: true }));
  if ("position_s" in r) radio.push(row("Position", duration(r.position_s), { mono: true }));

  const rec = s.recording, near = rec && rec.bytes > NEAR_WAV_LIMIT;
  const top = a && (a.top_calls || []).length ? a.top_calls.slice(0, 8).map(([c, k]) => `${c} ${num(k)}`).join(", ") : "";
  const sg = [row("Host", h.host, { ref: true }), row("Build", h.build, { mono: true, ref: true }), row("Clock", clock, { ref: true }),
    row("Decoder", h.decoder, { ref: true }),
    row("Bands", (h.bands || []).map(bandLabel).join(", "), { key: true }),
    fileRow("Config", run.config, "-", { ref: true }), fileRow("Log", run.log, "terminal", { ref: true }),
    rec ? fileRow("Recording", rec.file, "off", { bad: near, ref: !near }) : row("Recording", "off", { ref: true }),
    ...(rec ? [row("Recording size", gb(rec.bytes) + (near ? "  (near the 4 GiB limit)" : ""), { mono: true, key: true, bad: near })] : []),
    row("IQ trimmed", s.iq_trimmed_s !== undefined ? s.iq_trimmed_s + " s" + (s.iq_trimmed_s > 0 ? "  (decoder behind)" : "") : null, { mono: true, key: true, bad: s.iq_trimmed_s > 0 })];

  const out = (s.telnet ? [row("Telnet port", s.telnet.port, { mono: true, ref: true }), row("Telnet clients", s.telnet.clients, { mono: true, key: true })]
    : [row("Telnet", "not reported")]).concat(top ? [row("Top calls", top, { ref: true })] : []);

  const rate = i.statTotal !== undefined ? (i.total - i.statTotal) / Math.max(0.5, now() - i.statAt) : null;
  i.statTotal = i.total; i.statAt = now();
  const cons = [row("This console", `since ${utcTime(i.since)}${i.reconnects ? `, ${i.reconnects} reconnects` : ""}`, { mono: true, ref: true }),
    row("Rows a second", rate === null ? null : rate.toFixed(0), { mono: true, key: true }),
    ...(s.clients || []).map(c => row(c.name || c.peer, `${num(c.dropped)} dropped`, { mono: true, sub: `${c.name ? `${c.peer}, ` : ""}since ${utcTime(c.since)}`, key: true, bad: c.dropped > 600 }))];

  const p = s.problems || {};
  const probs = [`<div class="srow"><span class="k">Since start</span><span class="v"><span class="mono${p.errors ? " bad" : ""}">${num(p.errors)}</span> errors</span></div>`,
    ...(p.last || []).slice().reverse().map(e => `<div class="prob"><span class="mono t">${utcTime(e.utc)}</span>`
      + `<span class="pill warn">${esc(e.level)}</span><span class="m">${esc(e.msg)}</span></div>`)];

  // health at a glance: what needs attention, worst first
  const issues = [];
  if (r.state && !live) issues.push(["bad", `Radio ${r.state}`]);
  if (s.iq_trimmed_s > 0) issues.push(["bad", `Decoder behind: ${s.iq_trimmed_s} s of IQ trimmed`]);
  if (s.clock_synced === false) issues.push(["bad", "Clock not NTP synchronised"]);
  if (p.errors > 0) issues.push(["bad", `${num(p.errors)} errors since start`]);
  if (r.reconnects > 0) issues.push(["warn", `Radio reconnected ${r.reconnects}x, last ${utcTime(r.last_reconnect)}`]);
  if (lossy) issues.push(["warn", `${num(r.lost)} packets lost (${(100 * r.lost / r.packets).toFixed(2)}%)`]);
  if (near) issues.push(["warn", "Recording near the 4 GiB limit"]);
  for (const c of s.clients || []) if (c.dropped > 600) issues.push(["warn", `Console ${c.name || c.peer} falling behind`]);
  const worst = issues.some(x => x[0] === "bad") ? "bad" : issues.length ? "warn" : "ok";
  const banner = `<div class="health ${worst}">${issues.length ? issues.map(([lv, t]) => `<span class="${lv}">${esc(t)}</span>`).join("")
    : "<span>Healthy: radio live, decoding keeping up, no errors</span>"}</div>`;
  const tagRadio = r.state ? pill(String(r.state).toUpperCase(), live ? (r.reconnects || lossy ? "warnlvl" : "ok") : "warn") : "";
  const tagSg = s.clock_synced === false ? pill("CLOCK OFF", "warn") : s.clock_synced ? pill("CLOCK OK", "ok") : "";
  const tagProb = p.errors ? pill(`${num(p.errors)} ERRORS`, "warn") : pill("NONE", "ok");

  return head + banner + `<div class="stats stiles">${tiles.join("")}</div><div class="scards">`
    + card("RADIO", radio, false, tagRadio) + card("SPARKGAP", sg, false, tagSg) + card("SPOT OUTPUT", out) + card("CONSOLES", cons)
    + card("PROBLEMS", probs, true, tagProb) + "</div>";
}

// band picker: shown when one SparkGap runs several bands
// the band picker: every band of every connected server ("Beelink 40m"), when there is a choice
const HAM_BANDS = [["160m", 1800, 2000], ["80m", 3500, 4000], ["60m", 5330, 5410], ["40m", 7000, 7300], ["30m", 10100, 10150],
  ["20m", 14000, 14350], ["17m", 18068, 18168], ["15m", 21000, 21450], ["12m", 24890, 24990], ["10m", 28000, 29700]];
// a band's name; a band given as a frequency is named by the amateur band it falls in
function bandLabel(b) {
  if (!/^\d+$/.test(String(b.name))) return String(b.name);
  const h = HAM_BANDS.find(([, lo, hi]) => b.center_khz >= lo && b.center_khz <= hi);
  return h ? h[0] : `${b.center_khz} kHz`;
}
function bandChoices() {
  const out = [];
  app.instances.forEach((i, n) => {
    const host = i.name || shortUrl(i.url);
    const bands = i.bands.length ? i.bands : [null];
    bands.forEach((b, k) => out.push({ n, band: k, label: b ? `${host} ${bandLabel(b)}` : host }));
  });
  return out;
}

function renderBands() {
  const i = inst(), all = i ? bandChoices() : [], sel = $("bandSel");
  sel.hidden = all.length < 2;
  if (all.length < 2) return;
  sel.innerHTML = all.map((c, k) => `<option value="${k}">${esc(c.label)}</option>`).join("");
  sel.value = String(all.findIndex(c => c.n === app.current && c.band === i.band));
  sel.onchange = () => {
    const c = all[Number(sel.value)];
    if (c.n !== app.current) setCurrent(c.n);
    if (c.band !== inst().band) setBand(c.band); else renderAll();
  };
}

function setBand(n) {
  const i = inst();
  if (!i) return;
  i.setBand(n);
  app.selectedKhz = -1;
  wf.sel = -1;
  renderAll();
}

function renderStation() {
  const i = inst(), c = (i && i.hello.config) || {};
  $("station").textContent = [c.callsign, c.enable_ft8 ? "FT8 on" : ""].filter(x => x).join("  ·  ");
}

function yOf(khz, h) { return (wf.hi - khz) / Math.max(0.001, wf.hi - wf.lo) * h; }

function renderTicks() {
  const c = $("ticks"), w = c.clientWidth, h = c.clientHeight, dpr = window.devicePixelRatio || 1;
  c.width = w * dpr; c.height = h * dpr;
  const ctx = c.getContext("2d"), cs = getComputedStyle(document.documentElement);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (wf.hi <= wf.lo) return;
  ctx.font = "11px 'IBM Plex Mono', ui-monospace, monospace";
  ctx.textBaseline = "middle";
  const span = wf.hi - wf.lo, every = span > 40 ? 10 : span > 10 ? 5 : 1, step = span > 200 ? 5 : 1;
  for (let k = Math.ceil(wf.lo / step) * step; k <= wf.hi; k += step) {
    const y = Math.round(yOf(k, h)) + 0.5, major = k % every === 0;
    ctx.strokeStyle = cs.getPropertyValue(major ? "--muted" : "--edge").trim();
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(major ? 10 : 6, y); ctx.stroke();
    if (major) { ctx.fillStyle = cs.getPropertyValue("--muted").trim(); ctx.fillText(String(k), 12, y); }
  }
}

function renderLadder() {
  const box = $("chips"), i = inst(), h = $("ladder").clientHeight;
  box.textContent = "";
  if (!i || wf.hi <= wf.lo) return;
  let last = -99;
  for (const it of i.ladder(wf.lo, wf.hi)) {
    // top-down: push each chip below the one above, so they never overlap
    const y = Math.max(yOf(it.khz, h), last + 24);
    last = y;
    if (y >= h - 8) break;
    const chip = document.createElement("div");
    chip.className = "chip" + (it.spot ? " spot" : "") + (it.call ? " call" : "") + (Math.abs(app.selectedKhz - it.khz) < 0.05 ? " sel" : "");
    chip.style.top = (y - 11) + "px";
    chip.innerHTML = `<span class="cd"></span><span class="cc">${esc(it.call || it.khz.toFixed(2))}</span><span class="cw">${it.wpm ? Math.round(it.wpm) : ""}</span>`;
    chip.onclick = () => pick(it.khz, it.call);
    box.appendChild(chip);
  }
}

// a zoom's fixed point: the picked frequency while it is on screen, so it stays where it is; else `at`
function zoomAnchor(at) { return wf.sel > 0 && wf.sel >= wf.lo && wf.sel <= wf.hi ? wf.sel : at; }
// zoom to a span of `span` kHz about zoomAnchor(at)
function zoomTo(span, at) {
  const a = zoomAnchor(at), f = (a - wf.lo) / Math.max(0.001, wf.hi - wf.lo);
  wf.setView(a - f * span, a - f * span + span);
}

// the zoom step the view is at (index into ZOOM_STEPS), or -1 between steps
function zoomStep(i) {
  const full = i.highKhz - i.lowKhz, span = wf.hi - wf.lo;
  return ZOOM_STEPS.findIndex(([, d]) => Math.abs(span - full / d) <= full / d * ZOOM_MATCH);
}

function renderView() {
  const i = inst(), full = i ? i.highKhz - i.lowKhz : 0, span = wf.hi - wf.lo;
  const k = i && full > 0 ? zoomStep(i) : 0;  // the button names the zoom shown
  $("full").textContent = k >= 0 ? ZOOM_STEPS[k][0] : Math.round(100 * span / full) + "%";
  $("range").textContent = wf.hi > wf.lo ? `${wf.lo.toFixed(1)} – ${wf.hi.toFixed(1)} kHz` : "";
  const s = $("span");
  s.max = Math.max(3, full);
  s.min = full * MIN_SPAN;
  if (document.activeElement !== s) s.value = span;
  $("spanText").textContent = span > 0 ? span.toFixed(0) + " kHz" : "";
  // the scroll bar covers the receiver's span; its thumb is the part on screen
  const fs = $("fscroll");
  $("fspace").style.height = (full > 0 && span > 0 ? fs.clientHeight * full / span : fs.clientHeight) + "px";
  const top = full > 0 ? (i.highKhz - wf.hi) / full * fs.scrollHeight : 0;
  if (Math.abs(fs.scrollTop - top) >= 1) fs.scrollTop = top;
  renderTicks();
  renderLadder();
}

function costText(v) { return v === null || v === undefined ? "-" : v >= NO_FIT ? "no fit" : v.toFixed(2); }
function rateName(r) { return r === 100 ? "narrow" : r === 200 ? "wide" : "cost"; }

// the readings of a window to show: both, one filter width, or the lower timing cost (a tie or no fit keeps wide)
function readings(r, mode) {
  const all = [{ html: r.html, rate: r.rate, cost: r.cost }];
  if (r.altHtml) all.push({ html: r.altHtml, rate: r.altRate, cost: r.altCost });
  if (mode === "both" || all.length === 1) return all;
  const wide = all.find(x => x.rate === 200) || all[0];
  if (mode === "wide") return [wide];
  if (mode === "narrow") return [all.find(x => x.rate === 100) || all[0]];
  const k = c => (c === null || c === undefined || c >= NO_FIT ? Infinity : c);
  return [k(all[0].cost) === k(all[1].cost) ? wide : k(all[0].cost) < k(all[1].cost) ? all[0] : all[1]];
}

function renderDetail() {
  const i = inst(), d = i && app.selectedKhz > 0 ? i.detail : null;
  $("dTitle").textContent = d ? d.call || d.khz.toFixed(2) : "Pick a signal";
  const pill = $("dPill");
  pill.hidden = !d;
  if (d) { pill.textContent = d.spotted ? "SPOTTED" : "HEARD"; pill.className = "pill" + (d.spotted ? " spot" : ""); }
  $("dKhz").textContent = d ? d.khz.toFixed(2) : "-";
  $("dWpm").textContent = d && d.wpm ? Math.round(d.wpm) : "-";
  $("dSnr").textContent = d && d.snr ? Math.round(d.snr) + " dB" : "-";
  $("dNote").hidden = !(d && d.note);
  $("dNote").textContent = d ? d.note : "";
  // both decodes of each window: wide filter on top, narrow dimmed
  $("dCopy").hidden = !(d && d.windows.length);
  $("dWins").innerHTML = (d ? d.windows : []).map(r => {
    const rs = readings(r, app.decoded);
    return `<div class="win"><span class="t">${r.utc}</span><div class="x">`
      + rs.map((x, n) => `<span${n ? ' class="alt"' : ""}>${x.html}</span>`).join("")
      + `<span class="c">${rs.length > 1 || r.altHtml ? rs.map(x => `${rateName(x.rate)} ${costText(x.cost)}`).join("  ·  ") : "cost " + costText(r.cost)}</span>`
      + "</div></div>";
  }).join("");
}

function renderStatus() {
  const i = inst(), s = i ? i.status : {}, up = s.uptime_s || 0;
  const rec = i && s && s.recording;  // the server is writing IQ: REC beside the state
  $("rec").hidden = !rec;
  $("rec").title = rec ? `Recording IQ to ${rec.file}` : "";
  const items = [["Load", s.cpu !== undefined && s.cpu !== null ? s.cpu + "%" : "-"], ["Memory", s.rss_mb ? s.rss_mb + " MB" : "-"],
    ["Bins", s.bins !== undefined ? s.bins : "-"], ["Consoles", s.clients ? s.clients.length : "-"],
    ["Decoder", i && i.hello.decoder ? i.hello.decoder : "-"], ["Server Build", i && i.hello.build ? i.hello.build : "-"],
    ["Up", `${Math.floor(up / 3600)}h ${Math.floor(up % 3600 / 60)}m`]];
  $("statusbar").innerHTML = items.map(([k, v]) => `<span><span class="k">${k}</span><span class="v">${esc(String(v))}</span></span>`).join("");
}

function seg(el, labels, current, onPick) {
  el.textContent = "";
  labels.forEach((l, n) => {
    const s = document.createElement("span");
    s.textContent = l;
    if (n === current) s.className = "on";
    if (onPick) s.onclick = () => onPick(n);
    el.appendChild(s);
  });
}

// Settings: an Servers tab (servers, this browser) and a tab for each copy an server runs
function settingsTabs() {
  const tabs = [["servers", "Servers"]], servers = app.servers.filter(sv => sv.link);
  for (const sv of servers) {
    const k = app.servers.indexOf(sv), host = (sv.link.hello && sv.link.hello.host) || sv.address;
    for (const i of (sv.link.seat && sv.link.seat.instances) || []) {
      tabs.push([`${k}:${i.name}`, servers.length > 1 ? `${i.name} · ${host}` : i.name]);
    }
  }
  return tabs;
}

// the copy behind a copy tab: [server index, server, server's instance, its feed (null while stopped)]
function tabCopy(key) {
  const k = Number(key.split(":")[0]), name = key.slice(key.indexOf(":") + 1), sv = app.servers[k];
  const a = sv && sv.link && sv.link.seat && sv.link.seat.instances.find(x => x.name === name);
  const feed = a ? app.instances.find(x => x.server === sv && copyName(x) === name) || null : null;
  return [k, sv, a, feed];
}

// the tab of the copy on screen, so the dialog opens on it
function currentTab() {
  const i = inst(), name = copyName(i);
  return name ? `${app.servers.indexOf(i.server)}:${name}` : "servers";
}

function renderSettings() {
  if ($("modal").hidden) return;
  // a page from a SparkGap server opens the guide it ships with; one from a copy or a file, GitHub's
  $("help").href = /^https?:$/.test(location.protocol) && !app.here ? "/help" : HELP_URL;
  const tabs = settingsTabs(), info = app.mode === "info";
  if (!tabs.some(t => t[0] === app.tab)) app.tab = "servers";
  seg($("modeSeg"), ["Settings", "Info"], info ? 1 : 0, n => { app.mode = n ? "info" : "settings"; save("mode", app.mode); renderSettings(); });
  $("tabs").innerHTML = tabs.map(([key, label]) => `<button class="tab${key === app.tab ? " on" : ""}" data-tab="${esc(key)}">${esc(label)}</button>`).join("");
  $("tabServers").hidden = info || app.tab !== "servers";
  $("tabCopy").hidden = info || app.tab === "servers";
  $("infoBody").hidden = !info;
  if (info) {
    const box = $("infoBody");
    if (selecting(box)) return;
    if (app.here && app.tab === "servers") { box.innerHTML = statusHtml(app.instances[0], null); return; }
    if (app.tab === "servers") {
      // the consoles keep their place: at the bottom they follow new lines, scrolled up they stay
      const was = {};
      for (const c of box.querySelectorAll("[data-console]")) {
        was[c.dataset.console] = c.scrollHeight - c.scrollTop - c.clientHeight < 4 ? -1 : c.scrollTop;
      }
      box.innerHTML = serversInfoHtml();
      for (const c of box.querySelectorAll("[data-console]")) {
        const at = was[c.dataset.console];
        c.scrollTop = at === undefined || at < 0 ? c.scrollHeight : at;
      }
      return;
    }
    const [, , a, feed] = tabCopy(app.tab);
    box.innerHTML = feed && feed.status ? statusHtml(feed, a) : stoppedInfoHtml(a);
    return;
  }
  $("setNote").textContent = app.seatNote || "";
  $("setNote").className = "small " + (app.seatBad ? "err" : "muted");
  if (app.tab === "servers") {
    if (!selecting($("conns"))) renderConns();
    seg($("themeSeg"), ["Dark", "Light"], document.documentElement.dataset.theme === "light" ? 1 : 0, n => setTheme(n ? "light" : "dark"));
    seg($("decSeg"), ["Both", "Wide", "Narrow", "Lowest cost"], DECODED.indexOf(app.decoded), n => {
      app.decoded = DECODED[n]; save("decoded", app.decoded); renderSettings(); renderDetail();
    });
    return;
  }
  const box = $("tabCopy");
  if (selecting(box) || (document.activeElement && box.contains(document.activeElement) && document.activeElement.tagName === "SELECT")) return;
  const [k, sv, i] = tabCopy(app.tab);
  box.innerHTML = i ? copyCards(k, sv, i) : "";
}

// Info on the Servers tab: each server, its station and a line for each copy
function serversInfoHtml() {
  const servers = app.servers.filter(sv => sv.link);
  if (!servers.length) return '<div class="small muted">No server yet: add one under Settings.</div>';
  return '<div class="scards">' + servers.map(sv => {
    const a = sv.link, seat = a.seat || { instances: [], stations: [] };
    const rows = [`<div class="srow"><span class="k">Server</span><span class="v">${esc(a.state === "live" ? `${(a.hello && a.hello.host) || ""} (${a.hello && a.hello.build ? a.hello.build : "-"})` : a.state)}</span></div>`];
    for (const st of seat.stations || []) {
      const loss = st.last_loss ? `, last ${utcTime(st.last_loss[0])}: ${st.last_loss[1]}` : "";
      rows.push(`<div class="srow"><span class="k">Station</span><span class="v">${esc(`${st.station || st.radio}: ${st.live || st.state}${typeof st.rtt_ms === "number" ? `, ping ${st.rtt_ms} ms` : ""}, ${Number(st.losses_hour) || 0} losses in the last hour${loss}`)}</span></div>`);
    }
    for (const i of seat.instances || []) {
      const up = i.state === "running" && i.started ? `, up ${duration(now() - i.started)}` : "";
      rows.push(`<div class="srow"><span class="k">${esc(i.name)}</span><span class="v">${esc(`${i.band}, ${i.busy || i.state}${up}, ${Number(i.spots) || 0} spots${i.errors ? `, ${Number(i.errors) || 0} errors` : ""}`)}</span></div>`);
    }
    const lines = (a.log || []).map(l => `<div class="cl ${l.level === "ERROR" || l.level === "CRITICAL" ? "bad" : l.level === "WARNING" ? "warn" : ""}">`
      + `<span class="t">${esc(utcTime(l.utc))}</span> ${esc(String(l.msg))}</div>`).join("");
    return `<section class="scard wide"><h3><span>${esc(sv.address)}</span></h3>${rows.join("")}`
      + `<div class="label conhead">SERVER LOG</div><div class="console mono" data-console="${app.servers.indexOf(sv)}">${lines || '<div class="cl faint">nothing logged yet</div>'}</div></section>`;
  }).join("") + "</div>";
}

// Info on a stopped copy's tab: what its server knows
function stoppedInfoHtml(a) {
  if (!a) return "";
  const row = (k, v) => `<div class="srow"><span class="k">${esc(k)}</span><span class="v">${esc(dash(v))}</span></div>`;
  return `<section class="scard wide"><h3><span>${esc(a.name)} · ${esc(String(a.band))}</span></h3>`
    + row("State", a.busy || a.state) + row("Build", a.build) + row("Last log", a.log)
    + (a.last_error ? `<div class="small err">${esc(a.last_error)}</div>` : "") + "</section>";
}

// Settings lists servers (address and join code); each line shows the copies found on it
function renderConns() {
  const box = $("conns");
  box.textContent = "";
  const cs = getComputedStyle(document.documentElement);
  const here = app.here ? [{ address: location.host, here: true }] : [];
  here.concat(app.servers).forEach((sv, n) => {
    const copies = sv.here ? app.instances : app.instances.filter(i => i.server === sv);
    const live = copies.filter(i => i.state === "live");
    const a = sv.link, ok = sv.here ? live.length > 0 : a && a.state === "live";
    const row = document.createElement("div");
    row.className = "conn" + (copies.includes(inst()) ? " cur" : "");
    const dot = document.createElement("span");
    dot.className = "sdot";
    dot.style.background = cs.getPropertyValue(ok ? "--good" : !sv.here && a && a.state === "connecting" ? "--accent" : "--bad");
    const name = document.createElement("span"); name.className = "n"; name.textContent = sv.here ? `This SparkGap (${sv.address})` : sv.address;
    const what = document.createElement("span"); what.className = "u" + (ok ? "" : " bad");
    what.textContent = sv.here ? (copies[0] ? copies[0].error || copies[0].title : "")
      : a && a.state !== "live" ? (a.state === "connecting" ? "connecting..." : a.state)
      : copies.length ? copies.map(i => i.title).join(", ") : "no copies running";
    row.append(dot, name, what);
    const edit = document.createElement("button"); edit.className = "btn small"; edit.textContent = sv.here ? "Join code" : "Edit";
    edit.onclick = e => { e.stopPropagation(); editConn(sv.here ? -3 : n - here.length); };
    row.append(edit);
    if (!sv.here) {
      const del = document.createElement("button"); del.className = "btn small"; del.textContent = "✕";
      del.onclick = e => { e.stopPropagation(); removeConn(n - here.length); };
      row.append(del);
    }
    row.onclick = () => { if (copies.length) setCurrent(app.instances.indexOf(copies[0])); };
    const card = document.createElement("section");
    card.className = "scard wide";
    card.appendChild(row);
    if (a && a.state === "live") {
      const lines = [`Server ${(a.hello && a.hello.host) || sv.address}`];
      for (const st of (a.seat && a.seat.stations) || []) {
        const loss = st.last_loss ? `, last ${utcTime(st.last_loss[0])}: ${st.last_loss[1]}` : "";
        lines.push(`Station ${st.station || st.radio}: ${st.live || st.state}${typeof st.rtt_ms === "number" ? `, ping ${st.rtt_ms} ms` : ""}, losses in the last hour ${Number(st.losses_hour) || 0}${loss}`);
      }
      for (const text of lines) { const d = document.createElement("div"); d.className = "small muted"; d.textContent = text; card.appendChild(d); }
    }
    box.appendChild(card);
  });
  if (!app.servers.length && !app.here && app.editing === -2) {
    const p = document.createElement("div"); p.className = "small muted"; p.textContent = "No servers yet."; box.appendChild(p);
  }
  for (const note of [app.migrated, app.editing === -2 ? app.note : ""]) {  // the migration note stays until an server is added
    if (!note) continue;
    const p = document.createElement("div"); p.className = "small muted"; p.textContent = note; box.appendChild(p);
  }
  $("connForm").hidden = app.editing === -2;
  $("addConn").hidden = app.editing !== -2 || !!app.here;
  $("fAddr").hidden = app.editing === -3;
  $("fSave").textContent = $("fSave").disabled ? "Searching..." : app.editing === -1 ? "Add" : "Save";
}

function editConn(n) {
  const sv = app.servers[n];
  // a new server defaults to the machine that served this page, if one did
  const served = /^https?:$/.test(location.protocol);
  $("fAddr").value = sv ? sv.address : served ? location.hostname : "";
  $("fCode").value = n === -3 ? load("here", "") : sv ? sv.token : "";
  $("fErr").hidden = true;
  app.editing = n;
  app.form = {};  // this form; a search finished after it closed is dropped
  $("fSave").disabled = false;
  app.note = "";
  renderConns();
  $("fAddr").focus();
}

// a server is kept once a search of its address finds a SparkGap that takes the code
async function saveConn() {
  const n = app.editing, form = app.form, token = normalizeCode($("fCode").value);
  if (app.here) {  // a page served by a copy: only its code
    save("here", token);
    location.reload();
    return;
  }
  const address = $("fAddr").value.trim();
  let err = checkServer(address, token);
  if (!err && app.servers.some((sv, k) => k !== n && sv.address === address)) err = "already in the list";
  if (err) { $("fErr").textContent = err; $("fErr").hidden = false; return; }
  $("fErr").hidden = true;
  $("fSave").disabled = true;
  $("fSave").textContent = "Searching...";
  const found = await probeLink(address, token);
  if (app.form !== form) return;  // closed meanwhile; another form may be open
  $("fSave").disabled = false;
  if (found !== "server") {
    $("fErr").textContent = {
      code: "wrong join code for that server",
      lockout: `locked out after wrong join codes: try again in ${LOCKOUT_S / 60} min`,
    }[found] || `no SparkGap server answered at that address (port ${hostPort(address)[1] || SERVER_PORT})`;
    $("fErr").hidden = false;
    renderConns();
    return;
  }
  const was = n >= 0 && app.servers[n] ? app.servers[n].address : null;
  syncServers();  // change the saved list as it is now, not as this tab last read it
  const old = app.servers.find(x => x.address === was || x.address === address);
  const at = old ? app.servers.indexOf(old) : app.servers.length;  // an edit keeps its place
  if (old) dropServer(old);
  const sv = { address, token };
  app.servers.splice(at, 0, sv);
  app.editing = -2; app.form = null;
  app.note = "";
  app.migrated = "";
  saveServers();
  sv.link = new ServerLink(sv);
  sv.link.open();
  renderSettings();
}

function removeConn(n) {
  const address = app.servers[n] && app.servers[n].address;
  syncServers();
  const sv = app.servers.find(x => x.address === address);
  if (sv) dropServer(sv);
  app.editing = -2; app.form = null;
  saveServers();
  setCurrent(app.current);
}

// a copy's tab: its state and actions, its everyday settings, then the spot rule's finer options
function copyCards(k, sv, i) {
  const run = i.state === "running", busy = i.busy, saved = i.settings || {};
  const pend = app.pending[pendKey(sv, i.name)] || {}, st = { ...saved, ...pend }, n = Object.keys(pend).length;
  const live = run && n === 1 && "band" in pend;  // a band change alone moves a running copy in place
  const at = `data-server="${k}" data-name="${esc(i.name)}"`;
  const btn = (cmd, label, on) => `<button class="btn small" ${at} data-cmd="${cmd}"${on && !busy ? "" : " disabled"}>${label}</button>`;
  const choices = (k2, opts, cur) => `<div class="seg${k2 in pend ? " pend" : ""}" ${at} data-key="${k2}">${opts.map(([v, l]) =>
    `<span data-value="${esc(JSON.stringify(v))}"${v === cur ? ' class="on"' : ""}>${esc(l)}</span>`).join("")}</div>`;
  const onOff = (k2, cur) => choices(k2, [[false, "off"], [true, "on"]], !!cur);
  const row = (label, control, hint) => `<span class="label"${hint ? ` title="${esc(hint)}"` : ""}>${label}</span>${control}`;  // hint: on hover
  const bands = `<select class="bandsel${"band" in pend ? " pend" : ""}" ${at} data-key="band"${busy ? " disabled" : ""}>${(saved.bands_known && saved.bands_known.length ? saved.bands_known : [saved.band])
    .map(b => `<option${b === st.band ? " selected" : ""}>${esc(String(b))}</option>`).join("")}</select>`;
  const state = busy ? `${busy}...` : run ? "running" : "stopped";
  const err = !run && i.last_error ? `<div class="small err">${esc(i.last_error)}</div>`
    : run && i.feed_failed ? '<div class="small err">Its feed did not start (port in use): no waterfall until it restarts.</div>' : "";
  const shown = !!inst() && inst().server === sv && copyName(inst()) === i.name;
  const host = (sv.link.hello && sv.link.hello.host) || sv.address, valid = st.validation || "off";
  const bar = n ? `<div class="applybar"><span class="small">${n} change${n > 1 ? "s" : ""}</span><span class="grow"></span>`
    + `<button class="btn small primary" ${at} data-cmd="apply"${busy ? " disabled" : ""}>${live ? "Apply (changes band live)" : run ? `Apply (restarts ${esc(i.name)})` : "Save"}</button>`
    + `<button class="btn small" ${at} data-cmd="discard">Discard</button></div>` : "";
  return `<section class="scard wide"><h3><span>${esc(i.name)} · ${esc(String(saved.band || i.band || ""))}</span><span>${esc(host)}</span></h3>
    <div class="actions"><span class="${run ? "good" : "muted"}">${esc(state)}</span><span class="grow"></span>${btn("show", shown ? "Shown" : "Show", run && !shown)}
      <span class="group">${btn("start", "Start", !run)}${btn("restart", "Restart", run)}${btn("stop", "Stop", run)}</span></div>${err}</section>
    <section class="scard wide"><h3><span>SETTINGS</span><span>${run ? `band changes live, the rest by restarting ${esc(i.name)}` : "used at its next start"}</span></h3>
    <div class="grid setgrid">${row("BAND", bands)}${row("RECORDING", onOff("record", st.record))}${row("CALL VALIDATION", `<div class="inline">${choices("validation", VALIDATION.map(v => [v, v]), valid)}${valid === "off" ? '<span class="small faint">off (set in its config)</span>' : ""}</div>`, HINTS.validation)}${row("KNOWN CALL LIST", choices("call_source", CALL_SOURCES, st.call_source), HINTS.call_source)}</div>
    <details class="adv"${app.advOpen ? " open" : ""}><summary>ADVANCED: SPOT RULE</summary>
    <div class="grid setgrid">${RULE_FLAGS.map(([k2, l, hint]) => row(l.toUpperCase(), onOff(k2, st[k2]), hint)).join("")}</div></details>${bar}</section>`;
}

// drop the pending settings of an server's copies that gone(name) says are gone
function dropPending(address, gone) {
  for (const k of Object.keys(app.pending)) {
    const bar = k.lastIndexOf("|");
    if (k.slice(0, bar) === address && gone(k.slice(bar + 1))) delete app.pending[k];
  }
}

// pending settings belong to an server by its address, which does not move when the list changes
function pendKey(sv, name) { return `${sv.address}|${name}`; }

// a setting picked: pending until Apply; picked back to what the copy runs, it is no longer pending
function pickSetting(link, name, key, value) {
  const k = pendKey(link.server, name), i = ((link.seat && link.seat.instances) || []).find(x => x.name === name);
  const saved = (i && i.settings) || {}, pend = app.pending[k] || (app.pending[k] = {});
  if (saved[key] === value || (key === "validation" && (saved[key] || "off") === value)) delete pend[key]; else pend[key] = value;
  if (!Object.keys(pend).length) delete app.pending[k];
  renderSettings();
}

async function seatRequest(link, cmd, name, settings) {
  app.seatNote = `${cmd === "set" ? "changing" : cmd} ${name}...`; app.seatBad = false; renderSettings();
  const r = await link.request(cmd, name, settings);
  app.seatNote = r.ok ? r.message : `${name}: ${r.error}`; app.seatBad = !r.ok;
  renderSettings();
  return r.ok;
}

// Apply: every pending change in one request, so a running copy restarts once; asked first
async function applySettings(link, name) {
  const k = pendKey(link.server, name), settings = app.pending[k] ? { ...app.pending[k] } : null;  // what is sent
  if (!settings) return;
  const i = ((link.seat && link.seat.instances) || []).find(x => x.name === name);
  const what = Object.entries(settings).map(([key, v]) => `${key.replace(/^spot_rule_/, "").replace(/_/g, " ")} ${v === true ? "on" : v === false ? "off" : v}`).join(", ");
  const live = Object.keys(settings).length === 1 && "band" in settings;  // no restart unless the copy cannot
  if (i && i.state === "running" && !live && !confirm(`${name}: ${what}. This restarts ${name}. Go ahead?`)) return;
  const ok = await seatRequest(link, "set", name, settings);
  const now = app.pending[k];
  if (ok && now) {  // clear what was sent; a choice picked while it was sent stays pending
    for (const [key, v] of Object.entries(settings)) if (now[key] === v) delete now[key];
    if (!Object.keys(now).length) delete app.pending[k];
  }
  renderSettings();
}

function seatTarget(t) {
  const holder = t.closest("[data-server]");
  const sv = holder && app.servers[Number(holder.dataset.server)];
  return sv && sv.link ? [sv.link, holder.dataset.name, holder] : [null, "", null];
}

function openSettings() { app.tab = currentTab(); $("modal").hidden = false; renderSettings(); }
function closeSettings() { $("modal").hidden = true; app.editing = -2; app.form = null; $("fSave").disabled = false; }

// ---- input ----

function bindInput() {
  $("gear").onclick = () => ($("modal").hidden ? openSettings() : closeSettings());
  $("tabs").onclick = e => {
    const t = e.target.closest("[data-tab]");
    if (t) { app.tab = t.dataset.tab; app.editing = -2; renderSettings(); }
  };
  $("tabCopy").onclick = e => {
    const t = e.target.closest("[data-cmd], [data-value]");
    if (!t || t.disabled) return;
    const [link, name, holder] = seatTarget(t);
    if (!link) return;
    if (t.dataset.cmd === "show") {
      const n = app.instances.findIndex(x => x.server === link.server && copyName(x) === name);
      if (n >= 0) { setCurrent(n); closeSettings(); }
    } else if (t.dataset.cmd === "apply") applySettings(link, name);
    else if (t.dataset.cmd === "discard") { delete app.pending[pendKey(link.server, name)]; renderSettings(); }
    else if (t.dataset.cmd) {
      if (t.dataset.cmd !== "start" && !confirm(`${t.dataset.cmd === "stop" ? "Stop" : "Restart"} ${name}?`)) return;
      seatRequest(link, t.dataset.cmd, name);
    } else if (!t.classList.contains("on")) pickSetting(link, name, holder.dataset.key, JSON.parse(t.dataset.value));
  };
  $("tabCopy").addEventListener("toggle", e => { if (e.target.classList.contains("adv")) app.advOpen = e.target.open; }, true);
  $("tabCopy").onchange = e => {
    const [link, name, holder] = seatTarget(e.target);
    if (!link || holder.dataset.key !== "band") return;
    e.target.blur();  // a focused select holds back the redraw that shows Apply
    pickSetting(link, name, "band", e.target.value);
  };
  $("dCopy").onclick = () => copyText($("dWins").innerText).then(ok => {
    $("dCopy").textContent = ok ? "Copied" : "Copy failed";
    setTimeout(() => { $("dCopy").textContent = "Copy"; }, 1500);
  });
  $("close").onclick = closeSettings;
  $("modal").onmousedown = e => { if (e.target === $("modal")) closeSettings(); };
  $("addConn").onclick = () => editConn(-1);
  $("fSave").onclick = saveConn;
  $("fCancel").onclick = () => { app.editing = -2; app.form = null; $("fSave").disabled = false; renderConns(); };
  for (const id of ["fAddr", "fCode"]) $(id).onkeydown = e => { if (e.key === "Enter") saveConn(); };

  const search = $("search"), list = $("matches");
  const showMatches = () => {
    const i = inst();
    app.matches = i ? i.search(search.value) : [];
    list.innerHTML = app.matches.map((m, n) => `<div class="match${n ? "" : " first"}" data-n="${n}">`
      + `<span class="cd" style="width:6px;height:6px;border-radius:50%;border:1px solid var(${m.spot ? "--accent" : "--faint"});background:${m.spot ? "var(--accent)" : "transparent"}"></span>`
      + `<span class="mono" style="width:90px;font-weight:600">${esc(m.label)}</span>`
      + `<span class="mono muted" style="width:80px">${m.call ? m.khz.toFixed(2) : ""}</span>`
      + `<span class="ago">${m.ago ? m.ago + " ago" : "go to"}</span></div>`).join("");
    list.hidden = !app.matches.length || document.activeElement !== search;
  };
  search.oninput = showMatches;
  search.onfocus = showMatches;
  search.onblur = () => { list.hidden = true; };
  search.onkeydown = e => {
    if (e.key === "Enter" && app.matches.length) goTo(app.matches[0]);
    else if (e.key === "Escape") { search.value = ""; search.blur(); }
  };
  list.onmousedown = e => {  // before the blur hides the list
    const m = e.target.closest(".match");
    if (m) { e.preventDefault(); goTo(app.matches[Number(m.dataset.n)]); }
  };
  document.addEventListener("keydown", e => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") { e.preventDefault(); search.focus(); search.select(); }
    else if (e.key === "Escape" && !$("modal").hidden) closeSettings();
  });

  // waterfall: a tap picks the nearest signal, a drag pans, the wheel pans, Ctrl+wheel or a pinch zooms
  const box = $("wfbox"), pointers = new Map();
  let drag = null;
  box.addEventListener("pointerdown", e => {
    box.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, e.clientY);
    const r = box.getBoundingClientRect();
    if (pointers.size === 1) drag = { y: e.clientY, lo: wf.lo, moved: false };
    else if (pointers.size === 2) {
      const ys = [...pointers.values()];
      drag = { pinch: Math.abs(ys[0] - ys[1]) || 1, lo: wf.lo, hi: wf.hi, at: zoomAnchor(wf.khzAt((ys[0] + ys[1]) / 2 - r.top)), moved: true };
    }
  });
  box.addEventListener("pointermove", e => {
    if (!pointers.has(e.pointerId) || !drag) return;
    pointers.set(e.pointerId, e.clientY);
    const h = box.clientHeight;
    if (drag.pinch) {
      const ys = [...pointers.values()], f = drag.pinch / (Math.abs(ys[0] - ys[1]) || 1);
      wf.setView(drag.at - (drag.at - drag.lo) * f, drag.at + (drag.hi - drag.at) * f);
      return;
    }
    const dy = e.clientY - drag.y;
    if (Math.abs(dy) > 3) drag.moved = true;
    if (drag.moved) { box.classList.add("drag"); const span = wf.hi - wf.lo; wf.setView(drag.lo + dy / h * span, drag.lo + dy / h * span + span); }
  });
  const up = e => {
    if (!pointers.delete(e.pointerId)) return;
    box.classList.remove("drag");
    if (drag && !drag.moved && pointers.size === 0) {
      const i = inst(), r = box.getBoundingClientRect(), y = e.clientY - r.top, h = box.clientHeight;
      const tol = 10 / h * (wf.hi - wf.lo);  // 10 px either side
      const near = i ? i.nearestSignal(wf.khzAt(y), tol) : -1;
      if (near > 0) pick(near, "");
    }
    drag = pointers.size === 1 ? { y: [...pointers.values()][0], lo: wf.lo, moved: true } : pointers.size ? drag : null;
  };
  box.addEventListener("pointerup", up);
  box.addEventListener("pointercancel", up);
  box.addEventListener("wheel", e => {
    e.preventDefault();
    const h = box.clientHeight, span = wf.hi - wf.lo, px = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
    if (e.ctrlKey) zoomTo(span * Math.pow(0.8, -px / 100), wf.khzAt(e.clientY - box.getBoundingClientRect().top));
    else wf.pan(-px / h * span);
  }, { passive: false });

  // frequency scroll bar; a scroll that only follows the view (under a pixel off) is ignored
  const fs = $("fscroll");
  fs.addEventListener("scroll", () => {
    const i = inst();
    if (!i || !i.nBins) return;
    const full = i.highKhz - i.lowKhz, span = wf.hi - wf.lo, hi = i.highKhz - fs.scrollTop / fs.scrollHeight * full;
    if (Math.abs(hi - wf.hi) >= full / fs.scrollHeight) wf.setView(hi - span, hi);
  });

  $("span").oninput = e => zoomTo(Number(e.target.value), (wf.lo + wf.hi) / 2);
  $("full").onclick = () => {  // the next step: Full, Half, Quarter of the band, about the pick or the view's centre
    const i = inst();
    if (!i || !i.nBins) return;
    const k = zoomStep(i), d = ZOOM_STEPS[k < 0 ? 0 : (k + 1) % ZOOM_STEPS.length][1];
    if (d === 1) wf.setView(i.lowKhz, i.highKhz);
    else zoomTo((i.highKhz - i.lowKhz) / d, (wf.lo + wf.hi) / 2);
  };
  window.addEventListener("resize", () => { wf.dirty = true; renderView(); });
}

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  save("theme", t);
  wf.setColors();
  renderAll();
}

// ---- start ----

function start() {
  wf.onview = renderView;
  bindInput();
  setTheme(load("theme", "dark"));
  app.decoded = DECODED.includes(load("decoded", "lowest")) ? load("decoded", "lowest") : "lowest";
  startHere().then(here => {
    if (!here) loadServers();
    // another tab changed the saved servers: show the same list here
    if (!here) window.addEventListener("storage", e => {
      if (e.key === STORE + "servers") { syncServers(); setCurrent(app.current); renderAll(); }
    });
    // no server saved yet: Settings opens on Add server
    setCurrent(app.current);
    if (!here && !app.servers.length) { openSettings(); editConn(-1); }
  });
  setInterval(() => { $("utc").textContent = "UTC: " + new Date().toISOString().substr(11, 8); }, 250);
  setInterval(() => { renderLadder(); }, 1000);
  // instances not on screen still move their rows into history, so switching shows their recent waterfall
  setInterval(() => app.instances.forEach((i, n) => { if (n !== app.current) i.advance(); }), 50);
  const frame = () => { const i = inst(); wf.frame(i ? i.heardList : []); requestAnimationFrame(frame); };
  requestAnimationFrame(frame);
}

start();
