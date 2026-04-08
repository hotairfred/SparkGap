#!/bin/bash
# Mount NAS if not already mounted
mountpoint -q /mnt/atlas/nas || sudo mount -t cifs //192.168.1.200/share /mnt/atlas/nas \
    -o username=claude,password=***REDACTED***,uid=1000,gid=1000

pkill -f openskimmer.py
sleep 2
cd /home/fred/csdr-skimmer
timeout 3600 python3 openskimmer.py --config /home/fred/csdr-skimmer/skimmer_cwt.json \
    > /mnt/atlas/nas/skimmer/cwt_live.log 2>&1
