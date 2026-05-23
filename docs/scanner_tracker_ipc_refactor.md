# Refactor: structured IPC between _ItilaScanner and SpotTracker

**Status:** scoped 2026-05-23. Predecessor fix (Option A — per-entry pending
flush) shipped same day; this is the deeper architecture fix flagged during
that work.

**What's in the current PR (sparkgap-ipc-refactor branch):** the IPC seam
itself — `SpotIntent` dataclass, scanner emits intents instead of synthetic
text, dispatch sites updated.  `SpotTracker.process_intent()` is a thin
wrapper that synthesizes the legacy `"CQ <call> "` string and delegates to
`process(dec_type='itila')`, keeping gate behavior bit-identical to today.

**What's deferred to a follow-up PR:** the `_emit_spot_if_gated` helper
factoring, stripping the ITILA branch from `process()`, and moving
bucket/blacklist checks to the scanner side.  Those are listed below under
*Migration plan* as steps 2-5 — they describe the eventual end state, not
what this PR ships.  Read those sections as the design target, not as
already-landed code.

## Why

`_ItilaScanner` extracts a callsign from each window (Path 1 = CQ-runner
extraction, Path 2 = caller-spotting). To get that callsign through to
`SpotTracker`, it currently encodes the result as a synthetic string
`'CQ <call> '` and appends to `st['pending']`. `collect()` returns those
strings as `text`; `SpotTracker.process()` then re-parses them — running
`CALL_RE.finditer()`, `_itila_extract_cq_call()`, `CQ_PATTERNS.search()`,
`_scp_bucket()`, etc. — to recover what the scanner already knew.

The string IPC is the root cause of an entire class of bugs:

- **Pending-merge bug** (fixed by Option A 2026-05-23): `''.join(pending)`
  let multiple window events appear as one tracker event, conflating
  cq_runner determination across events. Suppressed K0TG, K1RV, N5XZ,
  NQ2W, W4SPR — 5 of 8 file-mode-missed CQers on B1_seg2.
- **Tie-break ambiguity**: `_itila_extract_cq_call` picks "most-frequent,
  last on ties" — when scanner already knew which call came from which
  window, that information is lost in the string and the wrong call wins.
- **Synthetic-CQ pollution**: Path 2 (caller-spotting) prepends `"CQ "` to
  every caller so the tracker treats it with CQ-context semantics. The
  tracker's `cq_runner` gate then has to distinguish "caller spotted as
  runner" from "actual runner" — and gets it wrong when the text contains
  both.
- **Bucket / blacklist runs twice**: `_scp_bucket()` and blacklist checks
  run inside `_itila_extract_cq_call` (in the scanner's spotted-set logic
  via the indirect path) and again in `tracker.process`. Two sources of
  truth for "what's this call's canonical form."

## Target

Replace the string channel with a structured channel. `_ItilaScanner`
emits one `SpotIntent` record per Path 1 or Path 2 extraction:

```python
@dataclass
class SpotIntent:
    call:       str         # callsign as extracted, uppercase, slash preserved
                            # (slash preservation is what enables future Method 0
                            # for PJ2/AG3I and W1AW/0 literal-match extraction)
    freq_khz:   float
    snr_db:     float
    wpm:        int
    is_runner:  bool        # True if Path 1 (extracted adjacent to CQ trigger)
    raw_text:   str         # the window's primary_text — for telemetry/debug only
    window_id:  int         # monotonic per-bin window counter — lets downstream
                            # group multi-call windows (caller + runner) without
                            # cross-window leakage
    bin_id:     int         # id(st) — stable for bin lifetime
```

