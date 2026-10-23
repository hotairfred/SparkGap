"""A FlexRadio stand-in for flex_iq tests: clients, pans, slices and DAX-IQ streams as the
FLEX-6400M showed them on 2026-10-22.  Ids are the lowest free (the radio reuses them), a
station's pans go with its GUI client, and a GUI client's saved pans come back when the
same client id registers again unless persistence is off."""

from dataclasses import dataclass, field

import pytest

import flex_iq

PAN_BASE = 0x4000_0000
STREAM_BASE = 0x2000_0000


@dataclass
class Client:
    handle: int
    client_id: str = ""
    program: str = ""
    station: str = ""
    gui: bool = False
    bound_to: int | None = None
    persistence: bool = True


@dataclass
class Pan:
    owner: int
    center: float = 14.1
    channel: int = 0


@dataclass
class Slice:
    pan: int
    owner: int
    freq: float = 14.1
    locked: bool = False


@dataclass
class Stream:
    channel: int
    client: int
    gui: int
    pan: int = 0


@dataclass
class FakeRadio:
    nickname: str = "Dallas"
    max_pans: int = 2
    gui_slots: int = 2
    clients: dict[int, Client] = field(default_factory=dict)
    pans: dict[int, Pan] = field(default_factory=dict)
    slices: dict[int, Slice] = field(default_factory=dict)
    streams: dict[int, Stream] = field(default_factory=dict)
    saved: dict[str, list[tuple[float, int]]] = field(
        default_factory=dict
    )  # client id -> (center, channel)
    refuse: dict[str, str] = field(default_factory=dict)  # command prefix -> error code
    silent: set[str] = field(default_factory=set)  # command prefixes the radio never answers
    down: int = 0  # an outage: the next this many commands and connects get nothing
    unanswered: dict[str, int] = field(default_factory=dict)  # prefix -> times it goes unanswered
    slices_locked: bool = False  # new slices come up locked (the radio keeps a band's lock)
    conns: list["FakeConnection"] = field(default_factory=list)
    log: list[tuple[int, str]] = field(default_factory=list)  # (client handle, command)
    _next_handle: int = 0x1000_0001

    def connect(self) -> "FakeConnection":
        return FakeConnection(self)

    def new_handle(self) -> int:
        h = self._next_handle
        self._next_handle += 0x0101_0101
        return h

    def gui_of(self, handle: int) -> int:
        c = self.clients[handle]
        return handle if c.gui else (c.bound_to or handle)

    def broadcast(self, line: str) -> None:
        for conn in self.conns:
            conn.queue.append(line)

    def drop(self, handle: int) -> None:
        """A client leaves: its streams go; a GUI client's pans and slices go with it."""
        c = self.clients.pop(handle, None)
        if c is None:
            return
        for sid in [s for s, st in self.streams.items() if st.client == handle]:
            del self.streams[sid]
        if c.gui:
            mine = [p for p, pan in self.pans.items() if pan.owner == handle]
            if c.persistence:
                self.saved[c.client_id] = [
                    (self.pans[p].center, self.pans[p].channel) for p in mine
                ]
            for p in mine:
                self.remove_pan(p)
        self.broadcast(f"S0|client 0x{handle:08X} disconnected forced=0")

    def remove_pan(self, p: int) -> None:
        self.pans.pop(p, None)
        for n in [n for n, s in self.slices.items() if s.pan == p]:
            del self.slices[n]

    def new_pan(self, owner: int, center: float = 14.1, channel: int = 0) -> int | None:
        if len(self.pans) >= self.max_pans:
            return None
        p = next(PAN_BASE + i for i in range(16) if PAN_BASE + i not in self.pans)
        self.pans[p] = Pan(owner, center, channel)
        n = next(i for i in range(8) if i not in self.slices)  # the band-default slice
        self.slices[n] = Slice(p, owner, center, locked=self.slices_locked)
        return p

    def objects(self) -> dict[str, object]:
        return {
            "pans": {hex(p): (pan.center, pan.channel) for p, pan in self.pans.items()},
            "slices": {n: (hex(s.pan), s.freq) for n, s in self.slices.items()},
            "streams": {hex(s): (st.channel, hex(st.pan)) for s, st in self.streams.items()},
        }

    def status(self, what: str) -> list[str]:
        if what == "client":
            return [
                f"S0|client 0x{h:08X} connected client_id={c.client_id} "
                f"program={c.program} station={c.station}"
                for h, c in self.clients.items()
                if c.gui
            ]
        if what == "pan":
            return [
                f"S0|display pan 0x{p:08X} client_handle=0x{pan.owner:08X} center={pan.center:.6f} "
                f"daxiq_channel={pan.channel}"
                for p, pan in self.pans.items()
            ]
        if what == "slice":
            return [
                f"S0|slice {n} in_use=1 RF_frequency={s.freq:.6f} "
                f"client_handle=0x{s.owner:08X} pan=0x{s.pan:08X}"
                for n, s in self.slices.items()
            ]
        if what == "daxiq":
            return [
                f"S0|stream 0x{s:08X} type=dax_iq daxiq_channel={st.channel} pan=0x{st.pan:08X} "
                f"client_handle=0x{st.client:08X} client_gui_handle=0x{st.gui:08X}"
                for s, st in self.streams.items()
            ]
        return []

    def run(self, handle: int, cmd: str) -> tuple[str, str, list[str]]:
        """(code, text, status lines) for one command from client `handle`."""
        self.log.append((handle, cmd))
        for prefix, code in self.refuse.items():
            if cmd.startswith(prefix):
                return code, "", []
        c = self.clients[handle]
        words = cmd.split()
        kv = dict(w.split("=", 1) for w in words if "=" in w)
        if cmd == "info":
            return "0", f'model="FLEX-6400M",name="{self.nickname}",callsign="N0CALL"', []
        if cmd.startswith("sub "):
            return "0", "", self.status(words[1])
        if cmd.startswith("client program"):
            c.program = words[2]
            return "10000002", "unknown client program", []
        if cmd == "client start_persistence off":
            c.persistence = False
            return "0", "", []
        if cmd.startswith("client gui "):
            cid = words[2]
            for other in [
                h for h, o in self.clients.items() if o.gui and o.client_id == cid and h != handle
            ]:
                self.evict(other)
            if sum(o.gui for o in self.clients.values()) >= self.gui_slots:
                return "50000016", "Unable to create GUI client", []
            c.gui, c.client_id = True, cid
            restored = self.saved.pop(cid, []) if c.persistence else []
            for center, channel in restored:
                self.new_pan(handle, center, channel)
            if not restored:
                self.new_pan(handle)  # a new GUI client's default pan and slice
            return "0", cid, []
        if cmd.startswith("client station "):
            c.station = words[2]
            return "0", "", []
        if cmd.startswith("client bind "):
            gui = next(
                (h for h, o in self.clients.items() if o.gui and o.client_id == kv["client_id"]),
                None,
            )
            if gui is None:
                return "500000A1", "no such client", []
            c.bound_to = gui
            return "0", "", []
        if cmd.startswith("display pan create"):
            p = self.new_pan(self.gui_of(handle))
            return ("0", f"0x{p:08X},0x{p + 0x0200_0000:08X}", []) if p else ("50000010", "", [])
        if cmd.startswith("display pan set "):
            pan = self.pans.get(int(words[3], 16))
            if pan is None:
                return "50000005", "", []
            pan.center = float(kv.get("center", pan.center))
            pan.channel = int(kv.get("daxiq_channel", pan.channel))
            for st in self.streams.values():
                if "daxiq_channel" in kv and st.channel == pan.channel and st.client == handle:
                    st.pan = int(words[3], 16)
            return "0", "", []
        if cmd.startswith("display pan remove "):
            p = int(words[3], 16)
            if p not in self.pans:
                return "50000005", "", []
            self.remove_pan(p)
            return "0", "", []
        if cmd.startswith(("slice lock ", "slice unlock ")):
            s = self.slices.get(int(words[2]))
            if s is None:
                return "50000005", "", []
            s.locked = words[1] == "lock"
            return "0", "", []
        if cmd.startswith("slice tune "):
            s = self.slices.get(int(words[2]))
            if s is None:
                return "50000005", "", []
            if s.locked:
                return "50000068", "Unable to tune a locked slice -- unlock first", []
            s.freq = float(words[3])
            self.pans[s.pan].center = s.freq  # the pan follows its slice (2026-10-22 11:50Z)
            return "0", "", []
        if cmd.startswith("slice set "):
            return ("0", "", []) if int(words[2]) in self.slices else ("50000005", "", [])
        if cmd.startswith("slice remove "):
            return ("0", "", []) if self.slices.pop(int(words[2]), None) else ("50000005", "", [])
        if cmd.startswith("slice create "):
            n = next(i for i in range(8) if i not in self.slices)
            self.slices[n] = Slice(int(kv["pan"], 16), self.gui_of(handle), float(kv["freq"]))
            return "0", str(n), []
        if cmd.startswith("stream create "):
            sid = next(STREAM_BASE + i for i in range(16) if STREAM_BASE + i not in self.streams)
            self.streams[sid] = Stream(int(kv["daxiq_channel"]), handle, self.gui_of(handle))
            return "0", f"{sid:08X}", []
        if cmd.startswith("stream remove "):
            return (
                ("0", "", []) if self.streams.pop(int(words[2], 16), None) else ("50000005", "", [])
            )
        return "0", "", []

    def drop_all(self) -> None:
        """The radio's keepalive ran out on everyone (an outage over 15 s): every client goes."""
        for conn in list(self.conns):
            conn.lost = "The radio closed the control connection"
            self.conns.remove(conn)
            self.drop(int(conn.handle, 16))

    def unreachable(self) -> bool:
        if self.down > 0:
            self.down -= 1
            return True
        return False

    def evict(self, handle: int) -> None:
        for conn in self.conns:
            if conn.handle == f"{handle:08X}":
                conn.lost = "The radio closed the control connection"
        self.drop(handle)


