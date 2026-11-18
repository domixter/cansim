"""Modele peryferiow. Bazowy model "pamiec" + male modele RCC, USART, timerow, zeby kod startowy nie wisial."""


class Peripheral:
    """Model jednego peryferium. Rejestry wg oficjalnego rozkladu ST (dev.layouts)."""
    irq_names = ()
    has_events = False          # True: silnik pyta o next_event/advance/irq_lines

    def __init__(self, engine, name, tname):
        self.eng = engine
        self.name = name
        self.tname = tname
        self.dev = engine.dev
        lay = self.dev.layouts[tname]
        self.regs_by_name = {r.name: r for r in lay["regs"]}
        self.regs_by_off = {}
        for r in lay["regs"]:
            for i in range(r.count):
                self.regs_by_off[r.offset + i * r.size] = (r, i)
        self.mem = {}                       # offset -> wartosc
        self.touched = set()
        self.seen = {}

    # --- dostep wg offsetu ---
    def off_of(self, reg, idx=0):
        return self.regs_by_name[reg].offset + idx * self.regs_by_name[reg].size

    def get(self, reg, idx=0):
        return self.mem.get(self.off_of(reg, idx), 0)

    def put(self, reg, v, idx=0):
        off = self.off_of(reg, idx)
        self.mem[off] = v & 0xFFFFFFFF
        self.seen[off] = self.seen.get(off, 0) | (v & 0xFFFFFFFF)   # bity ustawione chocby na chwile (dla podgladu)

    def field(self, reg, fname):
        pre = self.dev.type_prefix(self.tname)
        for n, pos, w in self.dev.fields.get((pre, reg), []):
            if n == fname:
                return pos, w
        raise KeyError(f"{pre}_{reg}_{fname}")

    def getf(self, reg, fname, idx=0):
        pos, w = self.field(reg, fname)
        return (self.get(reg, idx) >> pos) & ((1 << w) - 1)

    def setf(self, reg, fname, v, idx=0):
        pos, w = self.field(reg, fname)
        m = ((1 << w) - 1) << pos
        self.put(reg, (self.get(reg, idx) & ~m) | ((v << pos) & m), idx)

    def read(self, off):
        self.touched.add(off)
        return self.mem.get(off, 0)

    def write(self, off, val):
        self.touched.add(off)
        self.mem[off] = val & 0xFFFFFFFF

    # --- zdarzenia ---
    def irq_lines(self):
        return {}

    def next_event(self, now):
        return None

    def advance(self, now):
        pass

    def snapshot(self):
        """Lista rejestrow do UI: (nazwa, offset, wartosc)."""
        out = []
        for off, (r, i) in sorted(self.regs_by_off.items()):
            if r.name.startswith("RESERVED"):
                continue
            nm = r.name if r.count == 1 else f"{r.name}[{i}]"
            out.append((nm, off, self.mem.get(off, 0)))
        return out


class Plain(Peripheral):
    """Zwykla pamiec: odczyt daje ostatnio zapisana wartosc."""


class RCC(Plain):
    """Flagi gotowosci: X_ON -> X_RDY, SW -> SWS. Zeby petle 'czekaj na zegar' nie wisialy."""
    def write(self, off, val):
        super().write(off, val)
        r, _ = self.regs_by_off.get(off, (None, 0))
        if r is None:
            return
        pre = "RCC"
        if r.name in ("CR",):
            for fname, pos, w in self.dev.fields.get((pre, "CR"), []):
                if fname.endswith("ON") and w == 1:
                    rdy = fname[:-2] + "RDY"
                    for n2, p2, _ in self.dev.fields.get((pre, "CR"), []):
                        if n2 == rdy:
                            on = (self.mem[off] >> pos) & 1
                            self.mem[off] = (self.mem[off] & ~(1 << p2)) | (on << p2)
        if r.name == "CFGR":
            sw = self.field("CFGR", "SW")
            sws = self.field("CFGR", "SWS")
            v = (self.mem[off] >> sw[0]) & ((1 << sw[1]) - 1)
            self.mem[off] = (self.mem[off] & ~(((1 << sws[1]) - 1) << sws[0])) | (v << sws[0])


class USART(Plain):
    """Nadajnik zawsze gotowy (TXE/TC), zeby kod UART nie wisial. Odbior pomijany."""
    def read(self, off):
        v = super().read(off)
        r, _ = self.regs_by_off.get(off, (None, 0))
        if r and r.name in ("ISR", "SR"):
            v |= (1 << 7) | (1 << 6)
        return v


class BasicTimer(Plain):
    """TIMx jako licznik do przepelnienia. Flaga UIF liczona leniwie (przy odczycie SR); zdarzenia w silniku
    tylko gdy wlaczone przerwanie (DIER.UIE), zeby timery PWM nie obciazaly symulacji. Zegar timera: projekt (timer_clk_hz)."""
    has_events = True

    def __init__(self, engine, name, tname):
        super().__init__(engine, name, tname)
        self.next_ns = None
        self.period_ns = 0
        self.irq = f"{name}_IRQn"

    def _has(self, reg):
        return reg in self.regs_by_name

    def _enabled(self):
        return self._has("CR1") and bool(self.get("CR1") & 1)

    def _arm(self, now):
        if not (self._enabled() and self._has("PSC") and self._has("ARR")):
            self.next_ns = None
            return
        period = (self.get("PSC") + 1) * (self.get("ARR") + 1) * 1e9 / self.eng.prj.timer_clk_hz
        self.period_ns = max(int(period), 1)
        self.next_ns = now + self.period_ns

    def _catch_up(self, now):
        if self.next_ns is not None and now >= self.next_ns:
            if self._opm():
                self.put("CR1", self.get("CR1") & ~1)
                self.next_ns = None
            else:
                n = (now - self.next_ns) // self.period_ns + 1
                self.next_ns += n * self.period_ns
            self.put("SR", self.get("SR") | 1)

    def read(self, off):
        r, _ = self.regs_by_off.get(off, (None, 0))
        if r and r.name in ("SR", "CNT"):
            self._catch_up(self.eng.now)
        return super().read(off)

    def write(self, off, val):
        r, _ = self.regs_by_off.get(off, (None, 0))
        self._catch_up(self.eng.now)
        if r and r.name == "SR":                      # zapis 0 kasuje flage, 1 nie zmienia
            self.mem[off] = self.mem.get(off, 0) & val
            self.touched.add(off)
            return
        super().write(off, val)
        if r and r.name == "EGR" and (val & 1):       # UG: zdarzenie aktualizacji
            self.put("SR", self.get("SR") | 1)
        if r and r.name in ("CR1", "PSC", "ARR"):
            self._arm(self.eng.now)

    def _opm(self):
        try:
            return bool(self.getf("CR1", "OPM"))
        except KeyError:
            return False

    def _uie(self):
        return self._has("DIER") and bool(self.get("DIER") & 1)

    def next_event(self, now):
        return self.next_ns if (self.next_ns is not None and self._uie()) else None

    def advance(self, now):
        self._catch_up(now)

    def irq_lines(self):
        if not self._has("SR"):
            return {}
        self._catch_up(self.eng.now)
        return {self.irq: bool(self.get("SR") & self.get("DIER") & 1)}
