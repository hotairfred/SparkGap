# SparkGap Console (experimental)

SparkGap Web shows what a running SparkGap hears, in a browser: a live waterfall
of the receiver's span, the calls on a frequency ladder, and the decoded text of
the signal you pick. SparkGap stays an independent service next to the radio;
the page connects to it over the network and can come and go without disturbing
decoding. One page can watch several SparkGaps.

Status: experimental. The page only watches: each SparkGap keeps its own
config, startup and shutdown, and Settings shows its config read-only.

## Server: the feed

The feed is off unless the config has a `gui_feed` block:

```json
"gui_feed": {"bind": "127.0.0.1", "port": 7320, "join_code": null, "web": false}
```

- `bind`: the address to listen on. `127.0.0.1` serves only the same machine. To
  watch from other machines, use the IP address of the machine SparkGap runs on.
  For remote access, use a VPN that encrypts the link (such as Tailscale).
- `port`: 7320 by default.
- `join_code`: required whenever `bind` is not a loopback address. Six
  characters, minted without any pair easily confused by eye or over the air
  (O/0, I/1 and similar); case, spaces and dashes are ignored on entry. Make one
  with `python3 feed_server.py --new-code`. SparkGap logs the code at startup:

  ```text
  GUI feed on ws://192.168.1.50:7320/feed, join code XXX-XXX
  ```

- Five wrong codes from one address lock that address out for 10 minutes. A
  missing code is refused but not counted.
- At most 16 consoles at once; one that stops reading is disconnected.
- `web`: `true` also serves SparkGap Web on the same port (see below).
- `tls_cert` and `tls_key` (optional) serve `wss://`; without them the feed is
  not encrypted.

What the feed sends is described in [GUI_FEED_PROTOCOL.md](GUI_FEED_PROTOCOL.md).

The feed works in live and file mode and needs no extra Python packages on the
server. Its spectrum costs under 1% of a core, and nothing while no client is
connected.

## SparkGap Web

Nothing to install. Set `"web": true` in the `gui_feed` block and open the
address SparkGap logs at startup:

```text
GUI feed: browser console at http://192.168.1.50:7320/
```

In Settings, under CONNECTIONS, add the server with its join code (the address
and port are filled in from the page). A server added without a port is
searched for on ports 7320 to 7339, and every SparkGap found there is added.
The list, theme and demo setting are kept in that browser, separately for each
address the page is opened from.

Demo mode shows made-up signals, so the page can be tried without a server; it
is on until a server has been added. Turning it on only switches the view: the
servers stay connected and keep their waterfall, spots and text, and the server
list is hidden until demo is off again.

The page also opens from a file (`web/index.html`), for demo mode or for
servers that do not serve it. A server without a join code accepts a page only
when the page itself was opened from a loopback address on the feed's port.

## Using it

- Drag or two-finger drag pans the waterfall; Ctrl+scroll or a pinch zooms; the
  scroll bar beside it moves through the receiver's span.
- The panel under the decoded text: Zoom sets the width shown (its button steps
  through Full, Half and Quarter of the receiver), Span is the range on screen,
  and SNR is the waterfall's colour scale.
- Click a trace or a call to see its decoded windows; for a spotted call the
  frequency, speed and SNR are the ones in its spot. Ctrl+F searches calls,
  decoded text and frequencies; a frequency is taken as typed. Decoded text can
  be selected and copied.
- A SparkGap running several bands, or several SparkGaps, give a band picker in
  the header; a search for a call or frequency on another band switches to it.
- Settings, DECODED: each window's readings (Both, Wide, Narrow, or the one
  with the Lowest cost, the default).
- The status page (ⓘ in the header) shows the radio, SparkGap's run (config
  and log files, recording, clock), decoding counts, spot output, the connected
  consoles, CPU and memory, and SparkGap's last warnings and errors.
