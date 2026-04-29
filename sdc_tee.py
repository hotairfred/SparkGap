#!/usr/bin/env python3
"""Persistent SDC telnet tee. Connects to 192.168.1.205:7373, logs in as
WF8Z, appends every received line to /tmp/sdc_stream.log with a UTC
timestamp prefix. Auto-reconnects on disconnect.

Run: nohup python3 sdc_tee.py > /tmp/sdc_tee.log 2>&1 &
Cross-reference: grep 'HH:MM' /tmp/sdc_stream.log | grep '14[0-9]\\{3\\}\\.'
"""
import socket
import time
import sys
from datetime import datetime, timezone

LOG = '/tmp/sdc_stream.log'
HOST = '192.168.1.205'
PORT = 7373
CALL = b'WF8Z\n'

def stamp():
    return datetime.now(timezone.utc).strftime('%H:%M:%S')

def run():
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # SDC has consistent ~126s silent gaps during quiet hours; 120s
            # timeout was churn-disconnecting. 600s rides through them.
            s.settimeout(600)
            s.connect((HOST, PORT))
            time.sleep(0.5)
            try: s.recv(8192)  # banner
            except: pass
            s.sendall(CALL)
            with open(LOG, 'a', buffering=1) as fp:
                fp.write(f"# {stamp()} CONNECTED\n")
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
