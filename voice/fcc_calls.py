#!/usr/bin/env python3
"""Active US amateur calls from the FCC's ULS amateur database, one per line, for voice_spot.py --fcc.

usage: fcc_calls.py l_amat.zip > fcc_active_calls.txt
l_amat.zip is the weekly full file: https://data.fcc.gov/download/pub/uls/complete/l_amat.zip
A call counts when its license record (HD.dat) has status A (active)."""
import io
import sys
import zipfile


def active_calls(path):
    calls = set()
    with zipfile.ZipFile(path) as z, z.open('HD.dat') as f:
        for line in io.TextIOWrapper(f, encoding='latin-1'):
            p = line.split('|')
            if len(p) > 5 and p[5] == 'A' and p[4]:
                calls.add(p[4].strip().upper())
    return calls


if __name__ == '__main__':
    sys.stdout.write(''.join(c + '\n' for c in sorted(active_calls(sys.argv[1]))))
