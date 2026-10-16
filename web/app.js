// SparkGap Web: the browser console. Same feed, layout and logic as the Qt console (console/).
"use strict";

const PROTOCOL = "sparkgap.v1";
const CODE_PROTOCOL = "sparkgap.code.";  // the join code, offered as a subprotocol (a browser cannot set headers)
const FEED_VERSION = 1;
const MAX_BINS = 16_384;
const DEFAULT_PORT = 7320;
const SCAN_PORTS = Array.from({ length: 20 }, (_, k) => DEFAULT_PORT + k);  // tried when a server is added without a port
const SCAN_TIMEOUT_S = 3;
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
const DEMO_URL = "demo";
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
    this.demo = url === DEMO_URL;
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
    if (this.demo) {
      this.source = new Demo(this);
      this.setState("live");
      return;
    }
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

  get station() {
    const b = this.bands[this.band] || this.bands[0];
    const band = b ? bandLabel(b) : "";
    return [this.hello.host, band].filter(x => x).join(" · ");
  }

  get title() { return this.demo ? "Demo" : this.station || this.url.split("//").pop().split("/")[0]; }

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
      return [{ call: "", khz, ago: "", spot: false, label: `${khz.toFixed(1)} kHz` }];
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

// ---- demo: made-up signals sent as feed messages, so the page runs the same code as on a live feed ----

const MORSE = {
  A: ".-", B: "-...", C: "-.-.", D: "-..", E: ".", F: "..-.", G: "--.", H: "....", I: "..", J: ".---", K: "-.-", L: ".-..", M: "--",
  N: "-.", O: "---", P: ".--.", Q: "--.-", R: ".-.", S: "...", T: "-", U: "..-", V: "...-", W: ".--", X: "-..-", Y: "-.--", Z: "--..",
  0: "-----", 1: ".----", 2: "..---", 3: "...--", 4: "....-", 5: ".....", 6: "-....", 7: "--...", 8: "---..", 9: "----.", "/": "-..-.",
};
// [kHz, wpm, SNR dB, sent text, spotted]; null text: a carrier with nothing decoded
const DEMO_SIGNALS = [
  [7016.8, 21, 9, "TU 5NN TX", false], [7019.3, 25, 14, "CQ CQ DE N3AD N3AD K", true],
  [7021.2, 22, 18, "CQ CQ DE AF4PX AF4PX K", true], [7024.6, 26, 23, "CQ CQ DE AC5P AC5P K", false],
  [7027.9, 30, 16, "CQ NA4J NA4J TEST", true], [7031.3, 18, 27, "CQ CQ CQ DE K4WLO K4WLO K", true],
  [7033.0, 32, 12, "TEST K3WW K3WW", true], [7036.4, 20, 20, "CQ CQ DE WB5NHH WB5NHH PSE K", false],
  [7039.8, 24, 15, "CQ DE W3RJ W3RJ K", true], [7041.5, 27, 11, "CQ N4ZZ N4ZZ", false],
  [7046.2, 16, 19, "GM OM UR RST 579 579 NAME BOB BOB QTH OH", false], [7052.7, 0, 13, null, false],
  [7055.6, 19, 21, "CQ SKCC DE KD8QWX KD8QWX K", true], [7061.0, 23, 8, "R R TU 73 EE", false],
];
const DEMO_BAND = { name: "40m", center_khz: 7048.0, span_khz: 96.0 };
const DEMO_BINS = 2048;
const DEMO_SUBSTEPS = 4;  // keying sampled within each row, so edges blend

function keying(text, wpm) {  // [[on, seconds], ...] for one sending of text, then a pause
  const u = 1.2 / wpm, out = [];
  text.split(" ").forEach((word, wi) => {
    if (wi) out.push([0, 7 * u]);
    [...word].forEach((ch, ci) => {
      if (ci) out.push([0, 3 * u]);
      [...(MORSE[ch] || "")].forEach((el, ei) => { if (ei) out.push([0, u]); out.push([1, el === "." ? u : 3 * u]); });
    });
  });
  out.push([0, 2 + Math.random() * 3]);
  return out;
}

// a decoded reading of sent text: mostly right, sometimes a split or a stray letter
function misread(text, rough) {
  return text.split(" ").map(w => {
    const r = Math.random();
    if (r < rough * 0.3 && w.length > 2) return w.slice(0, 2) + " " + w.slice(2);
    if (r < rough * 0.5) return w.replace(/[A-Z]/, c => ({ E: "I", T: "M", A: "W", S: "H", N: "D" })[c] || c);
    return w;
  }).join(" ") + (Math.random() < rough ? " E" : "");
}

