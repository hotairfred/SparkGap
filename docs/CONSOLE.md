# SparkGap Console and SparkGap Web (experimental)

Two views of a running SparkGap: the desktop console (Windows, Linux, macOS)
and SparkGap Web, the same console in a browser with nothing to install. Both
show a live waterfall of the receiver's span, the calls on a frequency ladder,
and the decoded text of the signal you pick. SparkGap stays an independent
service next to the radio; the consoles connect to it over the network, come and
go without disturbing decoding, and each can watch several SparkGaps.

![SparkGap Web, 40m from a FLEX-6600](web.png)

Status: experimental. The consoles only watch: each SparkGap keeps its own
config, startup and shutdown, and Settings shows its config read-only.

## Getting connected

1. In SparkGap's config, add a `gui_feed` block with `"web": true`, where
   `<host-ip>` is the IP address of the machine SparkGap runs on, `<port>`
   the feed's port (7320 unless you pick another) and `XXX-XXX` your own join
   code (details under
   [Server: the feed](#server-the-feed)):

   ```json
   "gui_feed": {"bind": "<host-ip>", "port": <port>,
                "join_code": "XXX-XXX", "web": true}
   ```

2. Start SparkGap. It logs the browser address and the join code:

   ```text
   GUI feed on ws://<host-ip>:<port>/feed, join code XXX-XXX
   GUI feed: browser console at http://<host-ip>:<port>/
   ```

3. Open that address in a browser, or start the desktop console.
4. Open Settings (⚙), choose **+ Add server**, and enter the address and the
   join code. Leave the port blank to find every SparkGap on that machine
   (ports 7320 to 7339); each one that accepts the code is added.

A console with no servers starts in demo mode, with made-up signals, so it can
be tried before any server is set up.

## The main screen

**Header**, left to right:

- **Search** (Ctrl+F): a call, decoded text or a frequency in kHz. A frequency
  is taken as typed; a match on another band switches to it.
- **Band picker**: shown when there is a choice, from one SparkGap running
  several bands or several SparkGaps in the list. Each entry is the host name and
  band.
- **State**: Live, Connecting, or why the feed is down; DEMO MODE in demo.
- **⚙** Settings, **◐** light or dark theme, **ⓘ** the status page.

**Waterfall and call ladder.** The waterfall is the receiver's whole span. The
ladder above it marks each signal: spotted calls, calls heard but not spotted,
and signals with no call yet, by frequency. Click a trace or a ladder entry to
pick that signal.

- Drag or two-finger drag pans; Ctrl+scroll or a pinch zooms.
- The scroll bar beside the waterfall moves through the span.

**Decoded windows**, on the right: the picked signal's frequency, speed and SNR,
whether it was SPOTTED or only HEARD, and its decoded windows, newest first.
Text can be selected, and **Copy** copies all of the signal's decoded windows.

**Zoom panel**, under the decoded text: **Zoom** sets the width shown (the
button steps through Full, Half and Quarter of the span), **Span** is the range
on screen, and **SNR** is the waterfall's colour scale.

**Bottom bar**: SparkGap's load (percent of one core, a 60 s average), memory,
open decoder bins, connected consoles, decoder, build and uptime.

## The status page

ⓘ opens it; it updates every 2 seconds.

- **Health line**, on top: green when the radio is live, decoding keeps up and
  there are no errors; otherwise the problems, worst first.
- **Tiles**: uptime, spots (and in the last hour), calls, decoded windows, bins
  (and the peak), CPU and memory.
- **RADIO**: type, address and state, plus what the receiver reports, such as
  packets and packets lost.
- **SPARKGAP**: host and the port this console is connected on, the join code
  (masked; click to show and copy it), decoder, bands, config and log files,
  recording, and IQ trimmed (above zero means the machine is short of CPU).
- **SPOT OUTPUT**: the telnet port and how many clients are on it.
- **CONSOLES**: this console, and every console connected to this SparkGap by
  host name (from reverse DNS) with its address and connect time.
- **PROBLEMS**: warnings and errors since start, with the last five.
- **Footer**: SparkGap's build and whether its clock is NTP synchronised.

Selecting text holds the page still until the selection is cleared, so it can
be copied.

## Settings

- **DEMO**: made-up signals instead of your servers. Demo only switches the
  view: the servers stay connected and keep their waterfall, spots and text.
  Picking, adding, editing or removing a server turns demo off.
- **DECODED**: which readings of each window are shown: Both, Wide, Narrow, or
  the one with the Lowest cost (the default).
- **CONNECTIONS**: the servers this console watches, kept between runs. Click
  one to show it; Edit or ✕ to change or remove it.
- **SPOT RULE, VALIDATION, CALL SOURCE**: read-only, from the selected server's
  config.
- Help, a link to this guide, and the console's build, at the bottom.

The desktop console keeps its window size, position and theme between runs. A
browser keeps the server list, theme and demo setting for each address the page
is opened from.

## Tips

- Several SparkGaps on one host each need their own `port`. The status page's
  Host row shows which port, so which instance, you are looking at.
- For access from outside the LAN, use a VPN that encrypts the link rather than
  opening the port to the internet. Without `tls_cert`, the feed and its join
  code cross the network unencrypted.
- A console that falls behind is listed under CONSOLES and on the health line;
  the desktop console draws with the software renderer, which costs least over
  remote desktop.

## Server: the feed

The feed is off unless the config has a `gui_feed` block:

```json
"gui_feed": {"bind": "127.0.0.1", "port": 7320, "join_code": null, "web": false}
```

- `bind`: the address to listen on. `127.0.0.1` serves only the same machine. To
  watch from other machines, use the IP address of the machine SparkGap runs on.
  For remote access, use a VPN that encrypts the link.
- `port`: 7320 by default.
- `join_code`: required whenever `bind` is not a loopback address. Six
  characters, minted without any pair easily confused by eye or over the air
  (O/0, I/1 and similar); case, spaces and dashes are ignored on entry. Make one
  with `python3 feed_server.py --new-code`. SparkGap logs the code at startup:

  ```text
  GUI feed on ws://<host-ip>:<port>/feed, join code XXX-XXX
  ```

- Five wrong codes from one address lock that address out for 10 minutes. A
  missing code is refused but not counted.
- At most 16 consoles at once; one that stops reading is disconnected.
- `web`: `true` also serves SparkGap Web on the same port (see below).
- `spots`: which spots the console shows. `"rbn"` (default) is what `telnet_port`
  carries, the stream the RBN feeder reads; `"local"` is what `telnet_local_port` carries,
  with its own `local_spot_rule`.
- `tls_cert` and `tls_key` (optional) serve `wss://`; without them the feed is
  not encrypted. Settings adds servers as `ws://`, so give a `wss://` server on
  the command line.

What the feed sends is described in [GUI_FEED_PROTOCOL.md](GUI_FEED_PROTOCOL.md).

The feed works in live and file mode and needs no extra Python packages on the
server. With one or two consoles connected it costs about 5% of a core (96 kHz
on a Ryzen 5 5500U), and nothing while no client is connected.

## Console: running from source

Python 3.10 or later, from the `console` folder:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python sparkgap_console.py
```

On Windows, `run_console.bat` does the same on first run.

Add servers in Settings, under CONNECTIONS: address, port and join code. The
list is kept between runs. Servers given on the command line are added to it:

```sh
.venv/bin/python sparkgap_console.py ws://<host-ip>:<port>/feed#XXX-XXX
```

The console draws with Qt's software renderer, which costs far less CPU over
remote desktop. `--gpu` uses the graphics card instead.

## Console: Windows build

On Windows, from the `console` folder, with the venv above:

```bat
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller --noconfirm SparkGapConsole.spec
```

The result is `dist\SparkGapConsole`, a folder of about 125 MB with
`SparkGapConsole.exe`. It runs on any Windows PC without Python; copy the whole
folder. Settings shows the build's commit when a `build.txt` holding it sits
next to the spec at build time.

## SparkGap Web

The same console in a browser, with nothing to install. Set `"web": true` in
the `gui_feed` block and open the address SparkGap logs at startup:

```text
GUI feed: browser console at http://<host-ip>:<port>/
```

Settings works as in the desktop console: add the server with its join code
(the address and port are filled in from the page). The list, theme and demo
setting are kept in that browser, separately for each address the page is
opened from.

The page also opens from a file (`web/index.html`), for demo mode or for
servers that do not serve it. A server without a join code accepts a page only
when the page itself was opened from a loopback address on the feed's port.

## Keeping the two clients in step

The desktop console (`console/`) and SparkGap Web (`web/`) are two builds of one
design. A change to either lands in the other in the same commit, or this list
says why not.

Shared, and kept the same:

- Feed handling: the wide reading of each window on top unless it has no fit;
  a wrong join code is not retried; a lockout is retried after 10 minutes;
  reconnects back off over 1, 2, 5, 10 and 20 seconds; a change in the
  spectrum's bins, start, bin width or dB step clears the waterfall.
- The constants at the top of `console/backend.py` and `web/app.js`: same names,
  same values.
- Layout, theme tokens (`t` in `Main.qml`, `:root` in `style.css`), the
  waterfall palette (dark in both themes), header glyphs and fonts.
- Call ladder, bubbles, decoded windows, search, status bar, band picker,
  status page, and the zoom panel (Zoom, Span and SNR rows).
- Settings: demo, decoded readings, connections (a server added without a port
  is searched for on ports 7320 to 7339), the read-only spot rule, validation and
  call source (shown when the server reports them), the build, at 480 px.
- Demo mode: the same made-up signals; turning it on only switches the view.
- Scroll bars: thin, with small arrows, the same on the waterfall and the
  decoded text.
- Server names: host name and band; the address appears only in Settings.

Different on purpose:

- Desktop only: window size and position kept between runs; Settings scrolls
  inside its window (the browser page scrolls).
- Web only: the layout for narrow (phone) and short screens, where the page
  scrolls, and touch pan and pinch; the page fills in its own server's address
  when one is added.
- Refusals: the desktop console reads the HTTP status, the browser the close
  code. Both show the same message.
