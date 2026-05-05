#!/usr/bin/env python3
"""
telnet_server.py — DX cluster telnet server for CW skimmer spot output.

Provides a standard DX Spider-compatible telnet interface so GTBridge,
HRD, DXLab, or any DX cluster client can connect and receive live CW
spots from the SparkGap pipeline.

Supports standard DX spot format and VE7CC CC11 format.

Based on Grayline's telnet_server.py from the GTBridge project.

Usage:
    # As a module (imported by the skimmer daemon):
    server = SpotTelnetServer(callsign='WF8Z-2', port=7300)
    await server.start()
    server.broadcast_spot(freq_khz=14023.5, dx_call='W1AW', snr=22,
                          wpm=28, mode='CW', comment='CQ')

    # Standalone test:
    python3 telnet_server.py --port 7300 --callsign WF8Z-2
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Dict, Optional

log = logging.getLogger(__name__)


class SpotTelnetServer:
    """Async TCP telnet server that broadcasts CW spots to connected clients."""

    def __init__(self, host: str = '0.0.0.0', port: int = 7300,
                 callsign: str = 'WF8Z', node_call: str = 'SPARK-2',
                 skimmer_suffix: str = '-#', source_tag: str = 'SG',
                 op_name: str = '', qth: str = '', grid: str = '',
                 validation_level: str = 'Normal',
                 skimsrv_version: str = '1.6.0.145'):
        self.host = host
        self.port = port
        self.callsign = callsign  # Operator's callsign (without suffix)
        self.node_call = node_call
        # RBN convention: skimmer-source spots use a "-#" suffix on the
        # spotter call to mark them as machine-generated, distinguishing
        # them from human-submitted DX spots. SkimSrv, SDC Skimmer, and
        # all RBN-feeder skimmers use this. The suffix is what the
        # central RBN server expects when parsing skimmer spots.
        self.skimmer_suffix = skimmer_suffix
        # Trailing tag identifying the spot source (SDC, SkimSrv, SG for
        # SparkGap). Optional but useful for cluster filter rules
        # (e.g. CT1BOH's SKIMVALID can preference specific sources).
        self.source_tag = source_tag
        # SkimSrv impersonation fields. Aggregator's primary-skimmer
        # connection rejects sources that don't match SkimSrv's banner /
        # SETT response shape (it parses "Skimmer Server v.X.Y.Z >= 1.3"
        # from the pre-login banner and expects "SETT: vl<Level> <ranges>"
        # in response to a SKIMMER/SETT query). Spoofing v.1.6.0.145
        # passes the version gate; identifying as SparkGap in the
        # operator slot keeps it honest to a human reading the banner.
        self.op_name = op_name
        self.qth = qth
        self.grid = grid
        self.validation_level = validation_level
        self.skimsrv_version = skimsrv_version
        # Band ranges advertised in SETT response. List of (low_khz,
        # high_khz) tuples. Populated by the daemon after band-meta is
        # built; left empty here so construction order doesn't matter.
        self.bands = []
        self._clients: Dict[asyncio.StreamWriter, dict] = {}
        self._server = None
        self._spot_count = 0

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port,
            reuse_address=True,
        )
        log.info("Spot telnet server on %s:%d (spotter %s, node %s)",
                 self.host, self.port, self.callsign, self.node_call)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for writer in list(self._clients):
            try:
                writer.close()
            except Exception:
                pass
        self._clients.clear()
        log.info("Spot telnet server stopped")

    @property
    def client_count(self):
        return len(self._clients)

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter):
        peer = writer.get_extra_info('peername')
        addr = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        log.info("Client connected: %s", addr)

        try:
            # SkimSrv-shape pre-login banner. Aggregator parses these
            # lines for operator/version metadata. The "Skimmer Server
            # v.X.Y.Z" string must satisfy Aggregator's >=1.3 version
            # check, so we spoof a known-good SkimSrv version verbatim.
            # SparkGap identifies itself in the operator-name slot
            # so a human reading the banner sees what we actually are.
            op_label = self.op_name or 'SparkGap'
            # Banner order is "operated by <NAME>, <CALL>" — Aggregator's
            # parser maps the first field to skimName and the second to
            # skimCall. Reversing this trips the grid-format check
            # because Aggregator then thinks our callsign is the name.
            banner = (
                f"Welcome to the Skimmer Server Telnet cluster port!\r\n"
                f"Skimmer Server v.{self.skimsrv_version} is operated by "
                f"{op_label}, {self.callsign}\r\n"
                f"in {self.qth} ({self.grid})\r\n"
                f"Please enter your callsign:\r\n"
            )
            writer.write(banner.encode())
            await writer.drain()

            try:
                data = await asyncio.wait_for(reader.readline(), timeout=60)
            except asyncio.TimeoutError:
                writer.write(b"Timeout. Goodbye.\r\n")
                await writer.drain()
                writer.close()
                return

            login_call = data.decode('latin-1', errors='replace').strip().upper()
            if not login_call:
                login_call = "UNKNOWN"

            # SkimSrv-style post-login prompt: "<CALL> de SKIMMER
            # YYYY-MM-DD HH:MMZ CwSkimmer >". Aggregator uses the
            # "CwSkimmer" tail to identify the source kind.
            now = datetime.now(timezone.utc)
            ts = now.strftime('%Y-%m-%d %H:%M')
            prompt = f"{login_call} de SKIMMER {ts}Z CwSkimmer >\r\n"
            writer.write(prompt.encode())
            await writer.drain()
            log.info("Client logged in: %s (%s)", login_call, addr)

            self._clients[writer] = {'ve7cc': False, 'call': login_call}

            try:
                while True:
                    data = await reader.readline()
                    if not data:
                        break
                    cmd = data.decode('latin-1', errors='replace').strip()
                    if not cmd:
                        continue

                    parts = cmd.split(None, 1)
                    verb = parts[0].lower() if parts else ''

                    try:
                        if verb == 'skimmer/sett' or verb == 'sett':
                            # SkimSrv response shape:
                            #   SETT: vlNormal 7000.0-7035.0,14000.0-14045.5,...
                            # Aggregator uses this to learn validation
                            # level + scanned bands; if it doesn't get
                            # this back it refuses to forward our spots.
                            if self.bands:
                                ranges = ','.join(
                                    f"{lo:.1f}-{hi:.1f}" for lo, hi in self.bands
                                )
                            else:
                                ranges = '7000.0-7300.0'
                            writer.write(
                                f"SETT: vl{self.validation_level} {ranges}\r\n"
                                f"{prompt}".encode()
                            )
                        elif verb == 'set/ve7cc':
                            self._clients[writer]['ve7cc'] = True
                            writer.write(
                                f"VE7CC gateway mode enabled\r\n{prompt}".encode()
                            )
                        elif verb == 'set/skimmer':
                            writer.write(f"Skimmer mode enabled\r\n{prompt}".encode())
                        elif verb == 'set/ft8' or verb == 'set/ft4':
                            writer.write(f"Mode filter set\r\n{prompt}".encode())
                        elif verb == 'echo' and len(parts) > 1:
                            writer.write((parts[1] + "\r\n" + prompt).encode())
                        elif verb == 'bye' or verb == 'quit':
                            writer.write(b"CU AGN!\r\n")
                            await writer.drain()
                            break
                        elif verb.startswith('sh/'):
                            writer.write(prompt.encode())
                        else:
                            writer.write(prompt.encode())
                        await writer.drain()
                    except Exception:
                        break
            except (asyncio.CancelledError, ConnectionError):
                pass

        except (ConnectionError, OSError) as e:
            log.debug("Client error: %s (%s)", addr, e)
        finally:
            self._clients.pop(writer, None)
            try:
                writer.close()
            except Exception:
                pass
            log.info("Client disconnected: %s", addr)

    def broadcast_spot(self, freq_khz: float, dx_call: str,
                       snr: int = 0, wpm: int = 0,
                       mode: str = 'CW', comment: str = '') -> None:
        """Send a spot to all connected clients in canonical RBN-skimmer
        wire format:

          DX de WF8Z-#:   14033.42  N5TOO          CW   1 dB 27 WPM  CQ  SG  1337Z
                  ^^^^                              ^^^                   ^^
                  spotter+'#' suffix                explicit mode         source tag

        Format matches what SkimSrv, SDC Skimmer, and other RBN-feeder
        skimmers emit. The mode column (CW/FT8/FT4/RTTY/etc) is required
        for RBN-server parsing; it lives between the dx_call and the dB
        figure. The trailing tag (SG = SparkGap) distinguishes our
        source from SkimSrv (no tag) / SDC Skimmer (SDC tag) etc., useful
        for cluster filters that preference by source.

        For non-CW modes, WPM is omitted from the line. For FT8 the
        comment slot carries the decoded message ("CQ AG3I JN18", etc.);
        for CW the slot is "CQ" or "BEACON" or empty.

        Args:
            freq_khz: Frequency in kHz (e.g., 14023.5)
            dx_call: Spotted callsign
            snr: Signal-to-noise ratio in dB
            wpm: CW/RTTY speed in words per minute (ignored for FT8/digital)
            mode: Mode string (CW, RTTY, FT8, FT4, etc.)
            comment: Spot comment / message body (e.g. "CQ" or FT8 message)
        """
        if not self._clients:
            return

        self._spot_count += 1
        now = datetime.now(timezone.utc)
        time_str = now.strftime('%H%M')

        # Spotter call always carries the skimmer suffix
        spotter = self.callsign + self.skimmer_suffix  # e.g. "WF8Z-#"

        # SNR portion: WPM included only for CW/RTTY (modes that have a
        # speed); skipped for FT8/FT4/digital. Sign on dB preserved when
        # present (FT8 spots have +12, -3, etc.; CW skimmer dB is
        # always positive but always-positive formatting still parses).
        # No leading "+" for positive dB (matches SDC/SkimSrv convention).
        # Negative dB (FT8 weak signals) prints with "-" naturally.
        snr_str = f"{int(snr):>3d} dB" if snr else "  0 dB"
        if wpm and mode in ('CW', 'RTTY'):
            # Single space between dB and WPM number — Aggregator's
            # parser is rigid here. Two spaces causes it to drop both
            # SNR and WPM (every spot uploaded as 0 dB / 18 WPM).
            snr_str = f"{snr_str} {int(wpm):>2d} WPM"

        # Body — what was decoded ("CQ" or message). Default to "CQ" if
        # the caller didn't pass a comment.
        body = (comment or 'CQ').strip() or 'CQ'

        # Mode field: 4-char column.
        mode_col = (mode or 'CW').upper()[:5]

        # Standard DX Spider / skimmer-source format
        std_line = (f"DX de {spotter+':':<10s}"
                    f"{freq_khz:9.2f}  "
                    f"{dx_call:<12s}"
                    f" {mode_col:<4s}"
                    f"{snr_str:<14s}"
                    f"  {body:<20s}"
                    f"{self.source_tag:<4s}"
                    f"{time_str}Z\a\r\n")

        # VE7CC CC11 format — caret-separated structured form
        date_str = now.strftime('%d-%b-%Y')
        cc11_comment = f"{snr_str}  {body}"
        cc11_line = (f"CC11^{freq_khz:.1f}^{dx_call}^{date_str}^{time_str}Z^"
                     f"{cc11_comment}^{spotter}^^^{mode_col}^\a\r\n")

        dead = []
        for writer, state in self._clients.items():
            try:
                if state.get('ve7cc'):
                    writer.write(cc11_line.encode())
                else:
                    writer.write(std_line.encode())
            except Exception:
                dead.append(writer)

        for writer in dead:
            self._clients.pop(writer, None)
            try:
                writer.close()
            except Exception:
                pass


async def _test_server():
    """Test mode: run server and generate fake spots every 5 seconds."""
    import random

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(message)s')

    server = SpotTelnetServer(port=7300, callsign='WF8Z-2')
    await server.start()
    print(f'Telnet server running on port 7300. Connect with: telnet localhost 7300')

    test_calls = ['W1AW', 'K5ZD', 'N6TV', 'DK3QN', 'CY0S', 'GB7HQ',
                  'LY0HQ', 'RA3CO', 'OL9HQ', 'SP3DIK']
    test_freqs = [3523.5, 7023.9, 10115.2, 14023.5, 14035.0, 18082.1,
                  21023.5, 24895.0, 28023.5]

    try:
        while True:
            await asyncio.sleep(5)
            call = random.choice(test_calls)
            freq = random.choice(test_freqs)
            snr = random.randint(5, 35)
            wpm = random.choice([15, 20, 25, 28, 30, 35])
            server.broadcast_spot(freq, call, snr=snr, wpm=wpm)
            print(f'  Spot: {freq:.1f} {call} {snr}dB {wpm}WPM '
                  f'({server.client_count} clients)')
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


if __name__ == '__main__':
    try:
        asyncio.run(_test_server())
    except KeyboardInterrupt:
        print('\nStopped')