class Demo {
  constructor(inst) {
    this.inst = inst;
    this.start = now();
    this.spots = 0;
    this.hzPerBin = DEMO_BAND.span_khz * 1000 / DEMO_BINS;
    this.firstHz = DEMO_BAND.center_khz * 1000 - DEMO_BAND.span_khz * 500;
    this.sigs = DEMO_SIGNALS.map(([khz, wpm, snr, text, spot]) => {
      const k = text ? keying(text, wpm) : [[1, 1]];
      const total = k.reduce((a, [, d]) => a + d, 0);
      return { khz, wpm, snr, text, spot, k, total, phase: Math.random() * total, next: now() + 2 + Math.random() * 8, seen: 0, spotted: false };
    });
    inst.message({
      type: "hello", v: FEED_VERSION, call: "DEMO", decoder: "itila2",
      config: {
        callsign: "DEMO", cw_decoder: "itila2", sample_rate: 96_000, flex_daxiq_channel: null, telnet_port: 7300,
        enable_ft8: false, spot_rule: "repeat",
      },
      bands: [DEMO_BAND],
    });
    inst.message({ type: "snapshot", signals: [], windows: [], spots: [], status: null });
    this.timers = [
      setInterval(() => this.rows(), 1000 / SPECTRUM_HZ),
      setInterval(() => this.decode(), 500),
      setInterval(() => this.statusMsg(), 2000),
    ];
    this.lastRow = now();
  }

  stop() { this.timers.forEach(clearInterval); }

  keyed(s, t) {
    let x = (t + s.phase) % s.total;
    for (const [on, d] of s.k) { if (x < d) return on; x -= d; }
    return 0;
  }

  rows() {
    // every row due since the last call, so a throttled timer still fills the waterfall
    const t1 = now(), step = 1 / SPECTRUM_HZ;
    let t = this.lastRow + step;
    if (t1 - t > 2) t = t1 - 2;
    for (; t <= t1; t += step) this.row(t);
    this.lastRow = t - step;
  }

  row(t) {
    const unit = 0.25, row = new Uint8Array(DEMO_BINS);
    for (let i = 0; i < DEMO_BINS; i++) {
      const p = (-Math.log(Math.random() + 1e-9) - Math.log(Math.random() + 1e-9) - Math.log(Math.random() + 1e-9)) / 3;
      const db = 10 * Math.log10(p + 1e-3) + 1.5;  // dB over the floor, as the feed sends it
      row[i] = db > 0 ? Math.min(255, db / unit) : 0;
    }
    for (const s of this.sigs) {
      let on = 0;
      for (let k = 0; k < DEMO_SUBSTEPS; k++) on += this.keyed(s, t - k / (SPECTRUM_HZ * DEMO_SUBSTEPS));
      if (!on) continue;
      const db = s.snr + 1.5 * Math.sin(t * 0.7 + s.phase) + 10 * Math.log10(on / DEMO_SUBSTEPS);  // slow QSB, soft edges
      const pos = (s.khz * 1000 - this.firstHz) / this.hzPerBin;
      const b = Math.floor(pos), f = pos - b;
      for (const [i, lvl] of [[b - 1, db - 14], [b, db - 6 * f], [b + 1, db - 6 * (1 - f)], [b + 2, db - 14]]) {
        if (i >= 0 && i < DEMO_BINS) row[i] = Math.max(row[i], Math.min(255, Math.max(0, lvl / unit)));
      }
    }
    this.inst.row(t, this.firstHz, this.hzPerBin, unit, row);
  }

  decode() {
    const t = now();
    for (const s of this.sigs) {
      if (!s.text || t < s.next) continue;
      s.next = t + 5 + Math.random() * 4;
      s.seen++;
      const wpm = s.wpm + (Math.random() - 0.5) * 2, snr = s.snr + (Math.random() - 0.5) * 3;
      const rough = Math.max(0.05, 0.6 - s.snr / 40);
      const narrow = Math.random() < 0.08 ? "" : misread(s.text, rough * 1.5);
      const base = { type: "window", khz: s.khz, wpm: Math.round(wpm * 10) / 10, snr: Math.round(snr * 10) / 10, utc: t };
      this.inst.message({ ...base, text: narrow, cost: narrow ? 0.5 + Math.random() * 0.8 : NO_FIT });
      this.inst.message({ ...base, text: misread(s.text, rough), cost: 0.2 + Math.random() * 0.6 });
      if (s.spot && !s.spotted && s.seen >= 2) {
        s.spotted = true;
        this.spots++;
        const call = s.text.split(" ").find(w => CALL_RE.test(w));
        this.inst.message({ type: "spot", khz: s.khz, call, snr: base.snr, wpm: Math.round(wpm), utc: t });
      }
    }
  }

