# Radios, receivers and feeders

How SparkGap runs IQ radios, the same way for every SDR that delivers IQ
streams: FlexRadio today, Web-888 and Red Pitaya (HPSDR) next. Design for
cdub89/SparkGap#33.

## The model

Three things the operator sees, named the same on every SDR, in SparkGap
Web, the command line and the logs:

- **Radio**: the device, and the RBN node (one spotter ID, one feeder). Its
  name is the radio's own nickname: `Lake` (FLEX-6600), `Dallas`
  (FLEX-6400M), `Web888`.
- **Receiver**: one band window the radio delivers as IQ (band, centre,
  width), named `<radio>-<n>`: `Lake-1` to `Lake-4`, `Dallas-1`, `Web888-1`
  to `Web888-8`. On FlexRadio `n` is the DAX-IQ channel; on HPSDR it is the
  receiver number.
- **Feeder**: posts the radio's spots to RBN (`rbn2_feeder.py`), one per
  radio, fed by all its receivers; it applies the re-spot interval per call
  across them.

A receiver belongs to one radio. The skimmer process that decodes it
(`sparkgap.py`) is a part of the back end, not of the model: FlexRadio runs
one per receiver, HPSDR one for all the radio's receivers.

What a back end needs to get there stays inside it: the Flex station
(SparkGap's one GUI client on the radio, which its receivers share), DAX-IQ
channels, the Flex serial lookup, HPSDR receiver indexes.

## Files

- `config/<radio>.radio.json`: `type` (`flex`, `hpsdr`), `nickname`, model,
  serial, address and what the radio holds (pans, slices, DAX-IQ channels,
  SCUs), written by Discover; and the node settings (spotter ID, grid,
  operator, `rbn_upload`), set by the operator in Setup or `sparkgap.sh setup`.
- `config/<receiver>.local.json`: `radio` (its radio file), band, the receive
  path the radio offers (antenna, RF gain, loops), the receiver's channel,
  decoder and spot rule options; written by Add in SparkGap Web from the shipped
  Flex defaults, or from the config it had before it was removed. The receive
  path keys
  are the operator's startup values; left out, a receiver starts with the
  radio's.

A radio's address is found, not typed: Discover writes it, and a radio that
comes back on another address (DHCP) is found again by its identity and its
file updated. Where broadcasts do not reach SparkGap, give the radio's address
to Setup in SparkGap Web or to `sparkgap.sh setup`, which write it to the radio
file.

## Back ends

Each back end is an adapter behind one interface (`radios.py`). The control
service, the process hub, SparkGap Web and the command line use only the
interface.

- `discover(seconds)`: the radios of this type on the local network, each
  written to its radio file. FlexRadio: its broadcast on UDP 4992 (serial,
  nickname, address). HPSDR: its discovery reply (MAC, model, address).
- **Capabilities**: what a receiver can change, and what runs besides its
  skimmers. FlexRadio: antenna, RF gain and loops, band changed live, and the
  station, stopped when no skimmer of its radio runs. HPSDR: none yet.

Finding a radio that moved stays inside its back end's connection (FlexRadio:
the broadcast with its serial). How receivers group into skimmer processes
(FlexRadio one per receiver, HPSDR one for all) joins the interface with the
HPSDR back end, its first user.

## The control service

One service per machine (`sparkgap_server.py`), two clients: SparkGap Web
and the command line. Every process it runs links to its process hub
(`/process`, loopback only, `docs/SERVER_PROTOCOL.md`):

- `skimmer`, named for the receiver: sends its state, feed port, spots, CPU,
  memory and a `receivers` list (band, centre, receiver); takes `stop` and
  `note`.
- `feeder`, named for its radio: sends its mode, sign-in, posts, spots sent,
  warnings and errors; takes `stop`, `note` and `receivers`.
- `station` (Flex only), named for its radio: sends the radio connection's
  state each heartbeat and as a recovery starts; takes `stop`.

Every process also sends its warnings and errors, which the service puts in
its log with the process's name (`Station Dallas: ...`), and the service logs
each change in a station's state (`Server: radio Dallas reconnecting`).

A link is the service's view of a process: linked means running, a closed
link means it stopped. The service stops a feeder whose skimmers are gone and
a Flex station whose radio has no skimmer left.

## What the operator sees

SparkGap Web's **⚙** opens Radios: each radio on the left with its node,
feeder and receivers, and the server below them; the picked one's settings
on the right. The command line shows the same radio by radio
(`sparkgap.sh status`):

