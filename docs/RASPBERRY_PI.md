# Raspberry Pi: build and install

SparkGap runs headless on a Raspberry Pi 4 as an RBN node: six Web-888
receivers at 192 kHz, the itila2 chain, the Gated Rule and RBN upload. The
server and receivers are set up as on any Linux machine
([SERVER_SETUP.md](SERVER_SETUP.md)); this page covers what differs on a Pi.

## What you need

- A **Raspberry Pi 4, 4 GB**. A Pi 3 (1 GB) builds and runs SparkGap but is
  too small for several bands: a six-band process uses 600 to 850 MB.
- The **official 5.1 V, 3 A supply and its own cable**. A thin USB-C cable
  drops the voltage under load: the Pi then warns of undervoltage every few
  minutes and slows its CPU.
- A **vented case**, or heatsinks and a fan. In a closed case the SoC ran at
  73 C with six bands, 7 C under the point where it slows down.
- **Wired Ethernet.** Six receivers at 192 kHz send about 55 Mbit/s of UDP.
- A microSD card of 32 GB or more.

## 1. The OS

Write **Raspberry Pi OS Lite (64-bit)**, Debian 13 (Trixie), with Raspberry
Pi Imager. In its settings set the hostname, your user, an SSH key and the
time zone. No desktop is needed: SparkGap Web runs in a browser on another
machine.

## 2. The clock

A Pi has no battery-backed clock. Before installing anything, check that
`timedatectl` shows `System clock synchronized: yes` (apt refuses packages
"not live until" a future date otherwise; `sudo timedatectl set-ntp true`
turns sync on).

So that SparkGap waits for the clock at every boot:

```text
sudo systemctl enable systemd-time-wait-sync
```

Without it, after a power cut SparkGap can start minutes before the clock is
set, and its first spots carry the wrong time.

## 3. Packages

```text
sudo apt-get install -y git build-essential libfftw3-dev \
  python3-numpy python3-scipy python3-paho-mqtt python3-pytest
```

Debian's packages are recent enough; no venv is needed.

## 4. Set up and build

```text
git clone https://github.com/hotairfred/SparkGap.git ~/SparkGap
~/SparkGap/tools/sparkgap.sh setup
cd ~/sparkgap/code && make native ARCH=native
```

`setup` makes the run folder `~/sparkgap` with its own copy of the code (see
[SERVER_SETUP.md](SERVER_SETUP.md)). The libraries in the repository are
built for x86-64: `make native ARCH=native` rebuilds them for the Pi in under
a minute. `make all` also tries `libcw_dispatcher.so`, which needs an
x86-only library and fails here; the itila2 chain doesn't use it.

The RBN auth library is fetched for the Pi's own machine type (aarch64) when
the feeder starts.

Then add the radio's receivers in SparkGap Web.

## 5. Start on boot

```ini
# /etc/systemd/system/sparkgap.service
[Unit]
Description=SparkGap server and receivers
Wants=network-online.target
After=network-online.target time-sync.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=<user>
WorkingDirectory=/home/<user>/sparkgap
ExecStart=/home/<user>/sparkgap/sparkgap.sh start
ExecStop=/home/<user>/sparkgap/sparkgap.sh stop
TimeoutStartSec=300
TimeoutStopSec=120

[Install]
WantedBy=multi-user.target
```

```text
sudo systemctl daemon-reload
sudo systemctl enable --now sparkgap
```

At boot `sparkgap.sh start` starts the server and the receivers that were
running before, including after a power cut. A receiver gets up to 120 s to
go live; `TimeoutStartSec=300` leaves room for that and the feeders, since
systemd stops everything the unit started when the start times out.

Shut down with `sudo poweroff`, which stops SparkGap first, rather than
pulling the power: it protects the SD card.

## 6. Updating

The rebuilt libraries show as changed files in `~/sparkgap/code`. Put them
back before pulling, then rebuild and restart:

```text
cd ~/sparkgap/code
git checkout -- '*.so' && git pull
make native ARCH=native
~/sparkgap/sparkgap.sh restart
```

## What to expect

A Pi 4 with six Web-888 receivers at 192 kHz (80 m to 10 m and the 10 m
beacons), RBN upload on:

| | |
| --- | --- |
| SparkGap CPU | 170 to 210% of one core (of 400%) |
| Load average | 3 to 4 at busy times |
| Memory | 600 to 850 MB; 2.5 GB or more left free |
| SoC temperature | 45 to 50 C vented, 73 C in a closed case |

Its spots score the same against RBN as the same radio on an x86 machine.
The headroom is modest: more bands, or more receivers, need a faster machine.

## When something is wrong

- **Undervoltage or slowdowns:** `vcgencmd get_throttled` should read
  `throttled=0x0`. Any other value: change the supply or its cable
  (`journalctl -k | grep -i voltage` shows when it happened).
- **Too hot:** `vcgencmd measure_temp`. Above 75 C, vent the case.
- **Spots with the wrong time after a power cut:** step 2,
  `systemd-time-wait-sync`.