  statusMsg() {
    const active = this.inst.signals.size;
    this.inst.message({
      type: "status", cpu: Math.round(30 + Math.random() * 12), rss_mb: 330 + Math.round(Math.random() * 4),
      bins: active * 2 + 6, spots: this.spots, uptime_s: Math.round(now() - this.start), utc: now(),
    });
  }
}

// ---- the waterfall: frequency down, time flowing left ----

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
      const span = Math.min(Math.max(hi - lo, 2), inst.highKhz - inst.lowKhz);
      lo = Math.min(Math.max(lo, inst.lowKhz), inst.highKhz - span);
      hi = lo + span;
    }
    if (lo === this.lo && hi === this.hi) return;
    this.lo = lo; this.hi = hi;
    this.dirty = true;
    this.onview();
  }

  zoom(factor, at) { this.setView(at - (at - this.lo) * factor, at + (this.hi - at) * factor); }
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
    this.map = { interp: pxPerBin >= 1, a, b, frac, grow };
    this.v = new Float32Array(inst.nBins);
    this.wide = new Float32Array(inst.nBins);
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
  instances: [],
  current: 0,
  selectedKhz: -1,
  back: 0,  // the server shown before demo
  editing: -2,  // -2 no form, -1 adding, else the row being edited
  note: "",  // what the last search for servers found
  decoded: "lowest",
  matches: [],
};
const wf = new Waterfall($("wf"));

function inst() { return app.instances[app.current] || null; }

