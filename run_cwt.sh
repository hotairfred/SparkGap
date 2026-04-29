#!/bin/bash
# Mount NAS if not already mounted.
# Credentials live in ~/.smbcredentials (mode 600), format:
#     username=<user>
#     password=<pass>
mountpoint -q /mnt/atlas/nas || sudo mount -t cifs //192.168.1.200/share /mnt/atlas/nas \
    -o credentials="$HOME/.smbcredentials",uid=1000,gid=1000

pkill -f openskimmer.py
sleep 2
cd /home/fred/csdr-skimmer
timeout 3600 python3 openskimmer.py --config /home/fred/csdr-skimmer/skimmer_cwt.json \
    > /mnt/atlas/nas/skimmer/cwt_live.log 2>&1