```text
━━ Radio: Connected Dallas ━━  FLEX-6400M  192.168.1.150  1 SCU  pans 1/2
   node     WX7V-5  EM12ou  Chris  rbn upload DRY
   feeder   signed in

   receiver  state    band  kHz     spots  err  warn  up      cpu  mem     build
   Dallas-1  stopped  80m           0      2    1                          f637612
   Dallas-2  running  80m   3538.4  276    0    1     1h 4m   8%   160 MB  f637612
```

The radio's connection reads Connecting, Connected, Reconnecting or Not
connected ([SERVER_SETUP.md](SERVER_SETUP.md#radio-status)).

A receiver's pane is the same on every SDR: waterfall, decoded text by
signal, its settings and its info. Only the controls differ:

- **FlexRadio**: each receiver starts, stops and changes band, antenna and
  RF gain on its own, live. The radio applies antenna, RF gain and loops to
  every pan on the same SCU, and never says which pans share one.
- **HPSDR**: the receivers are the radio's band map, set with the radio, one
  width for all. The operator views receiver `n`; start and stop act on the
  radio.

## Stages

Each stage is live tested before the next.

1. **Names.** Radio, receiver (`<radio>-<n>`) and feeder in SparkGap Web,
   the command line and the logs. The skimmer's link uses them from the start.
2. **The skimmer on the hub.** Its link replaces its pid file, the feed port
   read from its log, and the station's status file.
3. **The radio as the node.** The node settings move to the radio file; one
   feeder per radio, fed by all its receivers (cdub89/SparkGap#33 O4).
4. **The adapter interface.** Discover, find and capabilities behind it,
   Flex first; `discover` asks each back end.
5. **The command line client** for the service, on the same interface.
6. **SparkGap Web** laid out as radio, receivers and feeder (the Settings and
   Info refactor).
7. **HPSDR's adapter,** with the Web-888 work.

## Done so far

- Stages 1 to 6: `<radio>-<n>` names; the skimmer, station and feeder on the
  process hub (station health and feed port from the links); one feeder per
  radio with its node settings in the radio file; `radios.py` with Flex's
  discover and capabilities; `sparkgap_ctl.py`, which `sparkgap.sh` uses for
  start, stop and status; the Radios view in SparkGap Web.
- Still on files: pid files, which the service writes when it starts a
  process and reads to stop one that has no link. Stage 7 (HPSDR) waits for
  the Web-888.

## Adding an IQ back end

A new radio needs one thing to skim: an object that delivers its IQ. The SparkGap Web side
(Discover, capabilities in `radios.py`) is optional and can come later. `rtp_receiver.py`
(ka9q-radio's radiod, for an RX888 and others) is a complete example in about 330 lines;
`flex_iq.py` is a larger one.

### The receiver

A class with these methods, built in `SparkGap.start()` on a new `sdr_type` value, beside
`rtp`, `flex` and the HPSDR default:

- `set_frequency(rx_index, freq_hz)`: called for each band before `start()`, and again on a
  live band change. `rx_index` is the band's place in the config's `bands` list (or its
  `rx_index`).
- `start()`: open the radio or the stream.
- `receive(callback)`: block on SparkGap's receive thread until `stop()`, and call
  `callback(rx_index, iq)` for each packet, `iq` an `(n, 2)` array of I and Q floats at 1.0
  full scale.
- `stop()`: end `receive()` and close.
- Optional: `health()`, a dict the status shows; `restarts()`, called at each health line,
  to log that the radio had to be restarted.

What the IQ has to be:

- **At `sample_rate`.** CW runs at 48, 96 or 192 kHz. FT8 and RTTY need 192 kHz.
- **The right way round.** A signal above the centre must come out at a positive frequency. If
  spots appear mirrored around the centre, negate Q.
- **Whole in time.** Fill a lost packet with zeros rather than skipping it, so a band's time
  line stays whole (FT8's slots, the repeat rule's windows), and count what was filled.
- **Prompt.** Hand packets over as they arrive: aligned FT8 windows take their time from
  arrival. A receiver that buffers seconds of IQ needs `"ft8_windows": "sliding"`.

That is all CW, FT8 and RTTY need: each band's minute for FT8 and RTTY is kept from the same
callback (`_py_minute_add`). Test with real audio from the radio, not synthetic tones: a few
minutes against another skimmer on the same antenna, or against RBN.

### SparkGap Web (optional)

To have the server discover the radio and run its receivers, add a back end to `radios.py`:
`discover(seconds, address)` returning `Found` radios, and its `Capabilities`; see Flex and
Hpsdr there, and the stages above.