function feedUrl(address, port) {
  const secure = location.protocol === "https:";
  return `${secure ? "wss" : "ws"}://${address.trim()}:${port}/feed`;
}
function urlParts(url) {
  try { const u = new URL(url); return [u.hostname.replace(/^\[|\]$/g, ""), Number(u.port) || DEFAULT_PORT]; } catch { return ["", DEFAULT_PORT]; }
}
function normalizeCode(code) { return code.toUpperCase().replace(/[ -]/g, ""); }
function checkFeed(address, port, code) {
  const a = address.trim();
  if (!a || /[\s/:#?@]/.test(a)) return "address: a VPN or local IP address, nothing else";
  if (!(port > 0 && port < 65_536)) return "port: 1 to 65535";
  const c = normalizeCode(code);
  if (c && (c.length !== CODE_LENGTH || [...c].some(ch => !CODE_ALPHABET.includes(ch)))) {
    return `join code: ${CODE_LENGTH} letters and digits, as SparkGap's log shows it`;
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

// what a scan found: the ports to add and a line for the operator
function scanReport(found, listed) {
  const ports = r => found.filter(f => f[1] === r).map(f => f[0]);
  const add = ports("ok").filter(p => !listed.includes(p)), have = ports("ok").filter(p => listed.includes(p));
  const parts = [];
  if (add.length) parts.push(`Added ${add.join(", ")}.`);
  if (have.length) parts.push(`Already in the list: ${have.join(", ")}.`);
  if (ports("code").length) parts.push(`Different join code on ${ports("code").join(", ")}.`);
  if (ports("lockout").length) parts.push(`Locked out for ${LOCKOUT_S / 60} min on ${ports("lockout").join(", ")}.`);
  if (ports("full").length) parts.push(`Too many clients on ${ports("full").join(", ")}.`);
  if (!parts.length) parts.push(`No SparkGap answered on ports ${SCAN_PORTS[0]} to ${SCAN_PORTS[SCAN_PORTS.length - 1]}.`);
  return { add, text: parts.join(" ") };
}

function saveFeeds() { save("connections", app.instances.filter(i => !i.demo).map(i => ({ url: i.url, token: i.token }))); }

function addInstance(url, token, at) {
  const i = new Instance(url, token);
  i.on = what => changed(i, what);
  if (at === undefined) app.instances.push(i); else app.instances.splice(at, 0, i);
  i.open();
  return i;
}

function changed(i, what) {
  if (what === "state") { renderConns(); if (i === inst()) renderState(); return; }
  if (i !== inst()) { if (what === "hello") { renderConns(); renderBands(); } return; }
  if (what === "hello") { wf.fit(); renderBands(); renderState(); renderStation(); renderSettings(); renderConns(); renderLadder(); }
  else if (what === "status") { renderStatus(); if (!$("statusModal").hidden) renderStatusPage(); }
  else if (what === "ladder") renderLadder();
  else if (what === "detail") { renderDetail(); renderStatus(); }
}

function setCurrent(n) {
  app.current = Math.max(0, Math.min(n, app.instances.length - 1));
  app.selectedKhz = -1;
  wf.sel = -1;
  wf.setInstance(inst());
  renderAll();
}

// demo is a view: the servers stay connected (and keep their history) while it shows
function setDemo(on) {
  save("demo", on);
  app.editing = -2;
  const at = app.instances.findIndex(i => i.demo);
  if (on && at < 0) {
    app.back = app.current;
    addInstance(DEMO_URL, "");
    setCurrent(app.instances.length - 1);
  } else if (!on && at >= 0) {
    app.instances.splice(at, 1)[0].close();
    setCurrent(app.back || 0);
  }
}

function loadServers() {
  for (const f of load("connections", [])) if (f && f.url) addInstance(f.url, f.token || "");
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
  $("stateText").textContent = i.demo ? "DEMO MODE" : word + (i.station && $("bandSel").hidden ? "  " + i.station : "")
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

function renderStatusPage() {
  const i = inst(), s = (i && i.status) || {}, h = (i && i.hello) || {}, run = h.run || {}, r = s.radio || {};
  if (!i || i.demo) {
    $("statusHead").innerHTML = "";
    $("statusBody").innerHTML = '<div class="small muted">Status is shown for a connected SparkGap, not demo mode.</div>';
    return;
  }
  const num = v => (v === null || v === undefined ? "-" : Number(v).toLocaleString("en-US"));
  // one row: label left, value right; mono for numbers; sub is a muted second line (a file's folder)
  // o.key: a value that signals trouble (full weight); o.ref: reference detail (muted); o.bad: red
  const row = (k, v, o = {}) => `<div class="srow"><span class="k">${esc(k)}</span><span class="v${o.mono ? " mono" : ""}${o.bad ? " bad" : ""}${o.key ? " key" : ""}${o.ref ? " ref" : ""}"`
    + `${o.title ? ` title="${esc(o.title)}"` : ""}>${esc(dash(v))}${o.sub ? `<span class="sub">${esc(o.sub)}</span>` : ""}</span></div>`;
  const file = p => { const n = String(p).lastIndexOf("/"); return n < 0 ? [p, ""] : [p.slice(n + 1), p.slice(0, n)]; };
  const fileRow = (k, p, none, o = {}) => {
    if (!p) return row(k, none, o);
    const [name, dir] = file(p);
    return row(k, name, Object.assign({ sub: dir, title: p }, o));
  };
  const pill = (text, cls) => `<span class="pill ${cls}">${esc(text)}</span>`;
  const card = (title, rows, wide, tag) => `<section class="scard${wide ? " wide" : ""}"><h3><span>${title}</span>${tag || ""}</h3>${rows.join("")}</section>`;

  const live = ["live", "replaying"].includes(r.state);
  $("statusHead").innerHTML = `<span class="sname">${esc(i.station || "")}</span>`
    + (r.state ? `<span class="pill ${live ? "ok" : "warn"}">${esc(String(r.state).toUpperCase())}</span>` : "");
  const tile = (k, v, sub, bad, primary) => `<div class="stat${bad ? " bad" : ""}${primary ? " primary" : ""}"><div class="label">${k}</div><div class="mono">${esc(dash(v))}</div>`
    + `${sub ? `<div class="tsub">${esc(sub)}</div>` : ""}</div>`;
  const tiles = [tile("UPTIME", duration(s.uptime_s)),
    tile("SPOTS", num(s.spots), s.spots_hour !== undefined ? `${s.spots_hour} last hour` : "", false, true),
    tile("CALLS", num(s.calls), "", false, true), tile("WINDOWS", num(s.windows), "", false, true),
    tile("BINS", num(s.bins), s.bins_peak !== undefined ? `peak ${s.bins_peak}` : ""),
    tile("IQ TRIMMED", s.iq_trimmed_s !== undefined ? s.iq_trimmed_s + " s" : "-", s.iq_trimmed_s > 0 ? "decoder behind" : "", s.iq_trimmed_s > 0),
    tile("CPU", s.cpu !== undefined && s.cpu !== null ? s.cpu + "%" : "-", "of a core" + (s.rss_mb ? ` · ${s.rss_mb} MB` : ""))];

  const lossy = r.packets > 0 && r.lost / r.packets > LOST_WARN;
  const radio = [row("Type", r.type === "flex" ? "FlexRadio" : r.type === "hpsdr" ? "HPSDR" : r.type === "file" ? "File replay" : r.type, { ref: true }),
    r.type === "file" ? fileRow("File", r.address, "-", { ref: true }) : row("Address", r.address, { mono: true, ref: true })];
  if ("station" in r) radio.push(row("Station", r.station, { ref: true }), row("DAX-IQ channel", r.daxiq_channel, { mono: true, ref: true }));
  if ("packets" in r) radio.push(row("Packets", num(r.packets), { mono: true, ref: true }), row("Lost", num(r.lost), { mono: true, key: true, bad: lossy }));
  if ("pkt_per_s" in r) radio.push(row("Packets a second", num(r.pkt_per_s), { mono: true, key: true }));
  if ("reconnects" in r) radio.push(row("Reconnects", r.reconnects ? `${r.reconnects}, last ${utcTime(r.last_reconnect)}` : "none", { mono: !!r.reconnects, key: true, bad: r.reconnects > 0 }));
  if ("station" in r) radio.push(row("Other stations", (r.others || []).join(", ") || "none", { key: true }));
  if ("position_s" in r) radio.push(row("Position", duration(r.position_s), { mono: true }));

  const rec = s.recording, near = rec && rec.bytes > NEAR_WAV_LIMIT;
  const sg = [row("Host", h.host, { ref: true }), row("Build", h.build, { mono: true, ref: true }), row("Decoder", h.decoder, { ref: true }),
    row("Bands", (h.bands || []).map(b => `${bandLabel(b)} (${b.center_khz} kHz)`).join(", "), { key: true }),
    row("Started", utcTime(run.started), { mono: true, ref: true }),
    fileRow("Config", run.config, "-", { ref: true }), fileRow("Log", run.log, "terminal", { ref: true }),
    rec ? fileRow("Recording", rec.file, "off", { bad: near, ref: !near }) : row("Recording", "off", { ref: true }),
    ...(rec ? [row("Recording size", gb(rec.bytes) + (near ? "  (near the 4 GiB limit)" : ""), { mono: true, key: true, bad: near })] : []),
    row("Clock", s.clock_synced === true ? "NTP synchronised" : s.clock_synced === false ? "not synchronised" : "unknown", { key: true, bad: s.clock_synced === false })];

  const out = s.telnet ? [row("Telnet port", s.telnet.port, { mono: true, ref: true }), row("Telnet clients", s.telnet.clients, { mono: true, key: true })]
    : [row("Telnet", "not reported")];

  const rate = i.statTotal !== undefined ? (i.total - i.statTotal) / Math.max(0.5, now() - i.statAt) : null;
  i.statTotal = i.total; i.statAt = now();
  const cons = [row("This console", `since ${utcTime(i.since)}${i.reconnects ? `, ${i.reconnects} reconnects` : ""}`, { mono: true, ref: true }),
    row("Rows a second", rate === null ? null : rate.toFixed(0), { mono: true, key: true }),
    ...(s.clients || []).map(c => row(c.peer, `${num(c.dropped)} dropped`, { mono: true, sub: `since ${utcTime(c.since)}`, key: true, bad: c.dropped > 600 }))];

  const p = s.problems || {};
  const probs = [`<div class="srow"><span class="k">Since start</span><span class="v"><span class="mono${p.errors ? " bad" : ""}">${num(p.errors)}</span> errors, `
    + `<span class="mono">${num(p.warnings)}</span> warnings</span></div>`,
    ...(p.last || []).slice().reverse().map(e => `<div class="prob"><span class="mono t">${utcTime(e.utc)}</span>`
      + `<span class="pill ${e.level === "WARNING" ? "warnlvl" : "warn"}">${esc(e.level)}</span><span class="m">${esc(e.msg)}</span></div>`)];

  // health at a glance: what needs attention, worst first
  const issues = [];
  if (r.state && !live) issues.push(["bad", `Radio ${r.state}`]);
  if (s.iq_trimmed_s > 0) issues.push(["bad", `Decoder behind: ${s.iq_trimmed_s} s of IQ trimmed`]);
  if (s.clock_synced === false) issues.push(["bad", "Clock not NTP synchronised"]);
  if (p.errors > 0) issues.push(["bad", `${num(p.errors)} errors since start`]);
  if (r.reconnects > 0) issues.push(["warn", `Radio reconnected ${r.reconnects}x, last ${utcTime(r.last_reconnect)}`]);
  if (lossy) issues.push(["warn", `${num(r.lost)} packets lost (${(100 * r.lost / r.packets).toFixed(2)}%)`]);
  if (near) issues.push(["warn", "Recording near the 4 GiB limit"]);
  for (const c of s.clients || []) if (c.dropped > 600) issues.push(["warn", `Console ${c.peer} falling behind`]);
  if (p.warnings > 0) issues.push(["warn", `${num(p.warnings)} warnings since start`]);
  const worst = issues.some(x => x[0] === "bad") ? "bad" : issues.length ? "warn" : "ok";
  const banner = `<div class="health ${worst}">${issues.length ? issues.map(([lv, t]) => `<span class="${lv}">${esc(t)}</span>`).join("")
    : "<span>Healthy: radio live, decoding keeping up, no errors</span>"}</div>`;
  const tagRadio = r.state ? pill(String(r.state).toUpperCase(), live ? (r.reconnects || lossy ? "warnlvl" : "ok") : "warn") : "";
  const tagSg = s.clock_synced === false ? pill("CLOCK OFF", "warn") : s.clock_synced ? pill("CLOCK OK", "ok") : "";
  const tagProb = p.errors ? pill(`${num(p.errors)} ERRORS`, "warn") : p.warnings ? pill(`${num(p.warnings)} WARNINGS`, "warnlvl") : pill("NONE", "ok");

  $("statusBody").innerHTML = banner + `<div class="stats stiles">${tiles.join("")}</div><div class="scards">`
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
    if (i.demo) return;
    const host = i.hello.host || shortUrl(i.url);
    const bands = i.bands.length ? i.bands : [null];
    bands.forEach((b, k) => out.push({ n, band: k, label: b ? `${host} ${bandLabel(b)}` : host }));
  });
  return out;
}

function renderBands() {
  const i = inst(), all = i && !i.demo ? bandChoices() : [], sel = $("bandSel");
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
  const i = inst(), c = (i && !i.demo && i.hello.config) || {};  // demo: the status says DEMO MODE
  $("station").textContent = [c.callsign, c.flex_daxiq_channel ? "DAX IQ " + c.flex_daxiq_channel : "",
    c.sample_rate ? c.sample_rate / 1000 + " kHz" : "", c.telnet_port ? "telnet " + c.telnet_port : "",
    c.enable_ft8 ? "FT8 on" : ""].filter(x => x).join("  ·  ");
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
  const every = wf.hi - wf.lo > 40 ? 10 : 5, step = wf.hi - wf.lo > 200 ? 5 : 1;
  for (let k = Math.ceil(wf.lo / step) * step; k <= wf.hi; k += step) {
    const y = Math.round(yOf(k, h)) + 0.5, major = k % every === 0;
    ctx.strokeStyle = cs.getPropertyValue(major ? "--muted" : "--line").trim();
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
    chip.innerHTML = `<span class="cd"></span><span class="cc">${esc(it.call || it.khz.toFixed(1))}</span><span class="cw">${it.wpm ? Math.round(it.wpm) : ""}</span>`;
    chip.onclick = () => pick(it.khz, it.call);
    box.appendChild(chip);
  }
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
  $("dTitle").textContent = d ? d.call || d.khz.toFixed(1) : "Pick a signal";
  const pill = $("dPill");
  pill.hidden = !d;
  if (d) { pill.textContent = d.spotted ? "SPOTTED" : "HEARD"; pill.className = "pill" + (d.spotted ? " spot" : ""); }
  $("dKhz").textContent = d ? (d.spotKhz !== null ? d.spotKhz.toFixed(1) : d.khz.toFixed(2)) : "-";  // a spot as spotted
  $("dWpm").textContent = d && d.wpm ? Math.round(d.wpm) : "-";
  $("dSnr").textContent = d && d.snr ? Math.round(d.snr) + " dB" : "-";
  $("dNote").hidden = !(d && d.note);
  $("dNote").textContent = d ? d.note : "";
  // both decodes of each window: wide filter on top, narrow dimmed
  $("dWins").innerHTML = (d ? d.windows : []).map(r => {
    const rs = readings(r, app.decoded);
    return `<div class="win"><span class="t">${r.utc}</span><div class="x">`
      + rs.map((x, n) => `<span${n ? ' class="alt"' : ""}>${x.html}</span>`).join("")
      + `<span class="c">${rs.length > 1 || r.altHtml ? rs.map(x => `${rateName(x.rate)} ${costText(x.cost)}`).join("  ·  ") : "cost " + costText(r.cost)}</span>`
      + "</div></div>";
  }).join("");
}

function renderStatus() {
  const i = inst(), s = i ? i.status : {}, d = i && app.selectedKhz > 0 ? i.detail : null, up = s.uptime_s || 0;
  const items = [["Load", s.cpu !== undefined && s.cpu !== null ? s.cpu + "%" : "-"], ["Memory", s.rss_mb ? s.rss_mb + " MB" : "-"],
    ["Bins", s.bins !== undefined ? s.bins : "-"], ["Selected", d && d.wpm ? Math.round(d.wpm) + " WPM" : "-"],
    ["Spots", s.spots !== undefined ? s.spots : "-"], ["Decoder", i && i.hello.decoder ? i.hello.decoder : "-"],
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

function renderSettings() {
  const demo = app.instances.some(x => x.demo);
  seg($("demoSeg"), ["Off", "On"], demo ? 1 : 0, n => { if ((n === 1) !== demo) setDemo(n === 1); });
  seg($("decSeg"), ["Both", "Wide", "Narrow", "Lowest cost"], DECODED.indexOf(app.decoded), n => {
    app.decoded = DECODED[n]; save("decoded", app.decoded); renderSettings(); renderDetail();
  });
  $("connSection").hidden = demo;
  $("demoHint").hidden = !demo;
  const c = (inst() && inst().hello.config) || {}, v = ["off", "permissive", "normal", "aggressive"];
  seg($("sRule"), ["off", "on"], c.spot_rule === "repeat" ? 1 : c.spot_rule ? 0 : -1);
  seg($("sValid"), v, v.indexOf(c.spot_validation || (c.spot_rule ? "off" : "")));
  seg($("sCalls"), ["SCP", "RBN", "RBN + SKCC"], ["scp", "rbn", "rbn+skcc"].indexOf(c.spot_validation_calls));
  $("build").textContent = (inst() && inst().hello.build) || "";  // the server's commit, which is this page's too
  for (const id of ["lValid", "sValid", "lCalls", "sCalls"]) $(id).hidden = !("spot_validation" in c);  // a SparkGap without call validation
}

function renderConns() {
  const box = $("conns");
  box.textContent = "";
  const cs = getComputedStyle(document.documentElement);
  app.instances.forEach((i, n) => {
    if (i.demo) return;  // demo is the toggle above, not a server
    const row = document.createElement("div");
    row.className = "conn" + (n === app.current ? " cur" : "");
    const failing = i.state !== "live" && !!i.error;
    const dot = document.createElement("span");
    dot.className = "sdot";
    dot.style.background = cs.getPropertyValue(i.state === "live" ? "--good" : i.state === "connecting" ? "--accent" : "--bad");
    const name = document.createElement("span"); name.className = "n"; name.textContent = i.title;
    const url = document.createElement("span"); url.className = "u" + (failing ? " bad" : "");
    url.textContent = failing ? i.error : shortUrl(i.url);
    row.append(dot, name, url);
    const edit = document.createElement("button"); edit.className = "btn small"; edit.textContent = "Edit";
    edit.onclick = e => { e.stopPropagation(); editConn(n); };
    const del = document.createElement("button"); del.className = "btn small"; del.textContent = "✕";
    del.onclick = e => { e.stopPropagation(); removeConn(n); };
    row.append(edit, del);
    row.onclick = () => setCurrent(n);
    box.appendChild(row);
  });
  if (!app.instances.some(i => !i.demo) && app.editing === -2) {
    const p = document.createElement("div"); p.className = "small muted"; p.textContent = "No servers yet."; box.appendChild(p);
  }
  if (app.note && app.editing === -2) {
    const p = document.createElement("div"); p.className = "small muted"; p.textContent = app.note; box.appendChild(p);
  }
  $("connForm").hidden = app.editing === -2;
  $("addConn").hidden = app.editing !== -2;
  $("fSave").textContent = $("fSave").disabled ? "Searching..." : app.editing === -1 ? "Add" : "Save";
}

function editConn(n) {
  const i = app.instances[n];
  // a new server defaults to the SparkGap that served this page, if one did
  const served = /^https?:$/.test(location.protocol);
  const [address, port] = i ? urlParts(i.url) : [served ? location.hostname : "", ""];  // no port: find them all
  $("fAddr").value = address; $("fPort").value = port; $("fCode").value = i ? i.token : "";
  $("fErr").hidden = true;
  app.editing = n;
  app.note = "";
  renderConns();
  $("fAddr").focus();
}

async function findConns() {
  const address = $("fAddr").value.trim(), code = normalizeCode($("fCode").value);
  const err = checkFeed(address, DEFAULT_PORT, code);
  if (err) { $("fErr").textContent = err; $("fErr").hidden = false; return; }
  $("fErr").hidden = true;
  $("fSave").disabled = true;
  $("fSave").textContent = "Searching...";
  const found = await Promise.all(SCAN_PORTS.map(p => probeFeed(feedUrl(address, p), code).then(r => [p, r])));
  $("fSave").disabled = false;
  if (app.editing !== -1) return;  // cancelled meanwhile
  const listed = app.instances.filter(i => urlParts(i.url)[0] === address).map(i => urlParts(i.url)[1]);
  const r = scanReport(found, listed);
  if (!r.add.length) { $("fErr").textContent = r.text; $("fErr").hidden = false; renderConns(); return; }
  for (const p of r.add) addInstance(feedUrl(address, p), code);
  app.editing = -2;
  app.note = r.text;
  saveFeeds();
  setCurrent(app.instances.length - r.add.length);
}

function saveConn() {
  if (app.editing === -1 && !$("fPort").value.trim()) { findConns(); return; }
  const address = $("fAddr").value, port = parseInt($("fPort").value) || 0, code = $("fCode").value;
  let err = checkFeed(address, port, code);
  const url = feedUrl(address, port), n = app.editing;
  if (!err && app.instances.some((i, k) => k !== n && i.url === url)) err = "already in the list";
  if (err) { $("fErr").textContent = err; $("fErr").hidden = false; return; }
  let at;
  if (n >= 0) { app.instances.splice(n, 1)[0].close(); at = n; } else at = app.instances.length;
  addInstance(url, normalizeCode(code), at);
  app.editing = -2;
  saveFeeds();
  setCurrent(at);
}

function removeConn(n) {
  app.instances.splice(n, 1)[0].close();
  if (app.current >= app.instances.length || app.current > n) app.current = Math.max(0, app.current - 1);
  app.editing = -2;
  saveFeeds();
  setCurrent(app.current);
}

function openSettings() { renderSettings(); renderConns(); $("modal").hidden = false; }
function closeSettings() { $("modal").hidden = true; app.editing = -2; }

// ---- input ----

function bindInput() {
  $("gear").onclick = () => ($("modal").hidden ? openSettings() : closeSettings());
  $("statusBtn").onclick = () => { renderStatusPage(); $("statusModal").hidden = !$("statusModal").hidden; };
  $("statusClose").onclick = () => { $("statusModal").hidden = true; };
  $("statusModal").onmousedown = e => { if (e.target === $("statusModal")) $("statusModal").hidden = true; };
  $("close").onclick = closeSettings;
  $("modal").onmousedown = e => { if (e.target === $("modal")) closeSettings(); };
  $("addConn").onclick = () => editConn(-1);
  $("fSave").onclick = saveConn;
  $("fCancel").onclick = () => { app.editing = -2; renderConns(); };
  for (const id of ["fAddr", "fPort", "fCode"]) $(id).onkeydown = e => { if (e.key === "Enter") saveConn(); };
  $("theme").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

  const search = $("search"), list = $("matches");
  const showMatches = () => {
    const i = inst();
    app.matches = i ? i.search(search.value) : [];
    list.innerHTML = app.matches.map((m, n) => `<div class="match${n ? "" : " first"}" data-n="${n}">`
      + `<span class="cd" style="width:6px;height:6px;border-radius:50%;border:1px solid var(${m.spot ? "--accent" : "--faint"});background:${m.spot ? "var(--accent)" : "transparent"}"></span>`
      + `<span class="mono" style="width:90px;font-weight:600">${esc(m.label)}</span>`
      + `<span class="mono muted" style="width:80px">${m.call ? m.khz.toFixed(1) : ""}</span>`
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
    else if (e.key === "Escape") $("statusModal").hidden = true;
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
      drag = { pinch: Math.abs(ys[0] - ys[1]) || 1, lo: wf.lo, hi: wf.hi, at: wf.khzAt((ys[0] + ys[1]) / 2 - r.top), moved: true };
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
    if (e.ctrlKey) wf.zoom(Math.pow(0.8, -px / 100), wf.khzAt(e.clientY - box.getBoundingClientRect().top));
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

  $("span").oninput = e => { const mid = (wf.lo + wf.hi) / 2, v = Number(e.target.value); wf.setView(mid - v / 2, mid + v / 2); };
  $("full").onclick = () => {  // the next step: Full, Half, Quarter of the band around the view's centre
    const i = inst();
    if (!i || !i.nBins) return;
    const k = zoomStep(i), d = ZOOM_STEPS[k < 0 ? 0 : (k + 1) % ZOOM_STEPS.length][1];
    const span = (i.highKhz - i.lowKhz) / d, mid = d === 1 ? (i.lowKhz + i.highKhz) / 2 : (wf.lo + wf.hi) / 2;
    wf.setView(mid - span / 2, mid + span / 2);
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
  // no saved servers yet: start in demo
  loadServers();
  setDemo(load("demo", !app.instances.length));
  setCurrent(app.current);
  if (!app.instances.length) { openSettings(); editConn(-1); }
  setInterval(() => { $("utc").textContent = "UTC: " + new Date().toISOString().substr(11, 8); }, 250);
  setInterval(() => { renderLadder(); }, 1000);
  // instances not on screen still move their rows into history, so switching shows their recent waterfall
  setInterval(() => app.instances.forEach((i, n) => { if (n !== app.current) i.advance(); }), 50);
  const frame = () => { const i = inst(); wf.frame(i ? i.heardList : []); requestAnimationFrame(frame); };
  requestAnimationFrame(frame);
}

start();