`InstanceManager.collect_all()` returns these alongside (or instead of)
text-tuples. `SpotTracker.process()` gains a new entry point — call it
`process_intent(intent)` — that consumes records directly. The legacy
text path stays for uhsdr_cw / bmorse / hamfist (those decoders genuinely
emit streamed character text; their IPC isn't synthetic).

## What this removes

Inside `SpotTracker.process()` (currently ~700 lines), the ITILA branch
no longer needs:

- `re.sub(r'\b[EIT]\b', '', new_text.upper())` — no text to clean
- `CALL_RE.finditer(clean)` — call is already known
- `_itila_extract_cq_call(context_text, valid_calls)` for runner identification — `intent.is_runner` is authoritative
- The "two-CQ-in-one-buffer" tie-breaker (the K0TG/N4BA pathology)
- The cq_runner gate becomes trivial: `if intent.is_runner: bypass_count_gate`

Inside `_ItilaScanner`:

- `st['pending']` no longer needs to hold strings; replace with a list of intents
- The Path 2 `f'CQ {c} '` synthetic prefix goes away — `is_runner=False` carries the signal directly
- The `st['spotted']` dedup set already exists per-bin; keep it (still useful)

## What this keeps

Tracker-side gates that operate per-call (not per-text):

- `_record_sighting` / `_count_recent_sightings` / consensus
- `blacklist` / `FALSE_POSITIVES` / `_scp_bucket` (run once, at the source — scanner side)
- `_freq_committed` / `_can_respot` / `_is_freq_leader` / `_harmonic_check`
- `gate_patt3ch_filter` / `gate_bypass_consensus` / `gate_freq_consensus`
- `scp_bypass_threshold` path for non-SCP structurally-valid calls

These all work on `(call, freq_khz, snr, now)` — no text needed.

## Migration plan

1. **Define `SpotIntent`** in sparkgap.py near `SpotTracker` (top-of-file
   typing imports). Keep it a `@dataclass` or `namedtuple` — minimal
   ceremony.

2. **Add `SpotTracker.process_intent(intent) -> list[spot]`**. Factor the
   shared post-extraction logic (sightings, consensus, freq commit,
   respot, harmonic, ingest support) out of `process()` into a helper
   `_emit_spot_if_gated(call, freq_khz, snr, wpm, has_cq_context, is_runner, now)`.
   Both `process()` (legacy text path) and `process_intent()` call it.

3. **Update `_ItilaScanner.collect()`** to emit `SpotIntent` records:
   - Path 1 → `is_runner=True`
   - Path 2 → `is_runner=False`
   - Bucket + blacklist checks move here (single source of truth)

4. **Update `InstanceManager.collect_all()`** signature: return
   `(text_tuples, spot_intents)` or interleave with a discriminator. Keep
   call sites in `run_file_mode` (line 7280) and `async_main` updated.

5. **Strip the ITILA branch out of `SpotTracker.process()`**. The
   `if dec_type == 'itila':` lines at 5540-5541 and the synthetic-text
   handling can go.

6. **Add `_itila_scanner_metrics.intents_emitted{runner=true|false}`** for
   telemetry — gives us per-second visibility into the scanner's spot
   intent rate (today this is buried in tracker.process noise).

## Testing protocol

Both metrics on `B1_seg2_15-30min_7090kHz.wav` (0-15 min) against
`cq_key_56`:

| Stage | Expected recall | Notes |
|---|---|---|
| Before any change | 44/56 (78.6%) | baseline 2026-05-23 |
| After Option A (text path, per-entry flush) | ~49/56 (87.5%) | this PR |
| After structured-IPC refactor | ≥49/56, ideally 50-52/56 | no recall regression + headroom for fixing the W6KC class (Path 1 never extracted) |

Plus per-window CQer-recall metric (`research/fusion_per_window.py`) —
should stay flat (refactor doesn't change what gets decoded, just how the
result is passed).

## Risks

- **uhsdr_cw / bmorse code paths**: must NOT regress. Easy to verify —
  the ITILA branch is isolated by `dec_type == 'itila'`.
- **Tracker telemetry**: existing log lines reference `text` and `ctx`.
  Keep `raw_text` in the intent so log format doesn't change.
- **Cluster / Telnet downstream**: spot dicts already carry `call`,
  `freq_khz`, `snr`, `wpm`, `method`. No external format change.

## Out of scope (deliberately)

- Path 1 vs Path 2 unification — keep them separate; just change how
  their results are conveyed.
- Bucket/correction logic improvements — those are independent bug
  classes, not blocked by this refactor.
- Replacing `valid_calls` global with a per-tracker handle — works fine
  as a set.
