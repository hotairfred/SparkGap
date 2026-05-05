#!/usr/bin/env python3
"""Persistent OpenSkimmer telnet tee. Connects to skimmer1:7300, logs in
as WF8Z, appends every received DX line to /tmp/os_stream.log with a UTC
timestamp prefix. Auto-reconnects on disconnect.

Mirror of sdc_tee.py / rbn_tee.py — gives us a local rolling capture of
our own emitted spots that the hourly scorer can read alongside the SDC
and RBN streams.

Run: nohup python3 os_tee.py > /tmp/os_tee.log 2>&1 &
"""
import socket
import time
from datetime import datetime, timezone

LOG = '/tmp/os_stream.log'
HOST = '192.168.1.76'
PORT = 7300
CALL = b'WF8Z\n'


def stamp():
    return datetime.now(timezone.utc).strftime('%H:%M:%S')


def run():
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(180)
            s.connect((HOST, PORT))
            time.sleep(0.5)
            try: s.recv(8192)  # banner
            except Exception: pass
            s.sendall(CALL)
            with open(LOG, 'a', buffering=1) as fp:
                buf = b''
                while True:
                    chunk = s.recv(8192)
                    if not chunk:
                        break
                    buf += chunk
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        text = line.decode('latin-1', errors='replace').rstrip('\r\x07 \t')
                        if 'DX de' in text:
                            fp.write(f'{stamp()} {text}\n')
        except (socket.error, OSError):
            pass
        finally:
            try: s.close()
            except Exception: pass
        time.sleep(5)


if __name__ == '__main__':
    run()
