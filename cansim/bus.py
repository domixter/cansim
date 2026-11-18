"""Magistrala CAN: arbitraz, czas trwania ramki (z prawdziwym CRC15 i wstawianiem bitow), ACK, bledy."""
from dataclasses import dataclass, field

NS = 1_000_000_000


@dataclass
class Frame:
    id: int
    data: bytes = b""
    ext: bool = False
    rtr: bool = False

    @property
    def dlc(self):
        return len(self.data)


def _crc15(bits):
    crc = 0
    for b in bits:
        nxt = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def _bits_of(v, n):
    return [(v >> i) & 1 for i in range(n - 1, -1, -1)]


def frame_bits(f: Frame):
    """Dlugosc ramki w bitach od SOF do konca EOF (bez przerwy miedzyramkowej), z wstawionymi bitami."""
    if f.ext:
        head = [0] + _bits_of(f.id >> 18, 11) + [1, 1] + _bits_of(f.id & 0x3FFFF, 18) + [int(f.rtr), 0, 0]
    else:
        head = [0] + _bits_of(f.id, 11) + [int(f.rtr), 0, 0]
    head += _bits_of(f.dlc if not f.rtr else f.dlc, 4)
    if not f.rtr:
        for byte in f.data:
            head += _bits_of(byte, 8)
    body = head + _bits_of(_crc15(head), 15)
    stuffed, run, last = 0, 0, None
    for b in body:
        stuffed += 1
        if b == last:
            run += 1
        else:
            run, last = 1, b
        if run == 5:
            stuffed += 1          # wstawiony bit przeciwny
            last, run = 1 - b, 1
    return stuffed + 10           # CRC delimiter, ACK slot, ACK delimiter, EOF(7)


IFS_BITS = 3


class BusNode:
    """Interfejs wezla magistrali. Podklasy: kontroler CAN mikrokontrolera, ODrive, wezel generyczny."""
    name = "node"
    bitrate = 0            # 0 = nie uczestniczy
    online = False         # False: odlaczony (INIT, bus-off, brak zasilania)
    passive = False        # Error Passive: flaga bledu jest recesywna, nie niszczy cudzych ramek

    def tx_candidate(self):
        """(Frame, uchwyt) albo None."""
        return None

    def next_event(self, now):
        return 1 << 62

    def advance(self, now):
        pass

    def tx_done(self, handle, acked, now):
        pass

    def tx_error(self, now, kind="ack"):
        """Blad podczas naszej wysylki. kind: 'ack' (nikt nie potwierdzil) albo 'bit' (ramka zniszczona)."""

    def rx(self, frame, now):
        """Poprawnie odebrana ramka. Zwraca True, jesli wezel ja przyjal (filtr/adres), False gdy tylko potwierdzil ACK."""
        return False

    def rx_error(self, now, lec=6):
        pass


class ErrorCounters:
    """Liczniki bledow ISO 11898-1 dla wezlow zewnetrznych (ODrive, analizator): Error Passive przy 128, Bus-Off po 255,
    automatyczny powrot po 128 x 11 bitach (zalozenie: kontroler wezla ma wlaczone auto-odzyskiwanie)."""

    def ec_init(self):
        self.tec = self.rec = 0
        self.passive = False
        self.bus_off = False
        self.recover_at = None
        self.powered = True

    @property
    def online(self):
        return self.powered and not self.bus_off

    @online.setter
    def online(self, v):
        self.powered = bool(v)

    def ec_update(self, now):
        self.passive = self.tec >= 128 or self.rec >= 128
        if self.tec > 255 and not self.bus_off:
            self.bus_off = True
            self.recover_at = now + int(128 * 11 * NS / max(self.bitrate, 1))

    def ec_tx_error(self, now, kind):
        if kind != "ack" or not self.passive:
            self.tec += 8
        self.ec_update(now)

    def ec_rx_error(self, now):
        self.rec = min(self.rec + 1, 255)
        self.ec_update(now)

    def ec_rx_ok(self, now):
        self.rec = 120 if self.rec > 127 else max(0, self.rec - 1)
        self.ec_update(now)

    def ec_tx_ok(self, now):
        self.tec = max(0, self.tec - 1)
        self.ec_update(now)

    def ec_next(self):
        return self.recover_at if self.recover_at is not None else 1 << 62

    def ec_advance(self, now):
        if self.recover_at is not None and now >= self.recover_at:
            self.recover_at = None
            self.bus_off = False
            self.tec = self.rec = 0
            self.passive = False

    def ec_state(self):
        return {"tec": self.tec, "rec": self.rec, "passive": self.passive, "bus_off": self.bus_off}