class FakeConnection(flex_iq.FlexConnection):
    """flex_iq.FlexConnection over a FakeRadio instead of a socket."""

    def __init__(self, radio: FakeRadio, ip: str = "192.0.2.1", port: int = 4992) -> None:
        super().__init__(ip, port)
        self.radio = radio
        self.queue: list[str] = []

    def open(self) -> list[str]:
        if self.radio.unreachable():
            raise OSError(113, "No route to host")
        h = self.radio.new_handle()
        self.radio.clients[h] = Client(h)
        self.radio.conns.append(self)
        self.handle = f"{h:08X}"
        return [f"H{self.handle}"]

    def cmd(self, cmd: str, timeout: float = 2.0, settle: float = 0.0) -> list[str]:
        self.seq += 1
        if self.lost or int(self.handle, 16) not in self.radio.clients:
            self.lost = self.lost or "The radio closed the control connection"
            return []
        if self.radio.unreachable() or any(cmd.startswith(p) for p in self.radio.silent):
            self.missed += 1
            return []
        for prefix, n in self.radio.unanswered.items():
            if n and cmd.startswith(prefix):
                self.radio.unanswered[prefix] = n - 1
                self.missed += 1
                return []
        code, text, status = self.radio.run(int(self.handle, 16), cmd)
        lines, self.queue = self.queue + status, []
        return lines + [f"R{self.seq}|{code}|{text}"]

    def poll(self) -> list[str] | None:
        if self.lost:
            return None
        lines, self.queue = self.queue, []
        return lines

    def close(self, reason: str) -> None:
        if self in self.radio.conns:
            self.radio.conns.remove(self)
            self.radio.drop(int(self.handle, 16))

    def cut(self) -> None:
        """The control connection breaks without a goodbye (a LAN drop)."""
        self.lost = "Control connection error: [Errno 104] Connection reset by peer"
        self.close("cut")


