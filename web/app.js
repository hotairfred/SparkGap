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
const VALIDATION = ["off", "permissive", "normal", "aggressive"];
const CALL_SOURCES = [["scp", "SCP"], ["rbn", "RBN"], ["rbn+skcc", "RBN + SKCC"]];
// Decoder Options: [config key, label, hint, values ([value, label]; none: on/off)]; null is off
const DECODER_OPTIONS = [
  ["spot_rule_split_runs", "Split runs", "Words sent with no gap are split before the rule reads them: CQCQDE becomes CQ CQ DE (hand-sent CW)."],
  ["spot_rule_contest_words", "Contest words", "CWT, SST or MST right before a call opens a CQ group as CQ does: runners who send CWT K5AX without the CQ."],
  ["spot_rule_cq_repeats", "CQ repeats", "Back-to-back CQs from one call count as separate copies, so a runner is spotted sooner."],
  ["spot_rule_doubled_calls", "Doubled calls", "A call sent twice in one CQ (CQ K8TLJ K8TLJ) counts as two copies. Used only while call validation is off; validation scores it itself."],
  ["spot_rule_split_up", "Split up", "TU <call> UP, a station working split, counts as a copy of that call."],
  ["spot_rule_runner_signs", "Runner signs", "Split DX that never sends CQ: KH8A UP or TU UP KH8A makes KH8A the runner of that frequency, and each window there holding KH8A counts as a copy. A call sent a report there (JH7ME 5NN) never counts."],
  ["spot_rule_window_join", "Window join", "A CQ group cut by the window edge, CQ CQ DE at the end of one window and the call at the start of the next, is read as one: the tail of the previous window from its CQ is read in front of the next window of the same signal."],
  ["spot_rule_garbled_runner", "Garbled runner", "A call right after a CQ word and a token that reads as a broken call (CQ POTAQ0SN K5LG) is the caller of that runner, not a CQ copy; a misread of the call after it (CQ K?9X KG9X) still is."],
  ["spot_rule_stray_known", "Stray, copied", "A 4-character call followed by one letter that completes a call copied near here in the last 10 minutes (DE K1PU G after DE K1PUG) is that longer call split at a gap, not a copy of the shorter one."],
  ["spot_rule_owner_cell", "Owner cell", "A new owner replaces only the owner within 0.25 kHz: a neighbour 0.5 kHz away keeps its frequency."],
  ["spot_rule_owned_two_windows", "Owned: two windows", "A call challenging a frequency another call owns needs copies in two windows."],
  ["spot_rule_owner_listed", "Owner listed", "One copy owns an empty frequency only when its call is on the known call list; a call with a prosign glued on (WB0MQAK) never owns one."],
  ["spot_rule_la1d", "Noise letters", "A lone E, T, I or S between CQ and the call is skipped, and a sign-off (K AR KN SK BK TU) ends the search: CQ CQ E E HFT ON6FT names ON6FT."],
  ["spot_rule_gluek", "Glued K", "CALL+K reads as CALL K when the call is also heard on its own (WB0MQA ... WB0MQAK)."],
  ["spot_rule_norival", "No unlisted rival", "Validation: an unlisted call read on the other path does not count against a listed one."],
  ["itila2_snr_decoder", "Decoder SNR", "Spot SNR from the decoded window's key-down power, in 500 Hz as CW Skimmer measures it."],
  ["spot_rule_fragments", "Fragments", "A call that is part of a longer call heard with it is not spotted: even, when the longer one is read as often; any, when it is read at all. Stray: TEST K1V U T is no copy of K1V.",
    [[null, "off"], ["any", "any"], ["any,stray", "any + stray"], ["even", "even"], ["even,stray", "even + stray"]]],
  ["spot_rule_takeover_idle_s", "Takeover", "Another call may take an owned frequency once its owner has been quiet this long.",
    [[null, "off"], [120, "120 s"], [180, "180 s"], [300, "300 s"]]],
  ["spot_rule_strongest_db", "Strongest copy", "No spot when the same call is read this many dB stronger elsewhere in the band.",
    [[null, "off"], [3, "3 dB"], [6, "6 dB"], [10, "10 dB"]]],
];
const RX_KEYS = ["antenna", "loop_a", "loop_b", "rf_gain"];  // a Flex copy's receive path: the radio's, changed live
const SHARED_NOTE = "Loops are shared by every pan on that antenna, and RF gain by every pan on the same receiver (SCU): "
  + "a change here changes them for every copy and program using it.";
// one sentence under each option whose name does not say what it does (the full text is on hover)
const OPTION_HINTS = {
  afc: "Each signal's decoder follows its tone as it drifts.",
  itila_window_sec: "How much audio each decoded window holds; shorter shows text sooner and costs more CPU.",
  spot_rule_split_runs: "Splits words sent with no gap: CQCQDE reads as CQ CQ DE.",
  spot_rule_contest_words: "Treats CWT, SST or MST before a call like a CQ.",
  spot_rule_cq_repeats: "Counts back-to-back CQs as separate copies, so a runner is spotted sooner.",
  spot_rule_doubled_calls: "Counts a call sent twice in one CQ as two copies (only while validation is off).",
  spot_rule_split_up: "Counts TU <call> UP from a station working split as a copy of that call.",
  spot_rule_runner_signs: "Takes split DX that never sends CQ (KH8A UP) as the runner of its frequency.",
  spot_rule_window_join: "Reads a CQ cut by a window edge together with the call at the start of the next window.",
  spot_rule_garbled_runner: "Takes the call after a misread runner call (CQ POTAQ0SN K5LG) as a caller.",
  spot_rule_stray_known: "Reads DE K1PU G after a copy of K1PUG as K1PUG, not K1PU.",
  spot_rule_owner_cell: "A new owner replaces only the owner within 0.25 kHz, never a neighbour.",
  spot_rule_owned_two_windows: "A call taking over a frequency another call owns needs copies in two windows.",
  spot_rule_owner_listed: "One copy owns an empty frequency only when its call is on the known call list.",
  spot_rule_la1d: "Skips a lone E, T, I or S between CQ and the call; a sign-off ends the search.",
  spot_rule_gluek: "Reads CALL+K as CALL K when the call is also heard on its own.",
  spot_rule_norival: "An unlisted call read on the other decoder path does not count against a listed one.",
  itila2_snr_decoder: "Measures spot SNR as CW Skimmer does: key-down power in 500 Hz.",
  spot_rule_fragments: "Skips a call that is part of a longer call heard with it.",
  spot_rule_takeover_idle_s: "Lets another call take a frequency once its owner has been quiet this long.",
  spot_rule_strongest_db: "Skips a spot when the same call is read much stronger elsewhere in the band.",
};

const HINTS = {
  window: "How much signal each decoded window holds (itila_window_sec). Shorter windows show text sooner but fit each signal's timing on less of it and cost more CPU; 15 and 7.5 s are untried. Applying restarts the skimmer.",
  record: "Saves the raw signal to disk for replay; needs 5 GB free. Restarts the skimmer.",
  afc: "Each signal's decoder follows its keyed tone, which helps weak and slightly off-tune signals (itila2_afc). Applying restarts the skimmer.",
  validation: "A confidence score decides which calls are spotted: permissive spots more, aggressive fewer (docs/ITILA2.md). Off: a set number of copies, 2 to 4 by how common the call's pattern is.",
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
// the decode window: 30 s is what the test seats run; 15 and 7.5 s are untried
const WINDOWS = [[60, "60 s"], [30, "30 s"], [15, "15 s"], [7.5, "7.5 s"]];
  // the dialog's header; its ? button shows help
const NEWS_SHOWN = 4;  // What's new entries shown in the dialog; the guide has them all
const NOTHING = '<span class="nothing">(nothing decoded)</span>';  // a path whose window decoded no text
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
const RUNNER_KEEP_S = 600;   // a split runner found by its sign labels its frequency this long (the spot rule's REPEAT_S)
const NOISE_LETTERS = new Set(["E", "T", "I", "S", "?"]);

// A split runner's call in one window: KH8A UP or TU UP KH8A (stray letters after UP allowed),
// never a call sent a report (JH7ME 5NN). As spot_rule.runner_signs_of (cdub89/SparkGap#37).
function runnerSign(text) {
  const toks = text.split(/\s+/).filter(Boolean);
  const worked = new Set(toks.filter((t, i) => /^[5E][9N][9N]$/.test(toks[i + 1] || "")));
  for (let i = 0; i < toks.length; i++) {
    const t = toks[i];
    if (!CALL_RE.test(baseCall(t)) || worked.has(t)) continue;
    const before = toks.slice(Math.max(0, i - 4), i).filter(x => !NOISE_LETTERS.has(x));
    if (toks[i + 1] === "UP" || before.slice(-2).join(" ") === "TU UP") return baseCall(t);
  }
  return "";
}

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
    this.runners = new Map();  // khz -> {call, utc}: a split runner found by its sign (runnerSign)
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
      this.subscribe();
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
      // a hello mid-connection with other bands: a live band change, so the picked signal is gone
      this.moved = !!this.hello.type && JSON.stringify(this.hello.bands) !== JSON.stringify(m.bands);
      if (this.moved) { this.selKhz = null; this.selCall = null; this.lastPick = null; }
      this.hello = m;
      if (!this.bands[this.band]) this.band = 0;
      this.subscribe();  // now the band on screen is known: only its spectrum is computed and sent
      this.windows = []; this.spots.clear(); this.signals.clear(); this.recent.clear(); this.runners.clear();
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
    const runner = runnerSign(text);
    if (runner) this.runners.set(khz, { call: runner, utc: now() });
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
    for (const [k, r] of this.runners) if (t - r.utc > RUNNER_KEEP_S) this.runners.delete(k);
    if (changed) this.on("ladder");
  }

  matchesSelection(khz) { return this.selKhz !== null && Math.abs(khz - this.selKhz) <= FREQ_MATCH_KHZ; }

  // host and band from hello; empty until the first hello. No address: that is for Settings.
  get bands() { return this.hello.bands || []; }
  bandAt(khz) { return this.bands.findIndex(b => Math.abs(khz - b.center_khz) <= b.span_khz / 2); }
  // the row rate, and the band whose waterfall is on screen (before the hello: every band)
  subscribe() {
    const b = this.bands[this.band];
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "subscribe", spectrum_hz: SPECTRUM_HZ, ...(b ? { band_khz: b.center_khz } : {}) }));
  }
  setBand(n) {
    if (n === this.band || !this.bands[n]) return;
    this.band = n;
    this.subscribe();
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

  // a split runner found near khz by its sign (KH8A UP), else the call-shaped word seen most
  // often (at least twice) in khz's recent windows: a split DX sends each caller's call twice
  guessCall(khz) {
    const t = now();
    for (const [k, r] of this.runners) {
      if (Math.abs(k - khz) <= DETAIL_MATCH_KHZ && t - r.utc <= RUNNER_KEEP_S) return r.call;
    }
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
      html: this.marked(w.text, call) || NOTHING, cost: w.cost, rate: w.rate,
      altHtml: w.alt ? this.marked(w.alt.text, call) || NOTHING : "", altCost: w.alt ? w.alt.cost : null, altRate: w.alt ? w.alt.rate : null,
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
      const mid = (inst.lowKhz + inst.highKhz) / 2, span = Number(load(zoomKey(inst), 30)) || 30;  // the copy's last zoom
      this.setView(mid - span / 2, mid + span / 2);
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
    if (inst && inst.nBins && Math.abs((hi - lo) - (this.hi - this.lo)) > 1e-6) save(zoomKey(inst), hi - lo);
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
  codeShown: false,  // join codes in clear (masked by default: screenshots carry them)
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
      dropPending(this.server.address, name => !names.has(name) && !name.startsWith("+add:"));
      serverCopies(this.server); renderSettings(); renderState();
    }
    else if (m.type === "server_result" && this.waiting.has(m.id)) { this.waiting.get(m.id)(m); this.waiting.delete(m.id); }
    else if (m.type === "log" && Array.isArray(m.lines)) {
      this.log = (m.reset ? [] : this.log || []).concat(m.lines).slice(-SERVER_LOG_KEPT);
      renderSettings();
    }
  }
  request(cmd, name, settings) {
    return new Promise(resolve => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) { resolve({ ok: false, error: "server not connected" }); return; }
      const id = this.next++;
      this.waiting.set(id, resolve);
      this.ws.send(JSON.stringify({ type: "server", id, cmd, name, by: "SparkGap Web", ...(settings ? { settings } : {}) }));
    });
  }
}

