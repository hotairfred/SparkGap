#!/usr/bin/env python3
"""
telnet_server.py — DX cluster telnet server for CW skimmer spot output.

Provides a standard DX Spider-compatible telnet interface so GTBridge,
HRD, DXLab, or any DX cluster client can connect and receive live CW
spots from the OpenSkimmer pipeline.

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
                 callsign: str = 'WF8Z-2', node_call: str = 'SPARK-2'):
        self.host = host
        self.port = port
        self.callsign = callsign  # Spotter callsign (appears in DX de)
        self.node_call = node_call
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
            # DX Spider login prompt
            writer.write(b"login: Please enter your call: ")
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

            # Welcome
            writer.write(
                f"Hello {login_call}, this is {self.node_call} running OpenSkimmer\r\n"
                f"Spotter: {self.callsign}\r\n"
                f"Spots delivered: {self._spot_count}\r\n"
                f"{login_call} de {self.node_call} >\r\n".encode()
            )
            await writer.drain()
            log.info("Client logged in: %s (%s)", login_call, addr)

            self._clients[writer] = {'ve7cc': False, 'call': login_call}
            prompt = f"{login_call} de {self.node_call} >\r\n"

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
                        if verb == 'set/ve7cc':
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
                            writer.write(b"73 de OpenSkimmer\r\n")
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
        """Send a spot to all connected clients.

        Args:
            freq_khz: Frequency in kHz (e.g., 14023.5)
            dx_call: Spotted callsign
            snr: Signal-to-noise ratio in dB
            wpm: CW speed in words per minute
            mode: Mode string (CW, RTTY, FT8, etc.)
            comment: Spot comment
        """
        if not self._clients:
            return

        self._spot_count += 1
        now = datetime.now(timezone.utc)
        time_str = now.strftime('%H%M')

        # Build comment with SNR and WPM
        if not comment:
            parts = []
            if snr:
                parts.append(f'{int(snr)} dB')
            if wpm:
                parts.append(f'{wpm} WPM')
            if mode and mode != 'CW':
                parts.append(mode)
            parts.append('CQ')
            comment = '  '.join(parts)

        # Standard DX Spider format
        spotter = (self.callsign + ':')[:10]
        std_line = (f"DX de {spotter:<10s}{freq_khz:10.1f}  "
                    f"{dx_call:<12s} {comment:<28s}{time_str}Z\a\r\n")

        # VE7CC CC11 format
        date_str = now.strftime('%d-%b-%Y')
        cc11_line = (f"CC11^{freq_khz:.1f}^{dx_call}^{date_str}^{time_str}Z^"
                     f"{comment}^{self.callsign}^^^0^\a\r\n")

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