class _Udp:
    packets: list[bytes] = []  # what recvfrom hands out, oldest first

    def setsockopt(self, *_: object) -> None:
        pass

    def settimeout(self, *_: object) -> None:
        pass

    def recvfrom(self, *_: object) -> tuple[bytes, tuple[str, int]]:
        if not self.packets:
            raise TimeoutError
        return self.packets.pop(0), ("192.0.2.1", 4991)

    def bind(self, *_: object) -> None:
        pass

    def close(self) -> None:
        pass


def use_fake_radio(monkeypatch: pytest.MonkeyPatch, **kw: object) -> FakeRadio:
    """flex_iq connects to a FakeRadio; no sockets, sleeps or signals."""
    radio = FakeRadio(**kw)  # pyright: ignore[reportArgumentType]
    monkeypatch.setattr(flex_iq, "FlexConnection", lambda ip, port=4992: radio.connect())
    monkeypatch.setattr(flex_iq.socket, "socket", lambda *_: _Udp())
    monkeypatch.setattr(flex_iq.os, "kill", lambda *_: None)
    monkeypatch.setattr(flex_iq.time, "sleep", lambda _s: None)
    monkeypatch.setattr(flex_iq, "STATION_WAIT_S", 0.05)
    for name in ("SETTLE_S", "RETRY_CONNECT_S", "HEARTBEAT_S", "STATION_POLL_S"):
        monkeypatch.setattr(flex_iq, name, 0)
    monkeypatch.setattr(flex_iq, "STREAM_GRACE_S", 0.01)
    monkeypatch.setattr(_Udp, "packets", [])
    return radio