// an server's running copies, each opened through it with the server's code
function serverCopies(s) {
  const urls = (s.link.seat.instances || []).filter(i => i.state === "running" && i.feed_port)
    .map(i => serverUrl(s.address, "/feed/" + encodeURIComponent(i.name)));
  reconcileUrls(s, urls);
}
// where a copy's last zoom is kept in this browser: its server and name, else its feed address
function zoomKey(i) { return "zoom." + (copyName(i) ? `${i.server.address}|${copyName(i)}` : i.url); }

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

// the radio reports a different receive path for copy i (from this page, SmartSDR, Maestro or another
// copy on the same SCU).  An open list in the dialog is left alone: the next status catches up
function rxMoved(i) {
  const r = i.status && i.status.radio && i.status.radio.rx, now = r ? JSON.stringify([r.rxant, r.rfgain, r.loopa, r.loopb]) : "";
  if (now === i.rxSeen) return false;
  const f = document.activeElement;
  if (f && f.tagName === "SELECT" && $("modal").contains(f)) return false;
  i.rxSeen = now;
  return true;
}

function changed(i, what) {
  // a copy that comes back on another port is followed by name, but never away from a live one
  if (what === "hello" && i.name === app.want && i !== inst() && !(inst() && inst().state === "live")) {
    setCurrent(app.instances.indexOf(i)); return;
  }
  if (what === "hello" && i === inst() && !app.want) { app.want = i.name; save("current", i.name); }
  if (what === "state") { renderConns(); if (i === inst()) renderState(); return; }
  if (what === "status" && rxMoved(i)) renderSettings();  // any copy, set from anywhere
  if (i !== inst()) { if (what === "hello") { renderConns(); renderBands(); } return; }
  if (what === "hello" && i.moved) { i.moved = false; app.selectedKhz = -1; wf.sel = -1; }
  if (what === "hello") { wf.fit(); renderBands(); renderState(); renderStation(); renderSettings(); renderConns(); renderLadder(); }
  else if (what === "status") { renderStatus(); renderSettings(); }
  else if (what === "ladder") renderLadder();
  else if (what === "detail") { renderDetail(); renderStatus(); }
}

