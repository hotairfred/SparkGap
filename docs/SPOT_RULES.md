<!-- markdownlint-disable MD013 -->
# Spot rules (spot_rule2)

2026-11-16

This document codifies the spot requirements as we understand them from the reference stack (CW
Skimmer, Skimmer Server and the Aggregator), and how spot_rule2 implements them in SparkGap. Where
spot_rule2 adds a rule of its own, the Source column says SparkGap.

SparkGap sends a CW spot only when every gate below is true, checked in order. A call not spotted is
dropped by the first gate that is false, and the log names that gate. The rule is `spot_rule2.py`
(`GateSpotRule`), switched on with `spot_rule_gates` (off by default; with it off, spots are identical
to the `repeat` rule). Tracking issue: cdub89/SparkGap#51.

The gates do two jobs, in order:

1. **Call validation** (gates S to 4): is this a real call?
2. **Spot decision** (gates 5 to 12): should this call be spotted now?

A spot rule for RBN has to be:

- **Repeatable:** the same decoded text always gives the same spots, live or on replay.
- **Explainable:** for any spot, or any call not spotted, the log says which gate decided it. Every
  decision is kept in `.trace` and logged at debug level as (gate, reason).
- **Familiar to RBN operators:** wherever possible the gates follow the reference stack, so a node
  behaves the way operators already expect a node to behave.

## Reference settings

The reference stack these requirements come from runs with these settings:

| Setting | Reference | spot_rule2 |
| --- | --- | --- |
| CW Skimmer validation | Normal | Gate 3 reads 2/3/5 at normal |
| CW Skimmer "Do not send call signs without CQ" | Unchecked: Skimmer sends every call, tagged CQ or not, and the Aggregator decides. It posts CQ and beacon spots, as RBN's spot windows allow, and logs the rest as NCQ | Gates 5 to 8 and 12 do both steps: only calls tied to a keyword, and beacons, are sent |
| CW Skimmer band plan | IARU Region 2 (CW Skimmer's default is Region 1) | The decoded window comes from an IARU Region 2 table, up to the top of the US CW and data segment (`cw_whole_segment`) |

## Call validation: is this a real call?

Gates read the signal's buffer (gate S) unless they say otherwise. They use the call validation
level (`spot_validation`) and, when the node sets one, the known call list (`spot_validation_calls`).

| # | Gate | True when | Source |
| --- | --- | --- | --- |
| S | Buffer | Each signal (decoder bin) keeps its last 256 decoded characters as one continuous text, first in, first out, with no age limit. The buffer ends with its bin. Of a window's two filter paths it keeps the copy the decoder scores cleaner (its cost); on WX7V-1, LakeDog-2 and a 40m CWT that cut Potential Junk 17 to 16, 11 to 7 and 6 to 1 for the same spots (cdub89/SparkGap#56) | CW Skimmer (256 characters per decoder); SparkGap (the cleaner copy) |
| 1 | Shape | Prefix `@`, `@@`, `#@` or `#@@`, then 1 to 4 digits, then 1 to 6 letters | CW Skimmer (patt3ch.lst shapes) |
| 2 | Not banned | When `spot_rule_bad_calls` is on, the call is not in `BadCalls.txt`; and when the MASTER.SCP filter (`spot_rule_scp`) is on, the call is in MASTER.SCP or on the known call list, unless gate 12 takes it as a beacon | Aggregator (Bad Calls, Only in MASTER.SCP); SparkGap (known call list) |
| 3 | Reads | Read in the buffer at least the count for the call's patt3ch.lst pattern at the node's call validation level: at normal, 2 for a `+` pattern, 3 for an unflagged pattern, 5 for no pattern; one fewer at permissive, one more at aggressive. Off or unset applies normal. A call decoded twice is read twice. A call not on the node's known call list needs one more read; a known call list never lowers the count. Example at aggressive: a call not on the list with a `+` pattern needs 4 reads (`3 of 4 reads (active pattern, aggressive, not listed)`) | SparkGap |
| 4 | Best reading | The most-read call in its buffer wins; a tie (N5AL and W5AL read equally) spots neither. A call split by a gap (`K1V UT`) is a read of the longer call read whole there (K1VUT). A cut-off copy of a longer call read there (N5T of N5TJ) never wins. Noise or sign-off letters (E T I S K) never make a longer call (K1TRE, `KE8UEX K`), nor does a portable form (W1AW/7 is W1AW) | SparkGap |

## Spot decision: should this call be spotted now?

| # | Gate | True when | Source |
| --- | --- | --- | --- |
| 5 | Keyword | The buffer holds CQ or TEST, or a qualifier the node enables (gate 6) | CW Skimmer (CQ, TEST) |
| 6 | Qualifier | A qualifier the node enables (`spot_rule_qualifiers`) acts as the event's CQ: `SS N6RO` | CW Skimmer (FD, SS, NA settings); SparkGap (the other qualifiers) |
| 7 | Tied | The call is the first callsign within 3 words after the keyword (`CQ TEST N6KD N6KD 5NN TU`: N6KD), or follows DE with the keyword within 3 words before DE (`CQ CQ CQ DE KB2DHG`: KB2DHG), or ends `TU <call> TEST`. See [Tied to a keyword](#tied-to-a-keyword) | CW Skimmer (CQ spots); SparkGap (the tie rules) |
| 8 | Own keyword | The keyword does not come right after another call (`K1ABC TEST DL1XYZ`) | SparkGap |
| 9 | Strongest | The copy beats every other copy of the same call more than 1 kHz away on the band, in the last 60 s, by `spot_rule_strongest_db` (6 dB unless set; null or 0 turns this gate off). A tie spots none. Example: while WX7V keys `CQ WES` on 7036.0, images of it appear on several 15m frequencies at once (`CQ GORES DE WK7V RES K` on 21111.1, `RQGTKRES BE WK7V REEE K CQ CQRES` on 21111.5); none beats the others by 6 dB, so none spots | SparkGap |
| 10 | Re-spot | The call was not spotted on this band in the last 10 minutes (plus 15 s, as RBN times it by upload), unless it moved 1 kHz or more. Examples: W7LHT on 7041.53 spotted at 00:32, 00:43 and 00:54Z, and nothing between; N0AC spotted on 14054.63, then again 3 minutes later on 14059.52 after a move | CW Skimmer |
| 11 | Window | The frequency is inside the band's decoded CW window: the band edge to the top of the US CW segment (7000 to 7125, 14000 to 14150), as far as the receiver's span reaches, with the FT8 and JS8 frequencies skipped. With `spot_rule_notched_freqs` on, every range in `NotchedFreqs.txt` is left out. Examples: N7MM spotted on 7123.03 and WA6YU on 14142.05; nothing is decoded on 14073.5 to 14081.5 (FT8) | Skimmer Server (CW segments); Aggregator (Notched Freqs) |
| 12 | Beacon | A beacon skips gates 5 to 8. A `/B` call, on any frequency, still needs gate 3's reads. An NCDXF beacon within 5 kHz of any of the five NCDXF frequencies (14100, 18110, 21150, 24930, 28200), or a call in `spot_rule_beacons` within 5 kHz of its listed frequency, spots on one read. An NCDXF beacon on its frequency also skips gate 4: the fading dashes between beacons decode as noise words that would tie it (`VE8AT I6BT` on 14100). Other beacons in its buffer are not a listed beacon's rivals at gate 4. On an NCDXF frequency, a word one letter off exactly one NCDXF call is read as that call, unless the word is on the known call list. Examples: `KH6RS T` on 21150.0 spots KH6RS; `UH6RS T` on 21150.0 also spots KH6RS, never UH6RS; `OA2B` spots nothing (it could be OH2B or OA4B). On RBN a beacon spot is typed as RBN stores it, `NCDXF B` for an NCDXF beacon and `BEACON` for a listed or `/B` one, not CQ; the telnet line still says CQ | Aggregator (beacon match); SparkGap (one read, one-letter correction) |

As CW Skimmer's Watch List spots a listed call the first time it is heard, gate 12 spots an NCDXF or
listed beacon on one read.

## Keywords

| Keyword | Allowed | Why |
| --- | --- | --- |
| CQ | Yes | The runner's call |
| TEST | Yes | The contest CQ, and `TU <call> TEST` |
| QRZ | No | Asks who is calling, no CQ: on WX7V-1 it spotted a net's callers as runners (cdub89/SparkGap#56) |
| UP | No | Ties callers in split pileups |
| TU | No | Callers send it too; as a sign-off it ends the CQ |
| QRL? | No | Not a CQ |

DE is not a keyword; it may sit between a keyword and the call.

## Tied to a keyword

Gate 7 decides which call in the buffer a keyword belongs to.

| Rule | Example |
| --- | --- |
| The call is the first callsign within 3 words after the keyword. A noise letter (E T I S ?) takes no word | `CQ E K1ABC`: K1ABC is tied |
| A sign-off (AR KN SK BK TU) ends the CQ; K and BT do not | `CQ TU K1ABC`: K1ABC is not tied |
| The call after DE is tied when CQ or TEST comes within 3 words before DE | `CQ CQ CQ DE KB2DHG DE KB2DHG K`: KB2DHG is tied |
| A callsign right before DE is skipped and never tied: it is the station called, or a word decoded as a call | `CQ WE TEST CQ WE T5T DE K3TW K3TW`: K3TW is tied, T5T (TEST) is not |
| Two callsigns before DE stop the tie (callers send DE too) | `CQ SS W5ACQ W5ACQ DE WC6A WC6A`: W5ACQ is tied, WC6A is not |
| Gate 4 settles a runner and a caller in one buffer | `CQ SS N4TY DE W0UI W0UI`: W0UI is tied, but N4TY, read more often, wins at gate 4 |
| A read followed by its exchange is a QSO, never tied: a name then a number or state, or a report then a number or county. A runner's own report doesn't count, nor does a grid square, which in decoded text is mostly noise after a call | `N4YDU KEITH 2182` and `K0PC 5NN CLRK`: not tied. `CQ TEST N6KD N6KD 5NN TU` and `CQ SS K3UA K3UA GL73`: N6KD and K3UA are tied |
| `TU <call> TEST` ties the call | `TU K1ABC TEST` |
| A repeat right after a tied read is tied too | `CQ K1ABC K1ABC` |

## Qualifiers

A qualifier is an event word that acts as CQ on a node that enables it: a node enables the events it
wants to spot (`spot_rule_qualifiers`, none by default). `spot_rule_qualifiers: ["CWT", "SST",
"MST"]` spots `CWT K5AX` as `CQ K5AX`.

Choices: SS, NA, FD, CWT, SST, MST, POTA, SOTA, WWFF, IOTA, SKCC, QRP, DX.

A qualifier that is not enabled is not a keyword, but `CQ POTA K1ABC` still ties K1ABC to CQ by the
3-word look-ahead.

## Aggregator filters

The Aggregator's optional spot filters, all off in a fresh install. They apply after Skimmer has
decided a spot, and drop it before it is posted.

| Filter | What it drops | SparkGap |
| --- | --- | --- |
| Only in MASTER.SCP | Every spot whose call is not in MASTER.SCP, beacons excepted. CW Skimmer's Paranoid level; the RBN team recommends against it | Default off (`spot_rule_scp`, the MASTER.SCP Filter, gate 2), from MASTER.SCP; a call on the known call list passes without it |
| Bad Calls | Calls in `BadCalls.txt`, one per line | Default off (`spot_rule_bad_calls`, gate 2) |
| Notched Freqs | Frequencies in `NotchedFreqs.txt`: a kHz or a range per line, `7074-7077` or `7074 7077` | Default off (`spot_rule_notched_freqs`, gate 11) |
| Grid squares | Calls shaped like a grid square, above 28.145 MHz | Not implemented |
| VHF+ SNR | Spots above 49.999 MHz at 1 dB or less, FT8 and FT4 excepted | Not implemented |

## Node settings

`spot_rule_gates` (default off) switches the gate rule on in place of the `repeat` rule.

Call validation:

| Key | Default | Gate | What it does |
| --- | --- | --- | --- |
| `spot_validation` | off (applies normal) | 3 | The call validation level: `permissive`, `normal` or `aggressive` steps gate 3's reads by one fewer, none or one more. Off or unset applies normal. Reported to RBN as the level applied; the start log names it and its counts |
| `spot_validation_calls` | none | 3 | The known call list (`scp`, `rbn` or `rbn+skcc`); a call not on it needs one more read. Not reported to RBN |
| `spot_rule_bad_calls` | off | 2 | Calls in `BadCalls.txt` beside the config never spot: a node's own call and its busts while it transmits |
| `spot_rule_scp` | off | 2 | The MASTER.SCP filter: a call in neither MASTER.SCP nor the known call list never spots, beacons excepted; with no list, MASTER.SCP alone decides. It acts only with `spot_rule_gates` |

Spot decision:

| Key | Default | Gate | What it does |
| --- | --- | --- | --- |
| `spot_rule_qualifiers` | none | 5, 6 | Event words that act as CQ on this node |
| `spot_rule_strongest_db` | 6 | 9 | The SNR margin a copy must beat; null or 0 is off |
| `spot_rule_notched_freqs` | off | 11 | No spot on a frequency in `NotchedFreqs.txt` beside the config |
| `spot_rule_beacons` | none | 12 | Beacon calls, each with its frequency in kHz |

`spot_rule_bad_calls`, `spot_rule_scp` and `spot_rule_notched_freqs` are also the Bad Calls, MASTER.SCP Filter and Notched Freqs switches
under Decoder options in SparkGap Web: in the radio's Node pane, or in the receiver's pane for a
receiver with no radio file. A change restarts the receivers that share the options.

## Not part of the rule

| Item | Why not |
| --- | --- |
| MASTER.SCP as a gate | Measured: no gain. A list only adds a read (gate 3) |
| UP as a keyword | Spots callers in split pileups |
| Non-CQ spots | The Aggregator logs them as NCQ and does not post them (see [Reference settings](#reference-settings)). The gate rule never sends them |