@dataclass
class LogEntry:
    t_ns: int
    src: str
    frame: Frame
    status: str            # ok / brak ACK / blad
    dur_ns: int
    rate: int
    accepted: list = field(default_factory=list)   # wezly, ktore przyjely ramke (filtr / adres)
    acked: list = field(default_factory=list)      # wezly, ktore tylko potwierdzily (ACK), ale odrzucily filtrem
    reason: str = ""


def disp(m):
    """Nazwa wezla do logow: dla kontrolera mikrokontrolera nazwa plytki."""
    b = getattr(m, "board", None)
    return b.name if b is not None else m.name


def same_rate(a, b):
    """Klasyczny CAN toleruje male roznice zegara; >0,5 % roznicy nominalnej traktujemy jako inny bitrate."""
    return a and b and abs(a - b) <= 0.005 * max(a, b)


class Bus:
    def __init__(self):
        self.nodes = []
        self.busy_until = 0
        self.cur = None            # dict ramki w toku
        self.log = []
        self.max_log = 4000
        self.corrupt_next = 0      # wymus N uszkodzonych ramek
        self.load_ns = []          # (start, dur) do liczenia obciazenia
        self.suspend = {}          # wezel -> czas do kiedy zawieszony (suspend transmission)
        self.forced_error = ""     # np. brak terminacji: kazda ramka konczy sie bledem (zalozenie modelu)
        self.id_senders = {}       # (id, ext) -> zbior nazw nadawcow (wykrywanie kolizji ID)
        self.seq = 0

    def add(self, node):
        if node not in self.nodes:
            self.nodes.append(node)
        return node

    def remove(self, node):
        if node in self.nodes:
            self.nodes.remove(node)
        if self.cur and self.cur["node"] is node:
            self.cur = None

    def next_event(self, now):
        if self.cur:
            return self.cur["end"]
        t = max(now, self.busy_until)
        best = None
        for n in self.nodes:
            if n.online and n.bitrate and n.tx_candidate():
                c = max(t, self.suspend.get(n, 0))
                best = c if best is None else min(best, c)
        return best

    def step(self, now):
        """Wywolywane gdy now >= next_event. Konczy ramke w toku albo startuje nowa."""
        if self.cur:
            if now >= self.cur["end"]:
                self._finish(now)
            return
        if now < self.busy_until:
            return
        cands = []
        for n in self.nodes:
            if n.online and n.bitrate and self.suspend.get(n, 0) <= now:
                c = n.tx_candidate()
                if c:
                    f, h = c
                    # arbitraz bit po bicie: nizsze ID bazowe wygrywa; przy rownym ID bazowym ramka standardowa (IDE=0)
                    # wygrywa z rozszerzona, a ramka danych (RTR=0) z ramka zdalna
                    key = (f.id >> 18 if f.ext else f.id, 1 if f.ext else 0, f.id & 0x3FFFF if f.ext else 0, 1 if f.rtr else 0)
                    cands.append((key, n, f, h))
        if not cands:
            return
        cands.sort(key=lambda c: c[0])
        key, n, f, h = cands[0]
        # dwa wezly z identycznym polem arbitrazu wygrywaja jednoczesnie: przy roznych danych powstaje blad bitu
        twins = [(m, g, hh) for k2, m, g, hh in cands[1:] if k2 == key and m is not n]
        rate = n.bitrate
        bits = frame_bits(f)
        dur = int(bits * NS / rate)
        reason = ""
        if self.corrupt_next > 0:
            self.corrupt_next -= 1
            reason = "wstrzyknięta awaria"
        clash = [t for t in twins if t[1].data != f.data or t[1].dlc != f.dlc]
        if clash:
            reason = "kolizja ID 0x%X: nadają jednocześnie %s" % (f.id, ", ".join([disp(n)] + [disp(t[0]) for t in clash]))
        self.cur = {"node": n, "frame": f, "h": h, "end": now + dur, "rate": rate, "reason": reason, "bits": bits,
                    "twins": twins, "clash": clash}

    def _finish(self, now):
        c = self.cur
        self.cur = None
        n, f, h, end, rate, bits = c["node"], c["frame"], c["h"], c["end"], c["rate"], c["bits"]
        dur = int(bits * NS / rate)
        senders = {n} | {t[0] for t in c["twins"]}
        others = [m for m in self.nodes if m not in senders and m.online and m.bitrate]
        same = [m for m in others if same_rate(m.bitrate, rate)]
        mism = [m for m in others if not same_rate(m.bitrate, rate)]
        reason = c["reason"] or self.forced_error
        if not reason and [m for m in mism if not m.passive]:
            # wezel z innym bitrate widzi smieci (blad wypelniania) i nadaje aktywna flage bledu, niszczac ramke
            if not same:
                reason = f"inny bitrate: nadawca {disp(n)} {rate / 1000:g}k, pozostali {mism[0].bitrate / 1000:g}k"
            else:
                reason = "inny bitrate: " + ", ".join(f"{disp(m)} {m.bitrate / 1000:g}k" for m in mism[:3])
        self.id_senders.setdefault((f.id, f.ext), set()).update(disp(s) for s in senders)
        entry = LogEntry(end - dur, disp(n), f, "", dur, rate, reason=reason)
        self.seq += 1
        entry.seq = self.seq
        if reason:
            entry.status = "blad"
            for m in same:
                m.rx_error(now, 6)
            for m in mism:
                # wezel o innym bitrate wykrywa blad pierwszy; po jego fladze widzi dominujace flagi innych wezlow,
                # wiec wg ISO 11898-1 jego REC rosnie o 8 (pozostali odbiorcy +1)
                for _ in range(8 if not same or m.bitrate != rate else 1):
                    m.rx_error(now, 1)
            n.tx_error(now, "bit")
            for t in c["clash"]:
                t[0].tx_error(now, "bit")
            self.busy_until = end + int((20 + IFS_BITS) * NS / rate)    # flaga bledu 6..12 bitow + ogranicznik 8
            for s in senders:
                if s.passive:
                    self.suspend[s] = self.busy_until + int(8 * NS / rate)   # suspend transmission (Error Passive)
        elif not same:
            entry.status = "brak ACK" + (" (odbiorcy mają inny bitrate)" if mism else "")
            for m in mism:
                m.rx_error(now, 1)
            n.tx_error(now, "ack")
            self.busy_until = end + int((20 + IFS_BITS) * NS / rate)
            if n.passive:
                self.suspend[n] = self.busy_until + int(8 * NS / rate)
        else:
            entry.status = "ok"
            for m in same:
                (entry.accepted if m.rx(f, now) else entry.acked).append(disp(m))
            n.tx_done(h, True, now)
            for t in c["twins"]:
                t[0].tx_done(t[2], True, now)
            self.busy_until = end + int(IFS_BITS * NS / rate)
        self.log.append(entry)
        if len(self.log) > self.max_log:
            del self.log[:500]
        self.load_ns.append((end - dur, dur))
        self.load_ns = [x for x in self.load_ns if x[0] > end - NS]

    def load_percent(self, now):
        window = NS
        tot = sum(min(d, max(0, s + d - (now - window))) for s, d in self.load_ns if s + d > now - window)
        return 100.0 * tot / window

    def id_collisions(self):
        return {k: v for k, v in self.id_senders.items() if len(v) > 1}
