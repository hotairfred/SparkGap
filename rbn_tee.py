#!/usr/bin/env python3
"""Persistent RBN telnet tee. Connects to telnet.reversebeacon.net:7000,
logs in as WF8Z, appends every line to /tmp/rbn_stream.log with a UTC
timestamp prefix. Auto-reconnects on disconnect.

Run: nohup python3 rbn_tee.py > /tmp/rbn_tee.log 2>&1 &
"""
import socket
import time
from datetime import datetime, timezone

LOG = '/tmp/rbn_stream.log'
HOST = 'telnet.reversebeacon.net'
PORT = 7000
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
            except: pass
            s.sendall(CALL)
            with open(LOG, 'a', buffering=1) as fp:
                fp.write(f"# {stamp()} CONNECTED to {HOST}:{PORT}\n")
                buf = b''
                while True:
                    chunk = s.recv(8192)
                    if not chunk:
                        fp.write(f"# {stamp()} DISCONNECT (EOF)\n")
                        break
                    buf += chunk
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        text = line.decode('ascii', errors='replace').rstrip('\r')
                        if text:
                            fp.write(f"{stamp()} {text}\n")
            s.close()
        except Exception as e:
            with open(LOG, 'a', buffering=1) as fp:
                fp.write(f"# {stamp()} ERR {e}\n")
            time.sleep(5)

if __name__ == '__main__':
    run()
