from ..bus import BusNode, Frame, NS
from .base import Peripheral

PROTECTED = {"NBTP", "DBTP", "RWD", "GFC", "SIDFC", "XIDFC", "XIDAM", "RXF0C", "RXBC", "RXF1C", "RXESC",
             "TXBC", "TXESC", "TXEFC", "TDCR", "TSCC", "TOCC"}
DATA_BYTES = [8, 12, 16, 20, 24, 32, 48, 64]
INIT_READBACK_NS = 1000           # zalozenie modelu: INIT czyta sie jako 1 po ~1 us (synchronizacja domen zegarowych)


class FDCAN(Peripheral, BusNode):
    has_events = True

    def __init__(self, engine, name, tname):
        Peripheral.__init__(self, engine, name, tname)
        self.irq_names = (f"{name}_IT0_IRQn", f"{name}_IT1_IRQn")
        self.name_bus = name
        self.name = name
        self.warnings = []
        self.tec = self.rec = 0
        self.passive = False
        self.bus_off = False
        self.warn_state = False
        self.init_visible_at = 0
        self.init_set = True
        self.recover_until = None
        self.fifo0_put = 0
        self.fifo0_get = 0
        self.fifo0_fill = 0
        self.rx_dropped = 0
        self.tx_sent = 0
        self.rx_stored = 0
        self.rx_filtered_out = 0
        self.rx_locked = 0
        self.reset()

    # ---------- reset ----------
    def reset(self):
        for r in ("CCCR",):
            self.put("CCCR", 0x1)
        self.put("NBTP", 0x06000A03)        # wartosc po resecie wg RM0433
        self.put("DBTP", 0x00000A33)
        self.put("ENDN", 0x87654321)
        self.put("PSR", 0x707)
        self.put("RWD", 0)
        self.put("RXF0S", 0)
        self.put("TXFQS", 0)

    def lint(self, msg):
        t = self.eng.now
        loc = self.eng.loc()
        if any(w[1] == msg and w[2] == loc for w in self.warnings[-20:]):
            return                                 # ta sama uwaga z tego samego miejsca: nie powtarzaj
        self.warnings.append((t, msg, loc))
        del self.warnings[:-100]
        self.eng.event("uwaga", f"{self.name}: {msg}", loc)

    # ---------- BusNode ----------
    @property
    def online(self):
        return not self.bus_off and self.recover_until is None and not (self.get("CCCR") & 1) and self.bitrate_hz() > 0

    @online.setter
    def online(self, v):
        pass

    @property
    def bitrate(self):
        return self.bitrate_hz()

    def bitrate_hz(self):
        clk = self.eng.prj.can_kernel_hz
        brp = self.getf("NBTP", "NBRP") + 1
        t1 = self.getf("NBTP", "NTSEG1") + 1
        t2 = self.getf("NBTP", "NTSEG2") + 1
        tq_per_bit = 1 + t1 + t2
        return clk / (brp * tq_per_bit) if clk else 0

    def cfg_ok(self):
        return (self.get("CCCR") & 1) == 0

    # ---------- Message RAM ----------
    def ram(self):
        return self.eng.msgram

    def rd_ram(self, byte_off, n=4):
        m = self.ram()
        if byte_off < 0 or byte_off + n > len(m):
            self.mem_fault(byte_off)
            return b"\0" * n
        return bytes(m[byte_off:byte_off + n])

    def wr_ram(self, byte_off, data):
        m = self.ram()
        if byte_off < 0 or byte_off + len(data) > len(m):
            self.mem_fault(byte_off)
            return
        m[byte_off:byte_off + len(data)] = data

    def mem_fault(self, off):
        self.setf("IR", "ARA", 1)
        self.lint(f"dostęp do Message RAM poza zakresem (offset {off}); ustawiono IR.ARA")

    def word(self, off):
        return int.from_bytes(self.rd_ram(off), "little")

    # ---------- dostep do rejestrow ----------
    def read(self, off):
        r, _ = self.regs_by_off.get(off, (None, 0))
        self.touched.add(off)
        if r is None:
            return 0
        n = r.name
        if n == "CCCR":
            v = self.get("CCCR")
            if (v & 1) and self.eng.now < self.init_visible_at:
                v &= ~1                   # INIT jeszcze nie widoczny
            return v
        if n == "ECR":
            return (min(self.tec, 255)) | (min(self.rec, 127) << 8) | ((1 << 15) if self.rec >= 128 else 0)
        if n == "PSR":
            v = self.psr()
            self.setf("PSR", "LEC", 7)            # M_CAN: kazdy odczyt PSR ustawia LEC na 7 (No Change)
            return v
        if n == "TXBRP":
            return self.get("TXBRP")
        if n == "RXF0S":
            return self.rxf0s()
        return self.mem.get(off, 0)

    def psr(self):
        v = self.get("PSR") & ~((1 << self.field("PSR", "EP")[0]) | (1 << self.field("PSR", "EW")[0]) | (1 << self.field("PSR", "BO")[0]))
        v |= int(self.passive) << self.field("PSR", "EP")[0]
        v |= int(self.warn_state) << self.field("PSR", "EW")[0]
        v |= int(self.bus_off) << self.field("PSR", "BO")[0]
        return v

    def rxf0_cfg(self):
        return self.getf("RXF0C", "F0S"), self.get("RXF0C") & 0xFFFC

    def rxf0s(self):
        size, _ = self.rxf0_cfg()
        fill = self.fifo0_fill if size else 0
        full = int(bool(size) and fill >= size)
        v = self.get("RXF0S") & (1 << self.field("RXF0S", "RF0L")[0])
        for fname, val in (("F0FL", fill), ("F0GI", self.fifo0_get), ("F0PI", self.fifo0_put), ("F0F", full)):
            pos, w = self.field("RXF0S", fname)
            v |= (val & ((1 << w) - 1)) << pos
        return v

    def write(self, off, val):
        r, idx = self.regs_by_off.get(off, (None, 0))
        self.touched.add(off)
        if r is None:
            self.mem[off] = val & 0xFFFFFFFF
            return
        n = r.name
        val &= 0xFFFFFFFF
        cfg_enabled = (self.get("CCCR") & 1) and ((self.get("CCCR") >> self.field("CCCR", "CCE")[0]) & 1)
        if n in PROTECTED and not cfg_enabled:
            self.lint(f"zapis {n}=0x{val:X} ZIGNOROWANY: rejestr chroniony, wymaga CCCR.INIT=1 i CCE=1 "
                      f"(INIT={self.get('CCCR') & 1}, CCE={(self.get('CCCR') >> 1) & 1})")
            return
        if n == "CCCR":
            return self.w_cccr(val)
        if n == "IR":
            self.put("IR", self.get("IR") & ~val)
            if (val >> self.field("IR", "RF0L")[0]) & 1:
                self.setf("RXF0S", "RF0L", 0)
            return
        if n == "RXF0C":
            self.fifo0_put = self.fifo0_get = self.fifo0_fill = 0   # nowa konfiguracja FIFO0: indeksy od zera
            self.put(n, val)
            return
        if n == "NDAT1" or n == "NDAT2":
            self.put(n, self.get(n) & ~val)
            return
        if n == "TXBAR":
            return self.w_txbar(val)
        if n == "TXBCR":
            return self.w_txbcr(val)
        if n == "RXF0A":
            size, _ = self.rxf0_cfg()
            if size:
                idx = (val & 0x3F) % size
                done = (idx - self.fifo0_get) % size + 1   # potwierdzenie indeksu zwalnia go i wszystkie starsze
                if done > self.fifo0_fill:
                    self.lint(f"RXF0A={idx}: potwierdzono element FIFO0, który nie jest zajęty (F0GI={self.fifo0_get}, F0FL={self.fifo0_fill})")
                    done = self.fifo0_fill
                self.fifo0_fill -= done
                self.fifo0_get = (self.fifo0_get + done) % size
            return
        if n in ("TXBRP", "TXBTO", "TXBCF", "ECR", "PSR", "RXF0S", "TXFQS", "ENDN", "CREL"):
            return                        # tylko do odczytu
        self.put(n, val)

    def w_cccr(self, val):
        old = self.get("CCCR")
        ipos = self.field("CCCR", "INIT")[0]
        cpos = self.field("CCCR", "CCE")[0]
        new = old
        want_init = (val >> ipos) & 1
        if want_init and not (old & 1):
            new |= 1
            self.init_visible_at = self.eng.now + INIT_READBACK_NS
            self.eng.event("fdcan", f"{self.name}: CCCR.INIT=1 (kontroler odłączony od magistrali)")
        elif not want_init and (old & 1):
            if self.eng.now < self.init_visible_at:
                self.lint("zapis CCCR z INIT=0 tuż po ustawieniu INIT (w trakcie synchronizacji): odczyt-modyfikacja-zapis "
                          "(CCCR |= ...) przeczytał INIT=0 i cofnął INIT, więc CCE nie zadziała; po CCCR.INIT=1 poczekaj, "
                          "aż INIT odczyta się jako 1")
            new &= ~1
            new &= ~(1 << cpos)
            self.start_operation()
        elif not want_init:
            new &= ~1
        # CCE mozna ustawic tylko gdy INIT jest widoczny jako 1
        init_visible = bool(old & 1) and self.eng.now >= self.init_visible_at
        want_cce = (val >> cpos) & 1
        if want_cce and not (new & 1):
            pass                               # INIT=0: CCE nie moze byc ustawione (sprzet je zeruje)
        elif want_cce:
            if init_visible or (new & 1 and old & 1 and want_init):
                new |= 1 << cpos
            else:
                self.lint("zapis CCCR.CCE=1 ZIGNOROWANY: INIT jeszcze nie odczytuje się jako 1 "
                          "(RM: CCE tylko przy INIT=1; po ustawieniu INIT poczekaj, aż bit się utrwali)")
        else:
            new &= ~(1 << cpos)
        # pozostale bity (TXP, DAR, MON, ASM, TEST, FDOE, BRSE ...): chronione, wymagaja CCE
        mask_prot = ~((1 << ipos) | (1 << cpos))
        if (new >> cpos) & 1 and (new & 1):
            new = (new & ~mask_prot) | (val & mask_prot)
        self.put("CCCR", new)

    def start_operation(self):
        br = self.bitrate_hz()
        self.eng.event("fdcan", f"{self.name}: CCCR.INIT=0, start, bitrate={br:.0f} bit/s")
        if self.bus_off:
            self.recover_until = self.eng.now + int(129 * 11 * NS / max(br, 1))
            self.eng.event("fdcan", f"{self.name}: Bus-Off, odzyskiwanie: 129 x 11 bitów recesywnych (RM0433)")
        self.run_lint()

    def run_lint(self):
        br = self.bitrate_hz()
        if br <= 0:
            self.lint("bitrate = 0 (sprawdź NBTP i zegar jądra FDCAN)")
        sjw = self.getf("NBTP", "NSJW")
        t2 = self.getf("NBTP", "NTSEG2")
        if sjw > t2:
            self.lint(f"NBTP: SJW ({sjw + 1} tq) większe niż TSEG2 ({t2 + 1} tq); wg specyfikacji SJW nie powinno przekraczać TSEG2")
        areas = []
        lss = self.getf("SIDFC", "LSS")
        areas.append(("filtry std", self.get("SIDFC") & 0xFFFC, lss * 4))
        ndtb = self.getf("TXBC", "NDTB")
        areas.append(("bufory Tx", self.get("TXBC") & 0xFFFC, ndtb * (8 + DATA_BYTES[self.getf("TXESC", "TBDS") & 7])))
        rbds = DATA_BYTES[self.getf("RXESC", "RBDS") & 7]
        # liczbe uzywanych buforow Rx wyznaczaja filtry z SFEC=7 (najwyzszy numer bufora + 1)
        nbuf = 0
        for i in range(lss):
            w = self.word((self.get("SIDFC") & 0xFFFC) + 4 * i)
            if (w >> 27) & 7 == 7:
                nbuf = max(nbuf, (w & 0x3F) + 1)
        areas.append(("bufory Rx", self.get("RXBC") & 0xFFFC, nbuf * (8 + rbds)))
        f0s = self.getf("RXF0C", "F0S")
        areas.append(("Rx FIFO0", self.get("RXF0C") & 0xFFFC, f0s * (8 + DATA_BYTES[self.getf("RXESC", "F0DS") & 7])))
        for i, (na, sa, ln) in enumerate(areas):
            if sa + ln > 0x2800:
                self.lint(f"{na}: obszar [{sa}, {sa + ln}) wychodzi poza 10 KB Message RAM")
            for nb, sb, lb in areas[i + 1:]:
                if ln and lb and sa < sb + lb and sb < sa + ln:
                    self.lint(f"obszary Message RAM nakładają się: {na} [{sa},{sa + ln}) i {nb} [{sb},{sb + lb})")
        if (self.get("GFC") >> 4) & 3 == 0:
            self.lint("GFC.ANFS=0: ramki bez pasującego filtra trafiaja do FIFO0 (nie zostaną odrzucone)")

    # ---------- Tx ----------
    def w_txbar(self, val):
        ndtb = self.getf("TXBC", "NDTB")
        for i in range(32):
            if (val >> i) & 1:
                if i >= ndtb:
                    self.lint(f"TXBAR: bufor {i} poza konfiguracja (NDTB={ndtb}), zlecenie zignorowane")
                    continue
                self.put("TXBRP", self.get("TXBRP") | (1 << i))
                self.put("TXBTO", self.get("TXBTO") & ~(1 << i))   # nowe zlecenie kasuje TXBTO/TXBCF tego bufora
                self.put("TXBCF", self.get("TXBCF") & ~(1 << i))
        self.eng.kick()

    def w_txbcr(self, val):
        for i in range(32):
            if (val >> i) & 1 and (self.get("TXBRP") >> i) & 1:
                if self.eng.bus.cur and self.eng.bus.cur[0] is self and self.eng.bus.cur[2] == i:
                    continue                        # ramka w toku konczy sie normalnie
                self.put("TXBRP", self.get("TXBRP") & ~(1 << i))
                self.put("TXBCF", self.get("TXBCF") | (1 << i))
                self.eng.event("fdcan", f"{self.name}: anulowano bufor Tx {i}")

    def elem_size(self):
        return 8 + DATA_BYTES[self.getf("TXESC", "TBDS") & 7]

    def tx_candidate(self):
        pend = self.get("TXBRP")
        if not pend:
            return None
        base = self.get("TXBC") & 0xFFFC
        best = None
        for i in range(32):
            if (pend >> i) & 1:
                off = base + i * self.elem_size()
                r0 = self.word(off)
                r1 = self.word(off + 4)
                xtd = (r0 >> 30) & 1
                fid = (r0 & 0x1FFFFFFF) if xtd else (r0 >> 18) & 0x7FF
                dlc = (r1 >> 16) & 0xF
                n = min(dlc, 8)
                data = self.rd_ram(off + 8, n)
                key = (fid >> 18 if xtd else fid, i)
                if best is None or key < best[0]:
                    best = (key, Frame(fid, data, bool(xtd), bool((r0 >> 29) & 1)), i)
        return (best[1], best[2]) if best else None

    def tx_done(self, handle, acked, now):
        i = handle
        self.put("TXBRP", self.get("TXBRP") & ~(1 << i))
        self.put("TXBTO", self.get("TXBTO") | (1 << i))
        if (self.get("TXBTIE") >> i) & 1:
            self.setf("IR", "TC", 1)
        self.tx_sent += 1
        self.setf("PSR", "LEC", 0)
        self.dec_tec()

    def dec_tec(self):
        if self.tec > 0:
            self.tec -= 1
        self.update_state()

    def tx_error(self, now, kind="ack"):
        # nadawca: +8. Wyjatek ISO 11898-1: w Error Passive blad ACK (bez dominanty w pasywnej fladze) nie zwieksza TEC
        if kind != "ack" or not self.passive:
            self.tec += 8
        self.setf("PSR", "LEC", 3 if kind == "ack" else 4)
        self.setf("IR", "PEA", 1)
        self.update_state()

    def tx_failed(self, handle):
        if (self.get("CCCR") >> self.field("CCCR", "DAR")[0]) & 1:
            self.put("TXBRP", self.get("TXBRP") & ~(1 << handle))
            self.put("TXBCF", self.get("TXBCF") | (1 << handle))
            self.eng.event("fdcan", f"{self.name}: CCCR.DAR=1, bufor Tx {handle} nie będzie ponawiany (TXBCF)")
            return True
        return False

    def rx_error(self, now, lec=6):
        self.rec = min(self.rec + 1, 255)
        self.setf("PSR", "LEC", lec)
        self.setf("IR", "PEA", 1)
        self.update_state()

    def update_state(self):
        was_p, was_w, was_bo = self.passive, self.warn_state, self.bus_off
        self.passive = self.tec >= 128 or self.rec >= 128
        self.warn_state = self.tec >= 96 or self.rec >= 96
        if self.tec > 255 and not self.bus_off:
            self.bus_off = True
            self.setf("CCCR", "INIT", 1)
            self.init_visible_at = 0
            self.setf("IR", "BO", 1)
            self.eng.event("fdcan", f"{self.name}: BUS-OFF (TEC={self.tec}); sprzęt ustawił INIT, program musi go zdjąć")
        if self.passive != was_p:
            self.setf("IR", "EP", 1)
            self.eng.event("fdcan", f"{self.name}: Error Passive {'WEJSCIE' if self.passive else 'WYJSCIE'} (TEC={self.tec}, REC={self.rec})")
        if self.warn_state != was_w:
            self.setf("IR", "EW", 1)

    # ---------- Rx ----------
    def rx(self, frame, now):
        # poprawny odbior: REC-1 (1..127), a powyzej 127 ustaw na wartosc 119..127 (ISO 11898-1; model: 120)
        self.rec = 120 if self.rec > 127 else max(0, self.rec - 1)
        self.setf("PSR", "LEC", 0)
        self.update_state()
        if frame.rtr and not frame.ext and (self.get("GFC") >> self.field("GFC", "RRFS")[0]) & 1:
            self.rx_filtered_out += 1
            return False
        if frame.ext:
            self.rx_filtered_out += 1
            return False                       # filtry rozszerzone nie sa modelowane
        lss = self.getf("SIDFC", "LSS")
        base = self.get("SIDFC") & 0xFFFC
        for i in range(lss):
            w = self.word(base + 4 * i)
            sft, sfec, id1, id2 = (w >> 30) & 3, (w >> 27) & 7, (w >> 16) & 0x7FF, w & 0x7FF
            if sfec == 0:
                continue
            if sfec == 7:
                hit = frame.id == id1
            elif sft == 0:
                hit = id1 <= frame.id <= id2
            elif sft == 1:
                hit = frame.id in (id1, id2)
            elif sft == 2:
                hit = (frame.id & id2) == (id1 & id2)
            else:
                hit = False
            if not hit:
                continue
            if sfec == 7:
                b = id2 & 0x3F
                nd = self.get("NDAT1") if b < 32 else self.get("NDAT2")
                if (nd >> (b % 32)) & 1:
                    self.rx_locked += 1
                    continue                   # M_CAN: bufor z NDAT=1 jest zablokowany, filtr "nie pasuje", szukaj dalej
            if sfec == 3:
                self.rx_filtered_out += 1
                return False
            if sfec == 7:
                return self.store_buffer(frame, id2 & 0x3F, i)
            if sfec in (1, 5):
                return self.store_fifo0(frame, i, False)
            if sfec in (2, 6):
                self.rx_filtered_out += 1      # FIFO1 nie jest modelowane
                return False
            return False
        anfs = (self.get("GFC") >> self.field("GFC", "ANFS")[0]) & 3
        if anfs == 0:
            return self.store_fifo0(frame, 0x3F, True)
        self.rx_filtered_out += 1
        return False

    def rx_words(self, frame, fidx, anmf):
        r0 = ((frame.id & 0x7FF) << 18) | (int(frame.rtr) << 29)
        r1 = ((frame.dlc & 0xF) << 16) | ((fidx & 0x3F) << 24) | (int(anmf) << 31) | (self.eng.now // 1000 & 0xFFFF)
        return r0.to_bytes(4, "little") + r1.to_bytes(4, "little") + frame.data.ljust(4 * ((len(frame.data) + 3) // 4), b"\0")

    def store_buffer(self, frame, bidx, fidx):
        rbds = DATA_BYTES[self.getf("RXESC", "RBDS") & 7]
        base = self.get("RXBC") & 0xFFFC
        self.wr_ram(base + bidx * (8 + rbds), self.rx_words(frame, fidx, False))
        self.put("NDAT1", self.get("NDAT1") | (1 << bidx)) if bidx < 32 else self.put("NDAT2", self.get("NDAT2") | (1 << (bidx - 32)))
        self.setf("IR", "DRX", 1)
        self.rx_stored += 1
        return True

    def store_fifo0(self, frame, fidx, anmf):
        size, base = self.rxf0_cfg()
        if not size:
            self.rx_filtered_out += 1
            return False
        esz = 8 + DATA_BYTES[self.getf("RXESC", "F0DS") & 7]
        if self.fifo0_fill >= size:
            if self.getf("RXF0C", "F0OM"):
                # tryb nadpisywania: najstarszy element jest zastepowany, indeks odczytu przesuwa sie
                self.fifo0_get = (self.fifo0_get + 1) % size
                self.fifo0_fill -= 1
            else:
                self.setf("IR", "RF0L", 1)        # tryb blokujacy: nowa ramka gubiona, Message Lost
                self.setf("RXF0S", "RF0L", 1)
                self.rx_dropped += 1
                return False
        self.wr_ram(base + self.fifo0_put * esz, self.rx_words(frame, fidx, anmf))
        self.fifo0_put = (self.fifo0_put + 1) % size
        self.fifo0_fill += 1
        self.setf("IR", "RF0N", 1)
        if self.fifo0_fill >= size:
            self.setf("IR", "RF0F", 1)
        self.rx_stored += 1
        return True

    # ---------- czas i przerwania ----------
    def next_event(self, now):
        return self.recover_until

    def advance(self, now):
        if self.recover_until is not None and now >= self.recover_until:
            self.recover_until = None
            self.bus_off = False
            self.tec = self.rec = 0
            self.update_state()
            self.eng.event("fdcan", f"{self.name}: wyszedł z Bus-Off, liczniki wyzerowane")

    def irq_lines(self):
        ir, ie, ils, ile = self.get("IR"), self.get("IE"), self.get("ILS"), self.get("ILE")
        l0 = bool(ir & ie & ~ils & 0xFFFFFFFF) and bool(ile & 1)
        l1 = bool(ir & ie & ils) and bool(ile & 2)
        return {self.irq_names[0]: l0, self.irq_names[1]: l1}

    def ram_view(self):
        """Zawartosc Message RAM rozbita wedlug biezacej konfiguracji (do podgladu w UI)."""
        out = {"filters": [], "tx": [], "rx": [], "fifo0": []}
        lss = self.getf("SIDFC", "LSS")
        fb = self.get("SIDFC") & 0xFFFC
        nbuf = 0
        for i in range(lss):
            w = self.word(fb + 4 * i)
            sft, sfec, id1, id2 = (w >> 30) & 3, (w >> 27) & 7, (w >> 16) & 0x7FF, w & 0x7FF
            if sfec == 7:
                nbuf = max(nbuf, (id2 & 0x3F) + 1)
            out["filters"].append({"i": i, "word": f"{w:08X}", "sft": sft, "sfec": sfec, "id1": f"{id1:03X}", "id2": id2})
        tb = self.get("TXBC") & 0xFFFC
        esz = self.elem_size()
        pend = self.get("TXBRP")
        for i in range(self.getf("TXBC", "NDTB")):
            o = tb + i * esz
            r0, r1 = self.word(o), self.word(o + 4)
            dlc = (r1 >> 16) & 0xF
            out["tx"].append({"i": i, "id": f"{(r0 >> 18) & 0x7FF:03X}", "dlc": dlc, "data": self.rd_ram(o + 8, min(dlc, 8)).hex(" "),
                              "pending": bool((pend >> i) & 1)})
        rbds = DATA_BYTES[self.getf("RXESC", "RBDS") & 7]
        rb = self.get("RXBC") & 0xFFFC
        nd = self.get("NDAT1")
        for i in range(nbuf):
            o = rb + i * (8 + rbds)
            r0, r1 = self.word(o), self.word(o + 4)
            dlc = (r1 >> 16) & 0xF
            out["rx"].append({"i": i, "id": f"{(r0 >> 18) & 0x7FF:03X}", "dlc": dlc, "data": self.rd_ram(o + 8, min(dlc, 8)).hex(" "),
                              "new": bool((nd >> i) & 1), "fidx": (r1 >> 24) & 0x3F})
        size, fbase = self.rxf0_cfg()
        fesz = 8 + DATA_BYTES[self.getf("RXESC", "F0DS") & 7]
        for k in range(self.fifo0_fill if size else 0):
            i = (self.fifo0_get + k) % size
            o = fbase + i * fesz
            r0, r1 = self.word(o), self.word(o + 4)
            if True:
                dlc = (r1 >> 16) & 0xF
                out["fifo0"].append({"i": i, "id": f"{(r0 >> 18) & 0x7FF:03X}", "dlc": dlc, "data": self.rd_ram(o + 8, min(dlc, 8)).hex(" ")})
        out["areas"] = {"filters": [fb, lss * 4], "tx": [tb, self.getf("TXBC", "NDTB") * esz], "rx": [rb, nbuf * (8 + rbds)],
                        "fifo0": [fbase, size * fesz]}
        return out

    def stats(self):
        return {"tec": self.tec, "rec": self.rec, "passive": self.passive, "bus_off": self.bus_off,
                "bitrate": round(self.bitrate_hz()), "tx_sent": self.tx_sent, "rx_stored": self.rx_stored,
                "rx_filtered_out": self.rx_filtered_out, "rx_dropped": self.rx_dropped, "rx_locked": self.rx_locked,
                "init": bool(self.get("CCCR") & 1), "lec": self.getf("PSR", "LEC"), "ctrl": "FDCAN"}

    def timing(self):
        """Parametry bitu z NBTP (wartosci rzeczywiste, bez -1)."""
        brp = self.getf("NBTP", "NBRP") + 1
        t1 = self.getf("NBTP", "NTSEG1") + 1
        t2 = self.getf("NBTP", "NTSEG2") + 1
        sjw = self.getf("NBTP", "NSJW") + 1
        return {"brp": brp, "tseg1": t1, "tseg2": t2, "sjw": sjw, "clk": self.eng.prj.can_kernel_hz}

    live_regs = ("ECR", "PSR", "RXF0S", "CCCR")