function setCurrent(n) {
  // a copy that covers the picked frequency keeps it and the view, to compare copies on one signal;
  // otherwise each copy comes back on the signal last picked on it
  const prev = inst();
  if (prev) prev.lastPick = app.selectedKhz > 0 ? { khz: app.selectedKhz, lo: wf.lo, hi: wf.hi } : null;
  let khz = app.selectedKhz, lo = wf.lo, hi = wf.hi;
  app.current = Math.max(0, Math.min(n, app.instances.length - 1));
  const i = inst();
  const covers = k => k > 0 && i && (!i.nBins || (i.lowKhz <= k && k <= i.highKhz));
  if (i && i.lastPick && (!i.nBins || !covers(khz)) && covers(i.lastPick.khz)) ({ khz, lo, hi } = i.lastPick);  // not drawn yet: its own pick
  if (i && i.hello.run) { app.want = i.name; save("current", i.name); }
  app.selectedKhz = -1;
  wf.sel = -1;
  const keep = covers(khz);
  if (keep && !i.nBins) i.goKhz = khz;  // not drawn yet: centre on it once it is
  wf.setInstance(i);
  if (keep && i.nBins && i.lowKhz <= lo && hi <= i.highKhz) wf.setView(lo, hi);
  if (keep) pick(khz);
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
  // a live feed from a receiver whose radio is down or reconnecting says so
  const seat = i.server && i.server.link && i.server.link.seat;
  const rx = seat && (seat.instances || []).find(x => x.name === copyName(i));
  const radio = i.state === "live" && rx ? radioLink(seat, rx.radio) : "";
  const look = radio === "down" ? "bad" : radio === "wait" || radio === "first" ? "wait" : i.state;
  dot.style.background = cs.getPropertyValue(look === "live" ? "--good" : look === "connecting" || look === "wait" ? "--accent" : "--bad");
  const word = radio === "down" ? "Radio not connected" : radio === "wait" ? "Reconnecting to radio" : radio === "first" ? "Connecting to radio"
    : { live: "Live", connecting: "Connecting", refused: "Refused" }[i.state] || "Reconnecting";
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
// a copy's six headline boxes: uptime, spots, calls, windows, bins, CPU (dashes while stopped)
// all: an HPSDR receiver's pane: its radio's totals and their label, under each count
function statusTilesHtml(s, all) {
  const num = v => (v === null || v === undefined ? "-" : Number(v).toLocaleString("en-US"));
  const of = k => (all && all[k] !== null && all[k] !== undefined ? `of ${num(all[k])}, ${all.label}` : "");
  const tile = (k, v, sub, bad, primary) => `<div class="stat${bad ? " bad" : ""}${primary ? " primary" : ""}"><div class="label">${k}</div><div class="mono">${esc(dash(v))}</div>`
    + `${sub ? `<div class="tsub">${esc(sub)}</div>` : ""}</div>`;
  const tiles = [tile("UPTIME", s.uptime_s === undefined ? null : duration(s.uptime_s), all ? all.label : ""),
    tile("SPOTS", num(s.spots), of("spots") || (s.spots_hour !== undefined ? `${s.spots_hour} last hour` : ""), false, true),
    tile("CALLS", num(s.calls), of("calls"), false, true), tile("WINDOWS", num(s.windows), of("windows"), false, true),
    tile("BINS", num(s.bins), of("bins") || (s.bins_peak !== undefined ? `peak ${s.bins_peak}` : "")),
    tile("CPU", s.cpu !== undefined && s.cpu !== null ? s.cpu + "%" : "-", "of a core" + (s.rss_mb ? ` · ${s.rss_mb} MB` : "") + (all ? `, ${all.label}` : ""))];
  return `<div class="stats stiles">${tiles.join("")}</div>`;
}

// tiles: false leaves out the headline boxes (the Radios view shows them above)
function statusHtml(i, a, tiles = true) {
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

  const lossy = r.packets > 0 && r.lost / r.packets > LOST_WARN;
  const radio = [row("Type", r.type === "flex" ? "FlexRadio" : r.type === "hpsdr" ? "HPSDR" : r.type === "file" ? "File replay" : r.type, { ref: true }),
    r.type === "file" ? fileRow("File", r.address, "-", { ref: true }) : row("Address", r.address, { mono: true, ref: true })];
  if ("station" in r) radio.push(row("Station", r.station, { ref: true }));
  if ("slice" in r) radio.push(row("Slice", r.slice === null ? null : r.slice + (r.slice_letter ? ` (${r.slice_letter})` : ""), { mono: true, ref: true }));
  if (r.rx) {
    const parts = [`ANT = ${r.rx.rxant || "-"}`, `RF gain = ${typeof r.rx.rfgain === "number" ? r.rx.rfgain : "-"}`];
    [["loopa", "Loop A"], ["loopb", "Loop B"]].forEach(([k, label]) => {
      if (typeof r.rx[k] === "boolean") parts.push(`${label} = ${r.rx[k] ? "on" : "off"}`);
    });
    radio.push(row("Antenna", parts.join(", "), { mono: true, key: true }));
  }
  const mhz = khz => (khz / 1000).toFixed(3);
  (h.bands || []).forEach((b, n) => {
    const tag = h.bands.length > 1 ? ` ${bandLabel(b)}` : "", d = (s.decode_khz || [])[n];
    const dax = "daxiq_channel" in r;
    radio.push(row((dax ? "DAX-IQ" : "IQ") + tag, `${dax ? `IQ = ${r.daxiq_channel}, ` : ""}centre = ${mhz(b.center_khz)} MHz, ${b.span_khz} kHz`, { mono: true, ref: true }));
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
    ...(s.clients || []).map(c => row(c.name || c.peer, `${num(c.dropped)} dropped`, { mono: true, sub: `${c.name ? `${c.peer}, ` : ""}${c.via ? `via ${c.via}, ` : ""}since ${utcTime(c.since)}`, key: true, bad: c.dropped > 600 }))];

  const p = s.problems || {};
  const probs = [`<div class="srow"><span class="k">Since start</span><span class="v"><span class="mono${p.errors ? " bad" : ""}">${num(p.errors)}</span> errors, `
      + `<span class="mono">${num(p.warnings || 0)}</span> warnings</span></div>`,
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
  for (const c of s.clients || []) if (c.dropped > 600) issues.push(["warn", `Console ${c.name || c.peer} falling behind`]);
  const worst = issues.some(x => x[0] === "bad") ? "bad" : issues.length ? "warn" : "ok";
  const banner = `<div class="health ${worst}">${issues.length ? issues.map(([lv, t]) => `<span class="${lv}">${esc(t)}</span>`).join("")
    : "<span>Healthy: radio live, decoding keeping up, no errors</span>"}</div>`;
  const tagRadio = r.state ? pill(String(r.state).toUpperCase(), live ? (r.reconnects || lossy ? "warnlvl" : "ok") : "warn") : "";
  const tagSg = s.clock_synced === false ? pill("CLOCK OFF", "warn") : s.clock_synced ? pill("CLOCK OK", "ok") : "";
  const tagProb = p.errors ? pill(`${num(p.errors)} ERRORS`, "warn") : pill("NONE", "ok");

  return head + banner + (tiles ? statusTilesHtml(s) : "") + `<div class="scards">`
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

function costText(v) { return v === null || v === undefined ? "--" : v >= NO_FIT ? "no fit" : v.toFixed(2); }
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
    // with both shown, the lower timing cost is marked (a tie or no fit marks neither)
    const k = c => (c === null || c === undefined || c >= NO_FIT ? Infinity : c);
    const best = rs.length > 1 && k(rs[0].cost) !== k(rs[1].cost) ? (k(rs[0].cost) < k(rs[1].cost) ? 0 : 1) : -1;
    return `<div class="win"><span class="t">${r.utc}</span><div class="x">`
      // the lower cost bright and bold, the higher at 75%, in either place; on a tie the second is dimmed
      + rs.map((x, n) => `<span class="${best < 0 ? (n ? "alt" : "") : n === best ? "best" : "worse"}">${x.html}</span>`).join("")
      + `<span class="c">${rs.length > 1 || r.altHtml ? rs.map(x => `${rateName(x.rate)} ${costText(x.cost)}`).join("  ·  ") : "cost " + costText(r.cost)}</span>`
      + "</div></div>";
  }).join("");
}

function renderStatus() {
  const i = inst(), s = i ? i.status : {}, up = s.uptime_s || 0;
  const rec = i && s && s.recording;  // the server is writing IQ: REC in the header
  $("rec").hidden = !rec;
  $("rec").title = rec ? `Recording IQ to ${rec.file}` : "";
  const items = [["Load", s.cpu !== undefined && s.cpu !== null ? s.cpu + "%" : "-"], ["Memory", s.rss_mb ? s.rss_mb + " MB" : "-"],
    ["Bins", s.bins !== undefined ? s.bins : "-"], ["Spots", s.spots !== undefined ? s.spots : "-"],
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

function renderSettings() {
  if ($("modal").hidden) return;
  if (app.pressed) { app.renderDue = true; return; }  // rebuilt between press and release, a click is lost
  const help = app.mode === "help";
  $("helpBtn").classList.toggle("on", help);
  $("helpBody").hidden = !help;
  $("radiosBody").hidden = help;
  $("radioHead").hidden = help;
  document.querySelector(".dialog.settings").classList.toggle("twopane", !help);
  renderFootLine();
  renderConnForm();
  if (help) { renderHelp(); return; }
  const box = $("radiosBody"), focus = document.activeElement;
  if (selecting(box) || (focus && (box.contains(focus) || $("radioHead").contains(focus)) && (focus.tagName === "SELECT" || focus.tagName === "INPUT"))) return;
  // the log keeps its place: at the bottom it follows new lines, scrolled up it stays
  const was = {};
  for (const c of box.querySelectorAll("[data-console]")) was[c.dataset.console] = c.scrollHeight - c.scrollTop - c.clientHeight < 4 ? -1 : c.scrollTop;
  box.innerHTML = radiosHtml();
  for (const c of box.querySelectorAll("[data-console]")) {
    const at = was[c.dataset.console];
    c.scrollTop = at === undefined || at < 0 ? c.scrollHeight : at;
  }
}

// a server's join code: masked until clicked; Copy puts it on the clipboard
function joinCodeRow(sv) {
  const code = String(sv.token || "").replace(/[^0-9A-Za-z]/g, "").toUpperCase();
  if (!code) return '<div class="srow"><span class="k">Join code</span><span class="v mono">-</span></div>';
  return `<div class="srow"><span class="k">Join code</span><span class="v"><span class="mono act" data-act="code" title="${app.codeShown ? "Click to hide" : "Click to show"}">`
    + `${esc(app.codeShown ? `${code.slice(0, 3)}-${code.slice(3)}` : "•••-•••")}</span> `
    + `<button class="btn small" data-act="copycode" data-code="${esc(`${code.slice(0, 3)}-${code.slice(3)}`)}">Copy</button></span></div>`;
}

// Info on a stopped copy's tab: what its server knows
function stoppedInfoHtml(a) {
  if (!a) return "";
  // the running layout's Radio and SparkGap cards, with what the server knows and the rest empty
  const row = (k, v) => `<div class="srow"><span class="k">${esc(k)}</span><span class="v">${esc(dash(v))}</span></div>`;
  const card = (title, rows) => `<section class="scard"><h3><span>${title}</span></h3>${rows.join("")}</section>`;
  const flex = !!a.radio;
  const radio = [row("Type", flex ? "FlexRadio" : null), row("Address", a.radio),
    ...(flex ? [row("Station", null), row("Slice", null), row("Antenna", null)] : []),
    row(flex ? "DAX-IQ" : "IQ", null), row("Decoding", null), row("Packets", null), row("Lost", null), row("Reconnects", null),
    ...(flex ? [row("Other stations", null)] : [])];
  const sg = [row("Host", null), row("Build", a.build), row("Clock", null), row("Decoder", null), row("Bands", a.band),
    row("Config", null), row("Log", a.log), row("Recording", null), row("IQ trimmed", null)];
  return `<div class="infohead"><span class="sname">${esc(a.name)}</span><span class="pill warn">${esc(String(a.busy || a.state).toUpperCase())}</span></div>`
    + (a.last_error ? `<div class="small err">${esc(a.last_error)}</div>` : "")
    + `<div class="scards">${card("RADIO", radio)}${card("SPARKGAP", sg)}</div>`;
}

// ---- Radios: two panes; the radio and its receivers on the left, the one picked on the right ----

const dot = state => `<span class="dot ${state === "running" ? "run" : state === "stopped" ? "stop" : "busy"}"></span>`;

// a radio's RBN upload as the node line shows it
const upMode = m => ({ off: "Off", dry: "Dry", live: "Live" }[m || "off"] || String(m));
// the feeder's state alone, as sparkgap.sh status shows it: off, signed in, or what is wrong
function feederState(f, mode) {
  if (!mode || mode === "off") return "off";
  if (!f) return '<span class="bad">not running</span>';
  const errs = Number(f.errors) || 0;
  if (errs) return `<span class="bad">${errs} error${errs > 1 ? "s" : ""}</span>`;
  return f.registered ? '<span class="good">signed in</span>' : '<span class="bad">not signed in</span>';
}

// an HPSDR radio's one process, for all its receivers, as sparkgap_ctl's skimmer row
function skimmerText(i, utc) {
  const run = i.state === "running", s = run && i.started ? Math.max(0, Math.floor((utc || Date.now() / 1000) - i.started)) : null;
  const up = s === null ? "" : s >= 3600 ? `up ${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m` : `up ${Math.floor(s / 60)}m ${s % 60}s`;
  const err = Number(i.errors) || 0, warn = Number(i.warnings) || 0;
  return [dot(i.busy ? "busy" : i.state) + esc(i.busy ? `${i.busy}...` : i.state), esc(up), run && i.cpu != null ? `cpu ${i.cpu}%` : "",
    run && i.rss_mb != null ? `${i.rss_mb} MB` : "", err ? `<span class="bad">err ${err}</span>` : "err 0",
    warn ? `<span class="warn">warn ${warn}</span>` : "warn 0", esc(i.build || "")].filter(Boolean).join(" · ");
}

// what the right pane shows: {k, kind: "rx"|"rbn"|"node"|"server"|"browser", name}; the receiver on screen first
function radioPick() {
  const servers = app.servers.filter(sv => sv.link && sv.link.seat);
  const all = servers.flatMap(sv => (sv.link.seat.instances || []).map(i => ({ k: app.servers.indexOf(sv), name: i.name })));
  const p = app.pick;
  if (p && p.kind === "rx" && p.rx) {  // an HPSDR receiver removed (elsewhere too): its radio's first
    const i = ((app.servers[p.k] && app.servers[p.k].link && app.servers[p.k].link.seat) || {}).instances?.find(x => x.name === p.name);
    if (i && !(i.receiver_list || []).some(r => r.receiver === p.rx)) p.rx = 1;
  }
  if (p && (p.kind !== "rx" || all.some(x => x.k === p.k && x.name === p.name))) return p;
  const i = inst(), here = i && all.find(x => app.servers[x.k] === i.server && x.name === copyName(i));
  return app.pick = here ? { ...here, kind: "rx" } : all[0] ? { ...all[0], kind: "rx" } : { k: app.servers.indexOf(servers[0]), kind: "server" };
}

function navItem(k, kind, name, label, value, on) {
  return `<button class="navitem${on ? " on" : ""}" data-act="pick" data-server="${k}" data-kind="${kind}" data-pick="${esc(name || "")}">`
    + `<span>${label}</span><span class="navval">${value}</span><span class="chev">›</span></button>`;
}

// decoder options in place: the same controls and Apply in a receiver's pane and in Add a receiver
function decoderOptions(at, st, pend) {
  const row = (label, control) => `<div class="frow"><span class="flabel">${label}</span><span class="fctl">${control}</span></div>`;
  const valid = st.validation || "off", win = st.itila_window_sec === undefined || st.itila_window_sec === null ? 60 : st.itila_window_sec;
  const ons = DECODER_OPTIONS.filter(([key, , , values]) => values ? st[key] !== null && st[key] !== undefined : !!st[key]).length + (st.afc ? 1 : 0);
  const choose = (key, opts, cur, off) => `<div class="seg${key in pend ? " pend" : ""}${off ? " off" : ""}" ${at} data-key="${key}"${off ? ` title="${esc(off)}"` : ""}>${opts.map(([v, l]) =>
    `<span${off ? "" : ` data-value="${esc(JSON.stringify(v))}"`}${v === cur ? ' class="on"' : ""}>${esc(l)}</span>`).join("")}</div>`;
  const hinted = (hint, html) => html.replace('<div class="frow"', `<div class="frow" title="${esc(hint)}"`);
  const short = key => OPTION_HINTS[key] ? `<div class="frow ohint"><span></span><span class="fhint">${esc(OPTION_HINTS[key])}</span></div>` : "";
  const decRows = [hinted(HINTS.validation, row("Call validation", choose("validation", VALIDATION.map(v => [v, v]), valid))),
    hinted(HINTS.call_source, row("Known call list", choose("call_source", CALL_SOURCES, st.call_source, valid === "off" ? "not used while call validation is off" : ""))),
    hinted(HINTS.window, row("Window", choose("itila_window_sec", WINDOWS, win))) + short("itila_window_sec"),
    hinted(HINTS.afc, row("AFC", choose("afc", [[false, "off"], [true, "on"]], !!st.afc, st.decoder && st.decoder !== "itila2" ? "acts only with the itila2 decoder" : ""))) + short("afc"),
    ...DECODER_OPTIONS.map(([key, label, hint, values]) => `<div class="frow" title="${esc(hint || "")}"><span class="flabel">${esc(label)}</span><span class="fctl">${
      values ? choose(key, values, st[key] === undefined ? null : st[key]) : choose(key, [[false, "off"], [true, "on"]], !!st[key])}</span></div>` + short(key))];
  return { decRows, win, valid, ons, choose };
}

// room on radio rn for another receiver: its free DAX-IQ channels, or why there is none
function addRoom(seat, rn) {
  const rf = (seat.radio_files || []).find(x => x.name === rn) || {};
  if (rf.type === "hpsdr") {  // one process: the next receiver, up to the radio's count
    const proc = (seat.instances || []).find(i => i.name === rn), n = ((proc && proc.receiver_list) || []).length + 1;
    const limit = Number.isInteger(rf.receivers) && rf.receivers > 0 ? rf.receivers : null;
    return limit !== null && n > limit ? { free: [], why: `the ${rf.model || rn} holds ${limit} receivers: all in use`, hpsdr: true, proc }
      : { free: [n], why: "", hpsdr: true, proc };
  }
  const mine = (seat.instances || []).filter(i => i.radio === rn);
  const used = new Set(mine.map(i => Number((/-(\d)$/.exec(i.name) || [])[1])));
  const caps = [rf.max_panadapters, rf.max_slices, rf.daxiq_channels].filter(v => Number.isInteger(v) && v > 0);
  const limit = caps.length ? Math.min(...caps) : null;
  const top = Number.isInteger(rf.daxiq_channels) && rf.daxiq_channels > 0 ? rf.daxiq_channels : 4;
  const free = Array.from({ length: top }, (_, n) => n + 1).filter(n => !used.has(n));
  const why = rf.type !== "flex" ? "only FlexRadio receivers are added here"
    : limit !== null && mine.length >= limit ? `the ${rf.model || rn} holds ${limit} receivers: all in use`
    : !free.length ? "every DAX-IQ channel is in use" : "";
  return { free, why };
}

function addItem(k, seat, rn, on) {
  const { why } = addRoom(seat, rn);
  return `<button class="navadd addsrv${on ? " on" : ""}" data-act="pick" data-server="${k}" data-kind="add" data-pick="${esc(rn)}"`
    + `${why ? ` disabled title="${esc(why)}"` : ""}>+ Add a receiver</button>`;
}

// Add a receiver: band and DAX-IQ channel, pending until Apply as other settings are
function addPaneHtml(k, sv, rn) {
  const seat = sv.link.seat || {}, { free, why, hpsdr, proc } = addRoom(seat, rn), bands = seat.bands || [];
  const head = `<div class="phead"><span class="ptitle">Add a receiver</span><span class="muted">${esc(rn)}</span></div>`;
  if (why) return head + `<div class="fhint">${esc(why)}</div>`;
  const d = app.addDraft && app.addDraft.radio === rn ? app.addDraft
    : (app.addDraft = { radio: rn, band: bands.includes("20m") ? "20m" : bands[0], channel: free[0] });
  if (!free.includes(d.channel)) d.channel = free[0];
  const name = `${rn}-${d.channel}`, at = `data-server="${k}" data-radio="${esc(rn)}"`;
  const back = (seat.removed || {})[name], pend = app.pending[pendKey(sv, addKey(rn))] || {}, st = { ...addBase(seat, rn), ...pend };
  const opt = `data-server="${k}" data-name="${esc(addKey(rn))}"`, { decRows, choose } = decoderOptions(opt, st, pend);
  const opts = (list, cur) => list.map(v => `<option value="${esc(String(v))}"${v === cur ? " selected" : ""}>${esc(String(v))}</option>`).join("");
  const row = (label, control, note = "") => `<div class="frow"><span class="flabel">${label}</span><span class="fctl">${control}${note}</span></div>`;
  return head + `<div class="fbar"><span>${esc(name)} not added yet</span><span class="grow"></span>`
    + `<button class="btn small primary" data-act="addapply" ${at}${app.adding ? " disabled" : ""}>Apply (adds ${esc(name)})</button>`
    + `<button class="btn small" data-act="addcancel" ${at}>Cancel</button></div>`
    + `<div class="form">${row("BAND", `<select class="fsel" data-add="band">${opts(bands, d.band)}</select>`, hpsdr ? `<span class="fnote">its name: ${esc(name)}</span>` : "")}`
    + (hpsdr ? `</div><div class="fhint">${proc ? `It joins ${esc(rn)}'s other receivers: their decoder options are its, and ${esc(rn)} restarts if it runs.` : `${esc(rn)} starts with it, from the shipped HPSDR defaults; change them here.`}</div>`
      + (proc ? "" : `<div class="fold"><div class="form">${decRows.join("")}</div></div>`) : "")
    + (hpsdr ? "" : row("DAX-IQ", `<select class="fsel" data-add="channel">${opts(free, d.channel)}</select>`, `<span class="fnote">its name: ${esc(name)}</span>`)
    + row("RECORDING", choose("record", [[false, "off"], [true, "on"]], !!st.record))
    + `</div><div class="fhint">It starts with the radio's antenna and RF gain, and ${back ? `the settings ${esc(name)} had before it was removed` : "the shipped default decoder options"}; change them here.</div>`
    + `<div class="fold"><div class="form">${decRows.join("")}</div></div>`);
}

// Reset to defaults: every decoder option that differs from the shipped defaults goes pending, for Apply
function resetDefaults(link, name) {
  const d = (link.seat && link.seat.default_settings) || {};
  for (const key of ["validation", "call_source", "itila_window_sec", "afc", ...DECODER_OPTIONS.map(([k]) => k)]) {
    if (key in d) pickSetting(link, name, key, d[key]);
  }
}

// what an added receiver starts with: the settings it had before it was removed, else the shipped defaults
function addBase(seat, rn) {
  const d = app.addDraft && app.addDraft.radio === rn ? app.addDraft : null;
  return (d && (seat.removed || {})[`${rn}-${d.channel}`]) || seat.default_settings || {};
}

// the pending settings of a receiver not added yet
function addKey(rn) { return `+add:${rn}`; }

async function addReceiver(sv, rn) {
  const d = app.addDraft;
  if (!d || d.radio !== rn || !d.channel) return;
  const name = `${rn}-${d.channel}`;
  app.adding = true; renderSettings();
  const k = pendKey(sv, addKey(rn));
  const ok = await seatRequest(sv.link, "add", name, { ...(app.pending[k] || {}), band: d.band });
  app.adding = false;
  const hp = addRoom(sv.link.seat || {}, rn).hpsdr;  // an HPSDR receiver: its radio's process, its number
  if (ok) { app.addDraft = null; delete app.pending[k]; app.pick = hp ? { k: app.servers.indexOf(sv), kind: "rx", name: rn, rx: d.channel } : { k: app.servers.indexOf(sv), kind: "rx", name }; }
  renderSettings();
}

// every radio on the connected servers, for the switch: {k, name, label}; name "" = receivers with no radio file
function radioList() {
  return app.servers.flatMap((sv, k) => {
    const seat = sv.link && sv.link.seat;
    if (!seat) return [];
    const files = seat.radio_files || [], list = seat.instances || [], all = [...(seat.radios || []), ...files.filter(f => !(seat.radios || []).some(x => x.name === f.name))];
    const shown = all.filter(r => list.some(i => i.radio === r.name) || files.some(f => f.name === r.name));
    const out = shown.map(r => ({ k, name: r.name, label: [r.name, r.model || r.type, r.callsign].filter(Boolean).join(" \u00b7 ") }));
    if (list.some(i => !shown.some(r => r.name === i.radio))) out.push({ k, name: "", label: "Receivers with no radio file" });
    return out;
  });
}

// the radio the switch shows: the one picked in this browser, else the picked receiver's, else the first
function radioSel(pick) {
  const all = radioList(), same = x => all.find(r => r.k === x.k && r.name === x.name);
  const saved = app.radioSel || load("radioSel", null);  // {address, name}: the server's address, not its place in the list
  const kept = saved && all.find(r => app.servers[r.k].address === saved.address && r.name === saved.name);
  if (kept) return kept;
  const sv = pick && app.servers[pick.k], i = pick && pick.kind === "rx" && sv && sv.link && ((sv.link.seat || {}).instances || []).find(x => x.name === pick.name);
  return (i && same({ k: pick.k, name: i.radio || "" })) || (pick && pick.kind !== "rx" && pick.name && same({ k: pick.k, name: pick.name })) || all[0] || null;
}

// what the right pane opens on for a radio: its first receiver, else its Add pane
function firstPick(x) {
  const named = radioList().filter(r => r.k === x.k && r.name).map(r => r.name);
  const first = (((app.servers[x.k].link || {}).seat || {}).instances || []).find(i => x.name ? i.radio === x.name : !named.includes(i.radio));
  return first ? { k: x.k, kind: "rx", name: first.name } : x.name ? { k: x.k, kind: "add", name: x.name } : { k: x.k, kind: "server" };
}

// a receiver of another radio than the switch shows gives way to that radio's first
function fitPick(pick) {
  if (radioList().length < 2 || pick.kind !== "rx") return pick;
  const sel = radioSel(pick), sv = app.servers[pick.k], i = sv && sv.link && ((sv.link.seat || {}).instances || []).find(x => x.name === pick.name);
  const named = radioList().filter(r => r.k === pick.k && r.name).map(r => r.name);
  const mine = i && sel && sel.k === pick.k && (sel.name ? i.radio === sel.name : !named.includes(i.radio));
  return mine || !sel ? pick : (app.pick = firstPick(sel));
}

function leftPaneHtml(pick) {
  const sel = radioList().length > 1 ? radioSel(pick) : null, only = (k, name) => !sel || (sel.k === k && sel.name === name);
  return app.servers.filter(sv => sv.link).map(sv => {
    const k = app.servers.indexOf(sv), seat = sv.link.seat || { instances: [], stations: [], radios: [] }, list = seat.instances || [];
    const is = (kind, name) => pick.k === k && pick.kind === kind && (name === undefined || pick.name === name);
    const rxItem = i => {
      if (i.receiver_list) return i.receiver_list.map(r => {  // an HPSDR radio: one card per receiver, one process
        const on = pick.k === k && pick.kind === "rx" && pick.name === i.name && (pick.rx || 1) === r.receiver;
        const run = i.state === "running", bins = run ? ((i.receivers || []).find(x => x.rx === r.receiver - 1) || {}).bins : null;
        const st = i.busy ? `${i.busy}...` : i.state, spots = run && r.spots !== null && r.spots !== undefined ? ` · ${r.spots} spots` : "";
        const open = bins !== null && bins !== undefined ? ` · ${bins} bins` : "";
        return `<button class="navrx${on ? " on" : ""}" data-act="pick" data-server="${k}" data-kind="rx" data-pick="${esc(i.name)}" data-rx="${r.receiver}">`
          + `<span class="rxtop">${dot(i.busy ? "busy" : i.state)}<b>${esc(r.name)}</b><span class="rxband">${esc(r.band)}</span><span class="chev">›</span></span>`
          + `<span class="rxsub">${esc(st)}${esc(open)}${esc(spots)}</span></button>`;
      }).join("");
      const pend = app.pending[pendKey(sv, i.name)];
      const band = (i.receivers && i.receivers.length ? i.receivers.map(r => r.band).join(", ") : String(i.band || ""));
      return `<button class="navrx${is("rx", i.name) ? " on" : ""}" data-act="pick" data-server="${k}" data-kind="rx" data-pick="${esc(i.name)}">`
        + `<span class="rxtop">${dot(i.busy ? "busy" : i.state)}<b>${esc(i.name)}</b><span class="rxband">${esc(band)}</span>${pend ? '<span class="pdot" title="change not applied"></span>' : ""}<span class="chev">›</span></span>`
        + `<span class="rxsub">${esc(i.busy ? `${i.busy}...` : i.state)}${!i.busy && i.state === "running" && (i.settings || {}).record ? ' · <span class="warn">recording</span>' : ""}</span></button>`;
    };
    const blocks = [], shown = new Set(), files = seat.radio_files || [];
    // a radio with no receivers yet still shows, so its first one can be added
    for (const r of [...(seat.radios || []), ...files.filter(f => !(seat.radios || []).some(x => x.name === f.name))]) {
      const mine = list.filter(i => i.radio === r.name);
      if (!mine.length && !files.some(f => f.name === r.name)) continue;
      mine.forEach(i => shown.add(i.name));
      if (!only(k, r.name)) continue;
      const st = (seat.stations || []).find(x => x.radio === r.name), f = r.feeder;
      blocks.push(`<div class="navradio">`
        + `<div class="infoblock">`
        + `<div class="info"><span class="ilabel">Node</span><span>${esc([r.callsign || "no spotter ID", r.grid, r.op_name].filter(Boolean).join(" · "))} · rbn upload ${esc(upMode(r.rbn_upload))}</span></div>`
        + (r.type === "hpsdr" && mine.length === 1 ? `<div class="info"><span class="ilabel">Skimmer</span><span>${skimmerText(mine[0], seat.utc)}</span></div>` : "")
        + `<div class="info"><span class="ilabel">Feeder</span><span>${feederState(f, r.rbn_upload)}${f && (r.rbn_upload || "off") !== "off" ? esc(` · ${Number(f.sent) || 0} spots sent`) : ""}</span></div>`
        + `</div>`
        + `<div class="navhead">RECEIVERS</div>${mine.map(rxItem).join("")}`
        + addItem(k, seat, r.name, is("add", r.name))
        + `</div>`);
    }
    const rest = list.filter(i => !shown.has(i.name));
    if (rest.length && only(k, "")) blocks.push(`<div class="navradio"><div class="navhead">RECEIVERS WITH NO RADIO FILE</div>${rest.map(rxItem).join("")}</div>`);
    const host = (sv.link.hello && sv.link.hello.host) || sv.address;
    const radio = (seat.radios || []).find(r => list.some(i => i.radio === r.name));
    const card = (kind, name, top, sub) => `<div class="navrx${is(kind, name) ? " on" : ""}" role="button" tabindex="0" data-act="pick" data-server="${k}" data-kind="${kind}" data-pick="${esc(name)}">`
      + `<span class="rxtop">${top}<span class="chev">›</span></span>${sub ? `<span class="rxsub">${sub}</span>` : ""}</div>`;
    blocks.push(`<div class="navhead">SERVER</div>`
      + card("server", "", `<b>${esc(host)}</b>`, sv.link.state === "live" ? "" : '<span class="bad">not connected</span>'));
    return blocks.join("");
  }).join('<hr class="navsep">') + `<button class="navadd addsrv" data-act="addserver">+ Add a server</button>`;
}

// the picked receiver: header, facts, its settings (each showing what it was while a change waits), Apply
function receiverPaneHtml(k, sv, i, rxNum) {
  const run = i.state === "running", busy = i.busy, saved = i.settings || {};
  const list = i.receiver_list, rr = list ? list.find(r => r.receiver === rxNum) || list[0] : null;  // an HPSDR receiver
  const radioName = i.radio || i.name, all = list ? `all ${list.length} receivers of ${radioName}` : "";
  const pend = app.pending[pendKey(sv, i.name)] || {}, st = { ...saved, ...pend }, n = Object.keys(pend).length;
  const rx = run && i.radio ? rxOf(sv, i.name) : null, rs = rx ? { ...rx, ...pend } : {};
  const at = `data-server="${k}" data-name="${esc(i.name)}"`;
  const sel = (key, opts, cur) => `<select class="fsel${key in pend ? " pend" : ""}" ${at} data-key="${key}"${busy ? " disabled" : ""}>${
    (opts.some(([v]) => v === cur) ? opts : [[cur, String(cur)], ...opts]).map(([v, l]) =>
      `<option value="${esc(String(v))}"${v === cur ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  const segOf = (key, opts, cur) => `<div class="seg${key in pend ? " pend" : ""}" ${at} data-key="${key}">${opts.map(([v, l]) =>
    `<span data-value="${esc(JSON.stringify(v))}"${v === cur ? ' class="on"' : ""}>${l}</span>`).join("")}</div>`;
  const was = (key, base, fmt = String) => key in pend ? `<span class="fnote">was ${esc(fmt(base))}</span>` : "";
  const known = saved.bands_known && saved.bands_known.length ? saved.bands_known : [];
  const bandOpts = known.includes(saved.band) || saved.band === undefined ? known : [saved.band, ...known];
  const gains = rx && rx.range ? Array.from({ length: Math.floor((rx.range[1] - rx.range[0]) / rx.range[2]) + 1 }, (_, g) => rx.range[0] + g * rx.range[2]) : [];
  const row = (label, control, note = "") => `<div class="frow"><span class="flabel">${label}</span><span class="fctl">${control}${note}</span></div>`;
  const rows = [row("RUN", segOf("run", [[false, "off"], [true, "on"]], "run" in pend ? pend.run : run), rr ? `<span class="fnote">${esc(all)}</span>` : ""),
    rr ? row("BAND", `<select class="fsel${(pend.rx_bands || {})[rr.receiver] ? " pend" : ""}" ${at} data-key="rx_bands" data-rx="${rr.receiver}"${busy ? " disabled" : ""}>${
        (known.includes(rr.band) ? known : [rr.band, ...known]).map(b => `<option value="${esc(b)}"${b === ((pend.rx_bands || {})[rr.receiver] || rr.band) ? " selected" : ""}>${esc(b)}</option>`).join("")}</select>`,
      (pend.rx_bands || {})[rr.receiver] ? `<span class="fnote">was ${esc(rr.band)}</span>` : `<span class="fnote">${run ? `restarts ${esc(all)}` : "used at its next start"}</span>`)
    : row("BAND", saved.multi_band ? esc(String(saved.band)) : sel("band", bandOpts.map(b => [b, String(b)]), st.band),
      "band" in pend ? was("band", saved.band) : `<span class="fnote">${run ? "changes live" : "used at its next start"}</span>`)];
  if (rx && rxHas(rx, "antenna")) rows.push(row("ANTENNA", sel("antenna", rx.ant_list.map(a => [a, a]), rs.antenna), was("antenna", rx.antenna)));
  if (rx && rxHas(rx, "rf_gain")) rows.push(row("RF GAIN", sel("rf_gain", gains.map(g => [g, `${g} dB`]), rs.rf_gain), was("rf_gain", rx.rf_gain, g => `${g} dB`)));
  if (rx && (rxHas(rx, "antenna") || rxHas(rx, "rf_gain"))) {
    const seat = sv.link.seat || {}, r = (seat.radios || []).find(x => x.name === i.radio) || {};
    const mates = (seat.instances || []).filter(x => x.radio === i.radio && x.name !== i.name && x.state === "running").map(x => x.name);
    const note = r.num_scu === 1 ? `Antenna and RF gain are one setting for ${[i.name, ...mates].join(", ")} and the SmartSDR pan (1 SCU): they follow.`
      : r.num_scu > 1 ? `Antenna and RF gain are shared by the receivers on the same SCU (${r.num_scu} SCUs; the radio does not say which): check the others after a change.` : SHARED_NOTE;
    rows.push(`<div class="frow"><span></span><span class="fhint">${esc(note)}</span></div>`);
  } else if (i.radio && !run && radioOf(sv, i).rx_settings && radioOf(sv, i).rx_settings.length) rows.push(`<div class="frow"><span></span><span class="fhint">Antenna and RF gain show while ${esc(i.name)} runs.</span></div>`);
  rows.push(row("RECORDING", segOf("record", [[false, "off"], [true, "on"]], !!st.record), "record" in pend ? `<span class="fnote">restarts ${esc(i.name)}</span>` : "")
    .replace('<div class="frow"', `<div class="frow" title="${esc(HINTS.record)}"`));
  rows.push(row("DECODED TEXT", `<div class="seg" title="Click for the next: Both, Wide, Narrow, Lowest cost"><span class="on" data-act="cycledecoded">${esc(["Both", "Wide", "Narrow", "Lowest cost"][DECODED.indexOf(app.decoded)] || "Both")}</span></div>`,
    '<span class="fnote">this browser: all receivers</span>'));
  const { decRows, win, valid, ons } = decoderOptions(at, st, pend);
  const fold = app.fold || {}, decOpen = !!fold.decoder, detOpen = !!fold.details;
  const feed = app.instances.find(x => x.server === sv && copyName(x) === i.name) || null;
  const entries = `<button class="fentry" data-act="fold" data-fold="decoder" aria-expanded="${decOpen}"><span>Decoder options</span><span class="fnote">${esc(`${win} s windows · validation ${valid} · ${ons} of ${DECODER_OPTIONS.length + 1} on`)}</span><span class="chev">${decOpen ? "\u25BE" : "\u25B8"}</span></button>`
    + (decOpen ? `<div class="fold"><div class="form">${decRows.join("")}</div><div class="fhint">${rr ? `Shared by ${esc(all)}: changes restart them all.` : `Changes restart ${esc(i.name)}.`} Hover over an option for more.</div>`
      + `<div class="fremove"><button class="btn small" ${at} data-act="resetdefaults">Reset to defaults</button></div></div>` : "")
    + `<button class="fentry" data-act="fold" data-fold="details" aria-expanded="${detOpen}"><span>Details</span><span class="fnote">packets, reconnects, warnings, log</span><span class="chev">${detOpen ? "\u25BE" : "\u25B8"}</span></button>`
    + (detOpen ? `<div class="fold details">${feed && feed.status ? statusHtml(feed, i, false) : stoppedInfoHtml(i)}</div>` : "");
  const others = Object.keys(pend).filter(x => !RX_KEYS.includes(x));
  const live = run && (!others.length || (others.length === 1 && others[0] === "band"));
  const label = { run: "run", band: "band", antenna: "antenna", rf_gain: "RF gain", record: "recording", afc: "AFC", itila_window_sec: "window",
    validation: "call validation", call_source: "known call list", ...Object.fromEntries(DECODER_OPTIONS.map(([key, l]) => [key, l])) };
  const what = pend.remove ? "remove" : Object.entries(pend).map(([key, v]) => key === "rx_bands" ? Object.entries(v).map(([n, b]) => `${radioName}-${n} ${b}`).join(", ")
    : `${label[key] || key} ${v === true ? "on" : v === false ? "off" : key === "rf_gain" ? `${v} dB` : v}`).join(", ");
  const bar = n ? `<div class="fbar"><span>${n} change${n > 1 ? "s" : ""} not applied: ${esc(what)}</span><span class="grow"></span>`
    + `<button class="btn small primary" ${at} data-cmd="apply"${busy ? " disabled" : ""}>${pend.remove ? `Apply (removes ${esc(typeof pend.remove === "string" ? pend.remove : i.name)})` : "run" in pend ? `Apply (${pend.run ? "starts" : "stops"} ${esc(i.name)})` : live ? "Apply (live)" : run ? `Apply (restarts ${esc(i.name)})` : "Save"}</button>`
    + `<button class="btn small" ${at} data-cmd="discard">Discard</button></div>` : "";
  const where = run ? (i.receivers || []).map(r => `${r.band} ${Number(r.centre_khz).toFixed(1)} kHz`).join(", ") : "";
  const shown = (run && feed && feed.status) || {};  // an HPSDR receiver: its own band's spots; the feeder has the total
  const binsOf = r => ((i.receivers || []).find(x => x.rx === r.receiver - 1) || {}).bins;
  const total = key => list.some(r => (key === "bins" ? binsOf(r) : r[key]) != null) ? list.reduce((n, r) => n + (Number(key === "bins" ? binsOf(r) : r[key]) || 0), 0) : null;
  const facts = rr ? statusTilesHtml({ ...shown, spots: rr.spots, calls: rr.calls, windows: rr.windows, bins: binsOf(rr), spots_hour: undefined, bins_peak: undefined },
    { label: all, spots: total("spots"), calls: total("calls"), windows: total("windows"), bins: total("bins") }) : statusTilesHtml(shown);
  const state = busy ? `${busy}...` : run ? `running${where ? ` · ${where}` : ""}` : "stopped";
  const err = !run && i.last_error ? `<div class="small err">${esc(i.last_error)}</div>` : "";
  const head = rr ? `${state.split(" · ")[0]} · ${rr.band}${rr.centre_khz ? ` ${Number(rr.centre_khz).toFixed(1)} kHz` : ""}${run && rr.spots !== null ? ` · ${rr.spots} spots` : ""}` : state;
  return `<div class="phead"><span class="ptitle">${esc(rr ? rr.name : i.name)}</span>${dot(busy ? "busy" : i.state)}<span class="muted">${esc(head)}</span></div>`
    + bar + facts + err + `<div class="form">${rows.join("")}</div><div class="fentries">${entries}</div>`
    + (!i.radio || (radioOf(sv, i).type !== "flex" && !rr) ? "" : `<div class="fremove"><button class="btn small${pend.remove ? " pend" : ""}" ${at} data-value="${esc(JSON.stringify(rr ? rr.name : true))}" data-key="remove"${busy ? " disabled" : ""}>`
    + `${pend.remove ? "Removed on Apply" : `Remove ${esc(rr ? rr.name : i.name)}`}</button></div>`);
}

const RADIO_TYPES = [["flex", "FlexRadio"], ["hpsdr", "HPSDR (Web-888)"]];  // radios.BACKENDS

// Setup: the radio picked in the header switch (its type, node, RBN upload, name, removal), then Add a radio.
// The same steps as sparkgap.sh setup, run by the server (discover, node, rename, remove_radio).
const TYPE_NAMES = { flex: "FlexRadio", hpsdr: "HPSDR" };
function setupHtml(k, sv) {
  const seat = sv.link.seat || {}, files = seat.radio_files || [];
  const d = app.setupDraft || (app.setupDraft = {});
  // a radio Find just wrote becomes the picked radio: the section above shows it, ready for its node
  const found = d.found && files.find(x => x.type === d.found.type && (!d.found.name || x.name === d.found.name) && (!d.found.address || x.address === d.found.address));
  if (found) { app.radioSel = { address: sv.address, name: found.name }; save("radioSel", app.radioSel); delete d.found; }
  const sel = radioList().length ? radioSel(app.pick) : null;
  const r = files.find(x => sel && sel.k === k && x.name === sel.name) || files[0];
  const row = (label, control, note = "") => `<div class="frow"><span class="flabel">${label}</span><span class="fctl">${control}${note ? `<span class="fnote">${note}</span>` : ""}</span></div>`;
  let mine = "";
  if (r) {
    const pend = srvPend(sv, r.name), users = (seat.instances || []).filter(i => i.radio === r.name), runs = users.some(i => i.state === "running");
    const field = (label, key, value, ph) => row(label, `<input class="finput short${key in pend ? " pend" : ""}" data-node="${key}" data-server="${k}" data-radio="${esc(r.name)}" value="${esc(key in pend ? pend[key] : value || "")}" placeholder="${esc(ph)}" spellcheck="false">`);
    const facts = [TYPE_NAMES[r.type] || r.type || "radio", r.model, r.address, r.board_id ? `board ${r.board_id}` : "", r.receivers ? `${r.receivers} receivers` : ""].filter(Boolean).join(" · ");
    const mode = pend.rbn_upload || r.rbn_upload || "off";
    const upload = `<div class="seg${"rbn_upload" in pend ? " pend" : ""}">${["off", "dry", "live"].map(m =>
      `<span data-act="srvpick" data-server="${k}" data-radio="${esc(r.name)}" data-key="rbn_upload" data-v="${m}"${m === mode ? ' class="on"' : ""}>${m}</span>`).join("")}</div>`;
    const [what, restart, feeder] = srvPlan(sv, r.name);
    const how = "name" in pend ? `Apply (renames ${r.name})` : restart.length ? `Apply (restarts ${restart.join(", ")})` : feeder ? "Apply (restarts the feeder)" : "Apply";
    const err = (app.srvError || {})[`${sv.address}|${r.name}`];
    const bar = (!what ? "" : `<div class="fbar"><span>not applied: ${esc(what)}</span><span class="grow"></span>`
      + `<button class="btn small primary" data-act="srvapply" data-server="${k}" data-radio="${esc(r.name)}">${esc(how)}</button>`
      + `<button class="btn small" data-act="srvcancel" data-server="${k}" data-radio="${esc(r.name)}">Cancel</button></div>`)
      + (err ? `<div class="small err">${esc(r.name)}: ${esc(err)}</div>` : "");
    mine = `<div class="psec"><span class="psectitle">Setup</span><span class="muted">${esc(r.name)}</span></div>${bar}<div class="form">`
      + row("Type", esc(facts))
      + field("Spotter ID", "callsign", r.callsign, "the callsign RBN shows (N0CALL-2)")
      + field("Grid", "grid", r.grid, "EM12 or EM12ub")
      + field("Operator", "op_name", r.op_name, "the operator's name")
      + row("RBN upload", upload, users.length ? "dry logs what it would post; only the feeder restarts" : "used once it has receivers")
      + (r.type !== "hpsdr" ? "" : row("Name", `<input class="finput short${"name" in pend ? " pend" : ""}" data-node="name" data-server="${k}" data-radio="${esc(r.name)}" value="${esc("name" in pend ? pend.name : r.name)}" spellcheck="false"${runs ? " disabled" : ""}>`,
        runs ? `stop ${esc(r.name)} to rename it` : "its receivers and process follow"))
      + row("Remove", `<button class="btn small" data-act="setupremove" data-server="${k}" data-radio="${esc(r.name)}"${users.length ? " disabled" : ""}>Remove ${esc(r.name)}</button>`,
        users.length ? `remove its receivers first (${esc(users.map(i => i.name).join(", "))})` : "its file is kept, renamed")
      + `</div>`;
  }
  const kind = d.type || "";
  const add = `<div class="psec"><span class="psectitle">Add a radio</span></div><div class="form">`
    + row("Type", `<select class="fsel" data-setup="type">` + [["", "choose a type"], ...RADIO_TYPES].map(([v, l]) => `<option value="${v}"${v === kind ? " selected" : ""}${v ? "" : " disabled"}>${esc(l)}</option>`).join("") + `</select>`)
    + (kind === "hpsdr" ? row("Name", `<input class="finput short" data-setup="name" value="${esc(d.name || "")}" placeholder="Pitaya" spellcheck="false">`, "empty: Web888 or HPSDR") : "")
    + row("Address", `<input class="finput short" data-setup="address" value="${esc(d.address || "")}" placeholder="its IP address" spellcheck="false">`, "empty: search the network")
    + row("", `<button class="btn small primary" data-act="setupfind" data-server="${k}"${kind ? "" : " disabled"}>Find</button>`)
    + `</div>`;
  return mine + add;
}

// the server pane's settings, waiting for Apply as a receiver's do: {address|radio: {key: value}}
function srvPend(sv, radio) { return (app.srvPending || {})[`${sv.address}|${radio}`] || {}; }

function srvPick(sv, radio, key, value) {
  const all = app.srvPending || (app.srvPending = {}), id = `${sv.address}|${radio}`, p = all[id] || (all[id] = {});
  const r = ((sv.link.seat && sv.link.seat.radio_files) || []).find(x => x.name === radio) || {};
  const now = key === "rbn_upload" ? r.rbn_upload || "off" : key === "name" ? radio : r[key] || "";
  if (app.srvError) delete app.srvError[id];  // a new choice: the last refusal no longer applies
  if (String(value).trim() === String(now)) delete p[key]; else p[key] = String(value).trim();
  if (!Object.keys(p).length) delete all[id];
}

// what Apply does to a radio's pending settings: [words, the receivers it restarts, feeder restart]
function srvPlan(sv, radio) {
  const p = srvPend(sv, radio), seat = sv.link.seat || {};
  const running = (seat.instances || []).filter(i => i.radio === radio && i.state === "running").map(i => i.name);
  const node = "callsign" in p || "grid" in p;
  const feeder = !node && running.length > 0 && ("op_name" in p || "rbn_upload" in p);
  const label = { callsign: "spotter ID", grid: "grid", op_name: "operator", rbn_upload: "RBN upload", name: "name" };
  return [Object.entries(p).map(([key, v]) => `${label[key]} ${v}`).join(", "), node ? running : [], feeder];
}

// Apply: write the node settings and the upload mode, then restart what they need (one step)
async function srvApply(sv, radio) {
  const link = sv.link, p = { ...srvPend(sv, radio) }, [, restart, feeder] = srvPlan(sv, radio), id = `${sv.address}|${radio}`;
  const r = ((link.seat && link.seat.radio_files) || []).find(x => x.name === radio) || {};
  const refused = res => { (app.srvError || (app.srvError = {}))[id] = res.error || "refused"; renderSettings(); };  // shown in Setup
  const was = radio;
  if ("name" in p) {  // the rename first: the rest then goes to the new name, in the same Apply
    const res = await link.request("rename", "rename", { radio, name: p.name });
    if (!res.ok) return refused(res);
    delete (app.srvPending || {})[id];
    app.radioSel = { address: sv.address, name: p.name };
    save("radioSel", app.radioSel);
    radio = p.name;
    delete p.name;
  }
  if ("callsign" in p || "grid" in p || "op_name" in p) {
    const res = await link.request("node", "node", { radio, callsign: p.callsign ?? r.callsign ?? "", grid: p.grid ?? r.grid ?? "", op_name: p.op_name ?? r.op_name ?? "" });
    if (!res.ok) return refused(res);
  }
  for (const n of restart) { await seatRequest(link, "stop", n); await seatRequest(link, "start", n); }
  const first = ((link.seat && link.seat.instances) || []).find(i => i.radio === radio || i.radio === was);  // a rename: the snapshot may still say the old name
  if (first && ("rbn_upload" in p || feeder)) await seatRequest(link, "set", first.name, { rbn_upload: p.rbn_upload || r.rbn_upload || "off" });
  delete (app.srvPending || {})[`${sv.address}|${radio}`];
  renderSettings();
}

function serverPaneHtml(k, sv) {
  const a = sv.link, host = `${(a.hello && a.hello.host) || sv.address}${a.hello && a.hello.build ? ` (${a.hello.build})` : ""}`;
  const lines = (a.log || []).map(l => `<div class="cl ${l.level === "ERROR" || l.level === "CRITICAL" ? "bad" : l.level === "WARNING" ? "warn" : ""}">`
    + `<span class="t">${esc(utcTime(l.utc))}</span> ${esc(String(l.msg))}</div>`).join("");
  const sec = html => `<section class="psection">${html}</section>`;
  return `<div class="phead"><span class="ptitle">Server</span><span class="muted">${esc(host)}</span><span class="grow"></span>`
    + `<button class="btn small" data-act="editserver" data-server="${k}">Edit</button><button class="btn small" data-act="removeserver" data-server="${k}">Remove</button></div>`
    + sec(`<div class="psec"><span class="psectitle">Join code</span></div>${joinCodeRow(sv)}`)
    + sec(setupHtml(k, sv))
    + sec(`<div class="psec"><span class="psectitle">Log</span></div><div class="console mono" data-console="r${k}">${lines || '<div class="cl faint">nothing logged yet</div>'}</div>`);
}

// a radio's link as its station reports it: up, first (connecting), wait (reconnecting) or down; "" with no station
function radioLink(seat, radio) {
  const st = ((seat && seat.stations) || []).find(x => x.radio === radio);
  if (!st) return "";
  if (st.state !== "running" || st.live === "halted" || st.live === "stopped") return "down";
  return st.live === "live" ? "up" : !st.live || st.live === "connecting" ? "first" : "wait";  // no report yet: connecting
}

// the radio across the top of the dialog: the picked receiver's (or the first) radio
function radioHeadHtml(pick) {
  const sv = app.servers[pick.k], seat = sv && sv.link && sv.link.seat;
  if (!seat) return "";
  const all = radioList(), sel = all.length > 1 ? radioSel(pick) : null;
  const pickHtml = () => `<select class="fsel" id="radioSwitch" title="Show another radio">${all.map((x, n) =>
    `<option value="${n}"${sel && x.k === sel.k && x.name === sel.name ? " selected" : ""}>${esc(x.label)}</option>`).join("")}</select>`;
  if (sel && sel.name === "") return `<span class="rhlabel">Radio:</span>${pickHtml()}`;
  const seatOf = sel ? (app.servers[sel.k].link.seat || {}) : seat;
  const i = pick.kind === "rx" ? (seat.instances || []).find(x => x.name === pick.name) : null;
  const radiosOf = [...(seatOf.radios || []), ...(seatOf.radio_files || [])];
  const r = sel ? radiosOf.find(x => x.name === sel.name) : (seat.radios || []).find(x => x.name === (i ? i.radio : pick.name)) || (seat.radios || [])[0];
  if (!r) return "";
  const mine = (seatOf.instances || []).filter(x => x.radio === r.name && x.state === "running"), running = mine.length;
  const inUse = mine.reduce((n, x) => n + ((x.receivers || []).length || 1), 0);
  const facts = [r.model || r.type, r.address, r.num_scu ? `${r.num_scu} SCU` : "", r.max_panadapters ? `${running} of ${r.max_panadapters} pans in use` : "",
    r.type === "hpsdr" && r.board_id ? `board ${r.board_id}` : "", r.type === "hpsdr" && r.receivers ? `${inUse} of ${r.receivers} receivers in use` : ""].filter(Boolean);
  const state = radioLink(seatOf, r.name);
  const link = { up: '<span class="good">Connected</span>', down: '<span class="bad">Not connected</span>', wait: '<span class="warn">Reconnecting</span>', first: '<span class="warn">Connecting</span>' }[state]
    || (r.type === "hpsdr" ? (running ? '<span class="good">Running</span>' : '<span class="muted">Stopped</span>') : "");
  return `<span class="rhlabel">Radio:</span>${link}${sel ? pickHtml() : `<span class="rhname">${esc(r.name)}</span>`}<span class="rhfacts">${esc(facts.join(" · "))}</span>`;
}

// a page served by one copy (no server): that copy, and its join code
function hereHtml() {
  $("radioHead").innerHTML = `<span class="rhlabel">This SparkGap:</span><span class="rhname">${esc(location.host)}</span>`;
  const i = app.instances[0];
  return `<div class="panes"><div class="rpane"><div class="phead"><span class="ptitle">This SparkGap</span><span class="grow"></span>`
    + `<button class="btn small" data-act="herecode">Join code</button></div>${i && i.status ? statusHtml(i, null) : '<div class="small muted">connecting...</div>'}</div></div>`;
}

function radiosHtml() {
  if (app.here) return hereHtml();
  if (!app.servers.some(sv => sv.link)) {
    $("radioHead").innerHTML = "";
    return `<div class="panes"><div class="rpane"><div class="phead"><span class="ptitle">No SparkGap server yet</span></div>`
      + `<div class="fhint">Add the server running on the machine beside the radio: its address and its join code (sparkgap.sh status shows both).</div>`
      + (app.editing === -2 ? '<div><button class="btn" data-act="addserver">+ Add a server</button></div>' : "") + `</div></div>`;
  }
  const pick = fitPick(radioPick()), sv = app.servers[pick.k];
  $("radioHead").innerHTML = radioHeadHtml(pick);
  let right = "";
  if (sv && sv.link) {
    if (pick.kind === "rx") {
      const i = ((sv.link.seat && sv.link.seat.instances) || []).find(x => x.name === pick.name);
      right = i ? receiverPaneHtml(pick.k, sv, i, pick.rx) : "";
    } else if (pick.kind === "add") right = addPaneHtml(pick.k, sv, pick.name);
    else right = serverPaneHtml(pick.k, sv);
  }
  return `<div class="panes"><nav class="lpane">${leftPaneHtml(pick)}</nav><div class="rpane">${right}</div></div>`;
}

// the add or edit form of a server (the servers themselves are the Radios view's left side)
function renderConnForm() {
  $("connForm").hidden = app.editing === -2;
  $("fAddr").hidden = app.editing === -3;
  $("fSave").textContent = $("fSave").disabled ? "Searching..." : app.editing === -1 ? "Add" : "Save";
  const notes = [app.migrated, app.editing === -2 ? app.note : ""].filter(Boolean);
  $("connNote").textContent = notes.join(" ");
  $("connNote").hidden = !notes.length;
}
function renderConns() { renderConnForm(); renderSettings(); }

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



// drop the pending settings of an server's copies that gone(name) says are gone
function dropPending(address, gone) {
  for (const k of Object.keys(app.pending)) {
    const bar = k.lastIndexOf("|");
    if (k.slice(0, bar) === address && gone(k.slice(bar + 1))) delete app.pending[k];
  }
}

// a running Flex copy's receive path as its radio reports it (the copy's own feed status), or null
function radioOf(sv, i) { return (((sv.link && sv.link.seat) || {}).radios || []).find(r => r.name === i.radio) || {}; }
function rxOf(sv, name) {
  const c = app.instances.find(x => x.server === sv && copyName(x) === name);
  const r = c && c.status && c.status.radio && c.status.radio.rx;
  return r ? { antenna: r.rxant, loop_a: r.loopa, loop_b: r.loopb, rf_gain: r.rfgain, ant_list: r.ant_list || [], range: r.rfgain_range } : null;
}
// what the radio reports for our pan is all that is offered: a loop only where its status has one
function rxHas(rx, key) {
  if (key === "antenna") return rx.ant_list.length > 0 && typeof rx.antenna === "string";
  if (key === "rf_gain") return !!rx.range && typeof rx.rf_gain === "number";
  return rx[key] === true || rx[key] === false;
}

// pending settings belong to an server by its address, which does not move when the list changes
function pendKey(sv, name) { return `${sv.address}|${name}`; }

// a setting picked: pending until Apply; picked back to what the copy runs, it is no longer pending
function pickSetting(link, name, key, value) {
  const k = pendKey(link.server, name), i = ((link.seat && link.seat.instances) || []).find(x => x.name === name);
  const tpl = name.startsWith("+add:") ? addBase(link.seat || {}, name.slice(5)) : null;  // a receiver not added yet
  const saved = (i && i.settings) || tpl || {}, pend = app.pending[k] || (app.pending[k] = {});
  const base = key === "run" ? !!i && i.state === "running"  // run on or off, applied as a start or stop
    : RX_KEYS.includes(key) ? (rxOf(link.server, name) || {})[key] : saved[key];  // the receive path: the radio's
  if (base === value || (key === "validation" && (saved[key] || "off") === value)) delete pend[key]; else pend[key] = value;
  if (!Object.keys(pend).length) delete app.pending[k];
  renderSettings();
}

// a request to the server; what it does shows in the server log, and so in the footer
async function seatRequest(link, cmd, name, settings) {
  const r = await link.request(cmd, name, settings);
  renderSettings();
  return r.ok;
}

// the footer: the server log's newest line (browsers joining and leaving left out); a click opens the Log
const FOOT_NOISE = /^(Server: )?\S+ (connected|left)$/;
function renderFootLine() {
  const el = $("setNote"), sv = app.servers.find(x => x.link) || null, a = sv && sv.link;
  if (!a) { el.textContent = ""; return; }
  if (a.state !== "live") { el.textContent = `server ${a.state}`; el.className = "small err footline"; return; }
  const l = (a.log || []).slice().reverse().find(x => !FOOT_NOISE.test(String(x.msg)));
  el.textContent = l ? `${utcTime(l.utc)} ${String(l.msg).replace(/^Server: /, "")}` : "";
  el.className = "small footline " + (!l ? "muted" : l.level === "ERROR" || l.level === "CRITICAL" ? "err" : l.level === "WARNING" ? "warnline" : "muted");
  el.dataset.server = String(app.servers.indexOf(sv));
}

// Apply: every pending change in one request, so a running copy restarts once (its label says so)
async function applySettings(link, name) {
  const k = pendKey(link.server, name), settings = app.pending[k] ? { ...app.pending[k] } : null;  // what is sent
  if (!settings) return;
  if ("run" in settings) {  // off: stop, then the rest (written while stopped); on: the rest, then start
    const rest = { ...settings };
    delete rest.run;
    if (!settings.run) for (const key of RX_KEYS) delete rest[key];  // the radio's, moot once stopped
    let ok = settings.run || await seatRequest(link, "stop", name);
    if (ok && Object.keys(rest).length) ok = await seatRequest(link, "set", name, rest);
    if (ok && settings.run) ok = await seatRequest(link, "start", name);
    if (ok) delete app.pending[k];
    renderSettings();
    return;
  }
  if (settings.remove) {  // its config, logs and recordings stay
    if (await seatRequest(link, "remove", typeof settings.remove === "string" ? settings.remove : name)) { delete app.pending[k]; app.pick = null; }
    renderSettings();
    return;
  }
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

function openSettings() { $("modal").hidden = false; renderSettings(); }

// the guide this page ships with: a SparkGap server's own, else GitHub's
function guideUrl() { return /^https?:$/.test(location.protocol) && !app.here ? "/help" : HELP_URL; }

function renderHelp() {
  const news = app.news === undefined ? '<div class="small muted">Loading...</div>'
    : app.news || `<div class="small muted">See the guide's What's new.</div>`;
  $("helpBody").innerHTML = `<section class="scard wide"><h3><span>HELP</span></h3>
    <div class="grid setgrid"><span class="label">GUIDE</span><a href="${esc(guideUrl())}" target="_blank" rel="noopener">SparkGap Web guide</a>
    <span class="label">KEYS</span><span>Ctrl-F finds a call or kHz on the main screen; with this dialog open it is the browser's own find. Esc closes it.</span></div></section>
    <section class="scard wide news"><h3><span>WHAT'S NEW</span></h3>${news}</section>`;
  if (app.news === undefined) loadNews();
}

// What's new: the newest entries of the guide's own section, as the server renders it
async function loadNews() {
  app.news = null;
  try {
    if (guideUrl() !== "/help") throw new Error("no server");
    const doc = new DOMParser().parseFromString(await (await fetch("/help")).text(), "text/html");
    const head = [...doc.querySelectorAll("h2")].find(h => /what.s new/i.test(h.textContent));
    const parts = [];
    for (let n = head && head.nextElementSibling; n && n.tagName !== "H2"; n = n.nextElementSibling) parts.push(n);
    const items = parts.flatMap(n => n.tagName === "UL" ? [...n.children].slice(0, NEWS_SHOWN) : []);
    app.news = items.length ? `<ul class="newslist">${items.map(li => `<li>${li.innerHTML}</li>`).join("")}</ul>` : null;
  } catch (e) { app.news = null; }
  if (!$("modal").hidden && app.mode === "help") renderHelp();
}
// a click in a copy's tab or a server's card: a command, or a setting picked
async function seatClick(e) {
  const t = e.target.closest("[data-cmd], [data-value]");
  if (!t || t.disabled || t.closest(".seg.off")) return;  // the known call list while validation is off
  const [link, name, holder] = seatTarget(t);
  if (!link) return;
  e.stopPropagation();
  const cmd = t.dataset.cmd;
  if (t.tagName === "SPAN" && t.classList.contains("on")) return;  // the run switch, already there
  if (cmd === "show") {
    const n = app.instances.findIndex(x => x.server === link.server && copyName(x) === name);
    if (n >= 0) { setCurrent(n); closeSettings(); }
  } else if (cmd === "apply") applySettings(link, name);
  else if (cmd === "discard") { delete app.pending[pendKey(link.server, name)]; renderSettings(); }
  else if (cmd === "discover") seatRequest(link, "discover", "radios");
  else if (cmd === "startall" || cmd === "stopall") {
    const stop = cmd === "stopall", list = ((link.seat && link.seat.instances) || []).filter(x => (x.state === "running") === stop && !x.busy);
    if (!list.length || (stop && !confirm(`Stop ${list.map(x => x.name).join(", ")}?`))) return;
    for (const x of list) await seatRequest(link, stop ? "stop" : "start", x.name);
  } else if (cmd) {
    if (cmd !== "start" && !confirm(`${cmd === "stop" ? "Stop" : "Restart"} ${name}?`)) return;
    seatRequest(link, cmd, name);
  } else if (!t.classList.contains("on")) pickSetting(link, name, holder.dataset.key, JSON.parse(t.dataset.value));
}

function closeSettings() { $("modal").hidden = true; app.editing = -2; app.form = null; $("fSave").disabled = false; }

// ---- input ----

function bindInput() {
  $("gear").onclick = () => {
    if (!$("modal").hidden) return closeSettings();
    app.mode = "radios";
    openSettings();
  };
  $("helpBtn").onclick = () => { app.mode = app.mode === "help" ? "radios" : "help"; renderSettings(); };
  $("radiosBody").onclick = e => {
    if (e.target.closest('[data-act="code"], [data-act="copycode"]')) return;  // the dialog's own handler
    const sp = e.target.closest('[data-act="srvpick"], [data-act="srvapply"], [data-act="srvcancel"]');
    if (sp) {
      const sv = app.servers[Number(sp.dataset.server)];
      if (!sv || !sv.link) return;
      if (sp.dataset.act === "srvpick") srvPick(sv, sp.dataset.radio, sp.dataset.key, sp.dataset.v);
      else if (sp.dataset.act === "srvapply") { srvApply(sv, sp.dataset.radio); return; }
      else { if (document.activeElement) document.activeElement.blur(); delete (app.srvPending || {})[`${sv.address}|${sp.dataset.radio}`]; }
      renderSettings();
      return;
    }
    const su = e.target.closest('[data-act="setupfind"], [data-act="setupremove"]');
    if (su) {
      const link = app.servers[Number(su.dataset.server)] && app.servers[Number(su.dataset.server)].link;
      if (!link) return;
      const d = app.setupDraft || {};
      if (su.dataset.act === "setupremove") {
        if (confirm(`Remove ${su.dataset.radio} from SparkGap? Its radio file is kept, renamed.`)) {
          seatRequest(link, "remove_radio", "radios", { radio: su.dataset.radio }).then(ok => { if (ok) app.setupDraft = { type: d.type }; renderSettings(); });
        }
        return;
      }
      if (!d.type) return;
      const named = d.type === "hpsdr" && (d.name || "").trim() ? { name: d.name.trim() } : {};
      const input = $("radiosBody").querySelector('[data-setup="address"]'), address = ((input && input.value) || "").trim();
      const at = address ? { address } : {};  // an address asks that radio; none searches the network
      app.setupDraft = { type: d.type, found: { type: d.type, ...at, ...named } };
      seatRequest(link, "discover", "radios", { type: d.type, ...at, ...named });
      return;
    }
    const f = e.target.closest('[data-act="fold"]');
    if (f) { app.fold = { ...(app.fold || {}), [f.dataset.fold]: !(app.fold || {})[f.dataset.fold] }; renderSettings(); return; }
    const rd = e.target.closest('[data-act="resetdefaults"]');
    if (rd) { const [link, name] = seatTarget(rd); if (link) resetDefaults(link, name); return; }
    const ad = e.target.closest('[data-act="addapply"], [data-act="addcancel"]');
    if (ad) {
      const s = app.servers[Number(ad.dataset.server)];
      if (ad.dataset.act === "addcancel") { app.addDraft = null; delete app.pending[pendKey(s, addKey(ad.dataset.radio))]; app.pick = null; renderSettings(); }
      else if (s && s.link) addReceiver(s, ad.dataset.radio);
      return;
    }
    const p = e.target.closest('[data-act="pick"]'), sv = e.target.closest('[data-act="addserver"], [data-act="editserver"], [data-act="removeserver"], [data-act="herecode"]');
    if (p) { app.pick = { k: Number(p.dataset.server), kind: p.dataset.kind, name: p.dataset.pick, rx: Number(p.dataset.rx) || undefined }; renderSettings(); return; }
    if (sv) {
      const act = sv.dataset.act, n = Number(sv.dataset.server);
      if (act === "addserver") editConn(-1);
      else if (act === "herecode") editConn(-3);
      else if (act === "editserver") editConn(n);
      else if (confirm(`Remove ${app.servers[n].address}? Adding it again needs its join code.`)) { removeConn(n); app.pick = null; renderSettings(); }
      return;
    }
    if (e.target.closest('[data-act="cycledecoded"]')) {
      app.decoded = DECODED[(DECODED.indexOf(app.decoded) + 1) % DECODED.length]; save("decoded", app.decoded); renderSettings(); renderDetail(); return;
    }
    seatClick(e);
  };
  // hold rebuilds while a button is pressed in the dialog; rebuild once after the click is handled
  $("modal").addEventListener("pointerdown", () => { app.pressed = true; });
  const released = () => {
    if (!app.pressed) return;
    app.pressed = false;
    if (app.renderDue) { app.renderDue = false; setTimeout(renderSettings, 0); }
  };
  // a dropdown's own list takes the release, so its change ends the press
  for (const ev of ["pointerup", "pointercancel", "change"]) document.addEventListener(ev, released);
  $("radiosBody").oninput = e => {
    const ds = e.target.dataset || {};
    if (ds.setup) (app.setupDraft || (app.setupDraft = {}))[ds.setup] = e.target.value;
    if (ds.node) { const sv = app.servers[Number(ds.server)]; if (sv && sv.link) srvPick(sv, ds.radio, ds.node, e.target.value); }
  };
  $("radioHead").onchange = e => {
    if (e.target.id !== "radioSwitch") return;
    e.target.blur();  // a focused switch holds off the redraw that shows the radio picked
    const x = radioList()[Number(e.target.value)];
    if (!x) return;
    app.radioSel = { address: app.servers[x.k].address, name: x.name };
    save("radioSel", app.radioSel);
    app.pick = firstPick(x);
    renderSettings();
  };
  $("radiosBody").onchange = e => {
    const add = e.target.dataset && e.target.dataset.add;
    if (add && app.addDraft) { app.addDraft[add] = add === "channel" ? Number(e.target.value) : e.target.value; renderSettings(); return; }
    if (e.target.dataset && e.target.dataset.setup === "type") { renderSettings(); return; }
    if (e.target.dataset && e.target.dataset.node) { renderSettings(); return; }
    const [link, name, holder] = seatTarget(e.target);
    const key = holder && holder.dataset.key;
    if (link && key === "rx_bands") {
      e.target.blur();
      const i = ((link.seat && link.seat.instances) || []).find(x => x.name === name), rr = i && (i.receiver_list || []).find(r => String(r.receiver) === holder.dataset.rx);
      if (!rr) return;
      const cur = { ...((app.pending[pendKey(link.server, name)] || {}).rx_bands || {}) };
      if (e.target.value === rr.band) delete cur[rr.receiver]; else cur[rr.receiver] = e.target.value;
      pickSetting(link, name, "rx_bands", Object.keys(cur).length ? cur : undefined);
      return;
    }
    if (!link || !["band", "antenna", "rf_gain"].includes(key)) return;
    e.target.blur();
    pickSetting(link, name, key, key === "rf_gain" ? Number(e.target.value) : e.target.value);
  };
  $("modal").addEventListener("click", e => {
    if (e.target.closest('[data-act="code"]')) { app.codeShown = !app.codeShown; renderSettings(); }
    const copy = e.target.closest('[data-act="copycode"]');
    if (copy) copyText(copy.dataset.code).then(ok => { copy.textContent = ok ? "Copied" : "Copy failed"; });
  });
  $("dCopy").onclick = () => copyText($("dWins").innerText).then(ok => {
    $("dCopy").textContent = ok ? "Copied" : "Copy failed";
    setTimeout(() => { $("dCopy").textContent = "Copy"; }, 1500);
  });
  $("close").onclick = closeSettings;
  $("setNote").onclick = () => { const k = Number($("setNote").dataset.server); if (k >= 0) { app.pick = { k, kind: "server" }; app.mode = "radios"; renderSettings(); } };
  $("themeBtn").onclick = () => setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  $("modal").onmousedown = e => { if (e.target === $("modal")) closeSettings(); };
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
    // Ctrl-F finds a call on the main page; with Settings | Info open it is the browser's own find
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f" && $("modal").hidden) { e.preventDefault(); search.focus(); search.select(); }
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
