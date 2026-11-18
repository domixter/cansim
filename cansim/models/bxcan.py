"""Model kontrolera bxCAN (STM32F4/F2/F1/F3, CAN_TypeDef). Zachowanie wg RM0090 rozdz. 32 w interpretacji autora symulatora.
Nazwy rejestrow i pozycje bitow MCR/MSR/TSR/RFxR/IER/ESR/BTR pochodza z oficjalnego naglowka ST.

Zasymulowane: tryby sleep/initialization/normal (z opoznieniem synchronizacji 11 bitow), BTR chronione (tylko INAK=1),
3 skrzynki nadawcze (priorytet po ID albo kolejnosc zlecen: TXFP), NART, ABOM, ABRQ, 2 kolejki odbiorcze FIFO po 3 ramki
(RFOM, FULL, FOVR, RFLM), banki filtrow (maska/lista, 16/32 bity, przypisanie do FIFO, FINIT, FACT, numer FMI),
licznik TEC/REC, Error Warning/Passive/Bus-Off z odzyskiwaniem, cztery linie przerwan (TX, RX0, RX1, SCE).
Pominiete: Time Triggered, tryb silent/loopback (bity sa przechowywane, ale bez efektu), wake-up, ALST (przegrana arbitrazu).
"""
from ..bus import BusNode, Frame, NS
from .base import Peripheral

TX_BOX = "sTxMailBox[{}].{}"
RX_BOX = "sFIFOMailBox[{}].{}"
SYNC_BITS = 11                     # synchronizacja z magistrala: 11 bitow recesywnych


class BxCAN(Peripheral, BusNode):
    has_events = True

    def __init__(self, engine, name, tname):
        Peripheral.__init__(self, engine, name, tname)
        self.irq_names = (f"{name}_TX_IRQn", f"{name}_RX0_IRQn", f"{name}_RX1_IRQn", f"{name}_SCE_IRQn")
        self.name = name
        self.warnings = []
        self.tec = self.rec = 0
        self.passive = False
        self.warn_state = False
        self.bus_off = False
        self.recover_until = None
        self.sync_until = None            # (czas, co ustawic) dla INAK/SLAK
        self.pending_mode = None
        self.fifo = [[], []]              # kolejki Frame (najstarsza pierwsza), max 3
        self.req_order = []               # kolejnosc zlecen TXRQ (TXFP=1)
        self.tx_sent = 0
        self.rx_stored = 0
        self.rx_filtered_out = 0
        self.rx_dropped = 0
        self.reset()

    # ---------- reset ----------
    def reset(self):
        self.mem.clear()
        self.put("MCR", 0x00010002)       # DBF=1, SLEEP=1
        self.put("MSR", 0x00000C02)       # SLAK=1, RX=1, SAMP=1
        self.put("TSR", 0x1C000000)       # 3 puste skrzynki
        self.put("BTR", 0x01230000)
        self.put("FMR", 0x2A1C0E01)       # FINIT=1, CAN2SB=14
        self.fifo = [[], []]
        self.req_order = []

    def lint(self, msg):
        t = self.eng.now
        loc = self.eng.loc()
        if any(w[1] == msg and w[2] == loc for w in self.warnings[-20:]):
            return
        self.warnings.append((t, msg, loc))
        del self.warnings[:-100]
        self.eng.event("uwaga", f"{self.name}: {msg}", loc)

    # ---------- narzedzia do pol ----------
    def fget(self, reg, fname):
        pos, w = self.field(reg, fname)
        return (self.get(reg) >> pos) & ((1 << w) - 1)

    def fset(self, reg, fname, v):
        self.setf(reg, fname, v)

    def master(self):
        """Banki filtrow i FMR zyja we wspolnym bloku CAN1; CAN2 korzysta z jego rejestrow."""
        if self.name == "CAN2" and "CAN1" in self.eng.by_name:
            return self.eng.by_name["CAN1"]
        return self

    # ---------- BusNode ----------
    @property
    def online(self):
        return (not self.bus_off and self.recover_until is None and not self.fget("MSR", "INAK")
                and not self.fget("MSR", "SLAK") and self.bitrate_hz() > 0)

    @online.setter
    def online(self, v):
        pass

    @property
    def bitrate(self):
        return self.bitrate_hz()

    def bitrate_hz(self):
        clk = self.eng.prj.can_kernel_hz
        brp = self.fget("BTR", "BRP") + 1
        ts1 = self.fget("BTR", "TS1") + 1
        ts2 = self.fget("BTR", "TS2") + 1
        return clk / (brp * (1 + ts1 + ts2)) if clk else 0

    # ---------- odczyt / zapis rejestrow ----------
    def read(self, off):
        r, _ = self.regs_by_off.get(off, (None, 0))
        self.touched.add(off)
        if r is None:
            return 0
        n = r.name
        if n == "ESR":
            return self.esr()
        if n == "TSR":
            return self.tsr()
        if n in ("RF0R", "RF1R"):
            return self.rfr(int(n[2]))
        if n.startswith("sFIFOMailBox"):
            return self.rx_mailbox_word(n)
        return self.mem.get(off, 0)

    def esr(self):
        v = self.get("ESR") & ~(0xFF000000 | 0x00FF0000 | 7)
        v |= (min(self.tec, 255) << 16) | (min(self.rec, 255) << 24)
        v |= int(self.warn_state) | (int(self.passive) << 1) | (int(self.bus_off) << 2)
        return v

    def tsr(self):
        """CODE: numer nastepnej wolnej skrzynki (gdy wszystkie zajete: skrzynki o najnizszym priorytecie); LOWx: skrzynka
        o najnizszym priorytecie (RM0090, CAN_TSR). HAL_CAN_AddTxMessage korzysta z CODE."""
        v = self.get("TSR") & ~((3 << 24) | (7 << 29))
        empty = [i for i in range(3) if (v >> (26 + i)) & 1]
        pend = [i for i in range(3) if i not in empty]
        low = None
        if len(pend) > 1 or (pend and len(empty) < 2):
            def prio(x):
                f = self.mailbox_frame(x)
                return ((f.id >> 18) if f.ext else f.id, x)
            low = max(pend, key=prio)
            v |= 1 << (29 + low)
        code = empty[0] if empty else (low if low is not None else 0)
        return v | (code << 24)

    def rfr(self, i):
        v = self.get(f"RF{i}R")
        pos, w = self.field(f"RF{i}R", f"FMP{i}")
        v = (v & ~(((1 << w) - 1) << pos)) | (len(self.fifo[i]) << pos)
        return v

    def rx_mailbox_word(self, regname):
        i = int(regname[len("sFIFOMailBox["):].split("]")[0])
        what = regname.split(".")[1]
        if not self.fifo[i]:
            return self.mem.get(self.off_of(regname), 0)
        f, fmi, t = self.fifo[i][0]
        if what == "RIR":
            if f.ext:
                return ((f.id & 0x1FFFFFFF) << 3) | 4 | (int(f.rtr) << 1)
            return ((f.id & 0x7FF) << 21) | (int(f.rtr) << 1)
        if what == "RDTR":
            return (f.dlc & 0xF) | ((fmi & 0xFF) << 8) | ((t & 0xFFFF) << 16)
        d = f.data.ljust(8, b"\0")
        return int.from_bytes(d[0:4] if what == "RDLR" else d[4:8], "little")

    def write(self, off, val):
        r, idx = self.regs_by_off.get(off, (None, 0))
        self.touched.add(off)
        val &= 0xFFFFFFFF
        if r is None:
            self.mem[off] = val
            return
        n = r.name
        if n == "MCR":
            return self.w_mcr(val)
        if n == "MSR":
            m = (1 << 2) | (1 << 3) | (1 << 4)
            self.put("MSR", self.get("MSR") & ~(val & m))
            return
        if n == "TSR":
            return self.w_tsr(val)
        if n in ("RF0R", "RF1R"):
            return self.w_rfr(int(n[2]), val)
        if n == "ESR":
            lec = self.field("ESR", "LEC")
            m = ((1 << lec[1]) - 1) << lec[0]
            self.put("ESR", (self.get("ESR") & ~m) | (val & m))
            return
        if n == "BTR":
            if not self.fget("MSR", "INAK"):
                self.lint(f"zapis BTR=0x{val:08X} ZIGNOROWANY: BTR można zmieniać tylko w trybie inicjalizacji "
                          f"(ustaw MCR.INRQ i poczekaj na MSR.INAK=1)")
                return
            self.put("BTR", val)
            return
        if n.startswith("sTxMailBox"):
            return self.w_txbox(n, val)
        if n.startswith("sFIFOMailBox") or n == "MSR":
            return
        if n in ("FMR", "FM1R", "FS1R", "FFA1R", "FA1R") or n.startswith("sFilterRegister"):
            return self.w_filter(n, val)
        self.put(n, val)

    def w_mcr(self, val):
        old = self.get("MCR")
        ipos = self.field("MCR", "INRQ")[0]
        spos = self.field("MCR", "SLEEP")[0]
        rpos = self.field("MCR", "RESET")[0]
        if (val >> rpos) & 1:
            self.eng.event("can", f"{self.name}: MCR.RESET, kontroler wraca do stanu początkowego (sleep)")
            self.reset()
            return
        want_init = (val >> ipos) & 1
        want_sleep = (val >> spos) & 1
        self.put("MCR", val & ~(1 << rpos))
        now = self.eng.now
        sync = int(SYNC_BITS * NS / max(self.bitrate_hz(), 1)) if self.bitrate_hz() else 1000
        # zadanie wejscia w tryb inicjalizacji / wyjscia z niego
        if want_init and not (old >> ipos) & 1:
            self.sync_until = (now + sync, "init_on")
            self.eng.event("can", f"{self.name}: MCR.INRQ=1, kontroler przejdzie w inicjalizację po synchronizacji")
        elif not want_init and (old >> ipos) & 1:
            self.sync_until = (now + sync, "init_off")
            if self.bus_off:
                self.recover_until = now + int(128 * 11 * NS / max(self.bitrate_hz(), 1))
                self.eng.event("can", f"{self.name}: Bus-Off, odzyskiwanie 128x11 bitów recesywnych")
        if want_sleep and not (old >> spos) & 1:
            self.fset("MSR", "SLAK", 1)
        elif not want_sleep and (old >> spos) & 1:
            self.pending_mode = (now + sync, "sleep_off")

    def w_tsr(self, val):
        for i in range(3):
            base = 8 * i
            if (val >> base) & 1:                      # zapis 1 do RQCPx zeruje tez TXOKx, ALSTx, TERRx
                self.put("TSR", self.get("TSR") & ~(0xF << base))
            if (val >> (base + 7)) & 1:                # ABRQx: zadanie anulowania
                self.abort(i)

    def w_rfr(self, i, val):
        if (val >> 5) & 1 and self.fifo[i]:            # RFOM: zwolnij najstarsza ramke
            self.fifo[i].pop(0)
        clr = val & ((1 << 3) | (1 << 4))              # FULL, FOVR: zapis 1 zeruje
        self.put(f"RF{i}R", self.get(f"RF{i}R") & ~clr)

    # ---------- filtry ----------
    def w_filter(self, n, val):
        if n == "FMR":
            self.put("FMR", val)
            if val & 1 == 0:
                self.eng.event("can", f"{self.name}: FMR.FINIT=0, filtry aktywne")
            return
        finit = self.fget("FMR", "FINIT")
        fa = self.get("FA1R")
        if n == "FA1R":
            self.put("FA1R", val)
            return
        if not finit and n != "FA1R":
            # RM: konfiguracje banku mozna zmieniac gdy FINIT=1 albo gdy bank jest nieaktywny (FACTx=0)
            if n.startswith("sFilterRegister"):
                bank = int(n.split("[")[1].split("]")[0])
                if (fa >> bank) & 1:
                    self.lint(f"zapis {n}=0x{val:08X} ZIGNOROWANY: bank {bank} jest aktywny; "
                              f"najpierw FMR.FINIT=1 albo wyzeruj FA1R.FACT{bank}")
                    return
            else:
                self.lint(f"zapis {n}=0x{val:08X}: zmiana trybu/skali/FIFO banku przy FINIT=0 jest niedozwolona w RM, bity mogą nie działać")
        self.put(n, val)

    def banks(self, active_only=True):
        """(numer banku, tryb_listy, skala32, fifo) dla banków tego kontrolera (domyslnie tylko aktywnych)."""
        m = self.master()
        fmr = m.get("FMR")
        sb = (fmr >> 8) & 0x3F
        out = []
        if fmr & 1 and active_only:
            return out                    # FINIT=1: odbior wylaczony (RM0090: "When FINIT=1, CAN reception is deactivated")
        for b in range(28):
            if active_only and not (m.get("FA1R") >> b) & 1:
                continue
            if self.name == "CAN1" and b >= sb:
                continue
            if self.name == "CAN2" and b < sb:
                continue
            out.append((b, (m.get("FM1R") >> b) & 1, (m.get("FS1R") >> b) & 1, (m.get("FFA1R") >> b) & 1))
        return out

    def fmi_table(self):
        """Numer filtra (FMI) dla (bank, pod-filtr). RM0090: numeracja nie zalezy od aktywacji banku i jest osobna dla
        kazdego FIFO; bank 32-bitowy w trybie maski ma 1 numer, 32-bit lista i 16-bit maska po 2, 16-bit lista 4."""
        cnt = {0: 0, 1: 0}
        tab = {}
        for b, lst, s32, fifo in self.banks(active_only=False):
            n = (2 if lst else 1) if s32 else (4 if lst else 2)
            for k in range(n):
                tab[(b, k)] = cnt[fifo]
                cnt[fifo] += 1
        return tab

    def filter_entries(self):
        """Lista pojedynczych filtrow aktywnych banków w kolejnosci priorytetu RM0090: najpierw 32-bit przed 16-bit,
        potem lista przed maska, potem nizszy numer filtra."""
        m = self.master()
        ents = []
        fmi = self.fmi_table()
        for b, lst, s32, fifo in self.banks():
            fr1 = m.get(f"sFilterRegister[{b}].FR1")
            fr2 = m.get(f"sFilterRegister[{b}].FR2")
            subs = []
            if s32 and lst:
                subs = [("list32", fr1, None), ("list32", fr2, None)]
            elif s32:
                subs = [("mask32", fr1, fr2)]
            elif lst:
                subs = [("list16", fr1 & 0xFFFF, None), ("list16", fr1 >> 16, None),
                        ("list16", fr2 & 0xFFFF, None), ("list16", fr2 >> 16, None)]
            else:
                subs = [("mask16", fr1 & 0xFFFF, fr1 >> 16), ("mask16", fr2 & 0xFFFF, fr2 >> 16)]
            for k, (kind, a, mk) in enumerate(subs):
                num = fmi.get((b, k), 0)
                prio = (0 if kind.endswith("32") else 1, 0 if kind.startswith("list") else 1, num, b, k)
                ents.append({"bank": b, "fifo": fifo, "kind": kind, "a": a, "m": mk, "prio": prio, "sub": k, "fmi": num})
        ents.sort(key=lambda e: e["prio"])
        return ents

    @staticmethod
    def _id32(f):
        return ((f.id & 0x1FFFFFFF) << 3 | 4 | (int(f.rtr) << 1)) if f.ext else (((f.id & 0x7FF) << 21) | (int(f.rtr) << 1))

    @staticmethod
    def _id16(f):
        if f.ext:
            return (((f.id >> 18) & 0x7FF) << 5) | (int(f.rtr) << 4) | 8 | ((f.id >> 15) & 7)
        return ((f.id & 0x7FF) << 5) | (int(f.rtr) << 4)

    def entry_match(self, e, f):
        k = e["kind"]
        if k == "list32":
            return self._id32(f) & ~1 == e["a"] & ~1
        if k == "mask32":
            return (self._id32(f) ^ e["a"]) & e["m"] & ~1 == 0
        if k == "list16":
            return self._id16(f) == e["a"]
        return (self._id16(f) ^ e["a"]) & e["m"] == 0

    # ---------- Tx ----------
    def w_txbox(self, n, val):
        i = int(n[len("sTxMailBox["):].split("]")[0])
        what = n.split(".")[1]
        empty = (self.get("TSR") >> (26 + i)) & 1
        if what == "TIR":
            if not empty:
                if (val & 1) and not (self.get(n) & 1):
                    self.lint(f"zapis do skrzynki Tx {i} przy niepustej (TME{i}=0) ZIGNOROWANY; sprawdź TSR.TMEx przed zapisem")
                return
            self.put(n, val & ~1)
            if val & 1:
                self.request(i)
            return
        if not empty:
            self.lint(f"zapis {n} przy niepustej skrzynce {i} ZIGNOROWANY (TME{i}=0)")
            return
        self.put(n, val)

    def request(self, i):
        if self.fget("MSR", "INAK"):
            self.lint(f"TXRQ w trybie inicjalizacji (INAK=1): skrzynka {i} zostanie wysłana dopiero po wyjsciu z inicjalizacji")
        v = self.get("TSR")
        v &= ~(1 << (26 + i))                           # TMEi = 0
        v &= ~(0xF << (8 * i))
        self.put("TSR", v)
        self.put(TX_BOX.format(i, "TIR"), self.get(TX_BOX.format(i, "TIR")) | 1)
        self.req_order.append(i)
        self.eng.kick()

    def abort(self, i):
        if (self.get("TSR") >> (26 + i)) & 1:
            return
        if self.eng.bus.cur and self.eng.bus.cur[0] is self and self.eng.bus.cur[2] == i:
            return                                       # ramka w toku konczy sie normalnie
        self.free_mailbox(i, ok=False, err=False)
        self.put("TSR", self.get("TSR") | (1 << (8 * i)))   # RQCP
        self.eng.event("can", f"{self.name}: anulowano skrzynkę Tx {i} (ABRQ)")

    def free_mailbox(self, i, ok, err):
        v = self.get("TSR") | (1 << (26 + i))
        v |= (int(ok) << (8 * i + 1)) | (int(err) << (8 * i + 3)) | (1 << (8 * i))
        self.put("TSR", v)
        tir = TX_BOX.format(i, "TIR")
        self.put(tir, self.get(tir) & ~1)
        if i in self.req_order:
            self.req_order.remove(i)

    def mailbox_frame(self, i):
        tir = self.get(TX_BOX.format(i, "TIR"))
        tdt = self.get(TX_BOX.format(i, "TDTR"))
        lo = self.get(TX_BOX.format(i, "TDLR"))
        hi = self.get(TX_BOX.format(i, "TDHR"))
        ext = (tir >> 2) & 1
        fid = (tir >> 3) & 0x1FFFFFFF if ext else (tir >> 21) & 0x7FF
        dlc = tdt & 0xF
        data = (lo.to_bytes(4, "little") + hi.to_bytes(4, "little"))[:min(dlc, 8)]
        return Frame(fid, data, bool(ext), bool((tir >> 1) & 1))

    def tx_candidate(self):
        cand = [i for i in range(3) if not (self.get("TSR") >> (26 + i)) & 1 and (self.get(TX_BOX.format(i, "TIR")) & 1)]
        if not cand:
            return None
        if self.fget("MCR", "TXFP"):
            i = min(cand, key=lambda x: self.req_order.index(x) if x in self.req_order else 99)
        else:
            def key(x):
                f = self.mailbox_frame(x)
                return ((f.id >> 18) if f.ext else f.id, x)
            i = min(cand, key=key)
        return self.mailbox_frame(i), i

    def tx_done(self, handle, acked, now):
        self.free_mailbox(handle, ok=True, err=False)
        self.fset("ESR", "LEC", 0)
        self.tx_sent += 1
        self.dec_tec()

    def dec_tec(self):
        if self.tec > 0:
            self.tec -= 1
        self.update_state()

    def tx_error(self, now, kind="ack"):
        if kind != "ack" or not self.passive:            # wyjatek ISO 11898-1 dla bledu ACK w Error Passive
            self.tec += 8
        self.fset("ESR", "LEC", 3 if kind == "ack" else 4)
        self.update_state()

    def tx_failed(self, handle):
        """Bus wola to po bledzie, gdy NART=1: skrzynka nie jest ponawiana."""
        if self.fget("MCR", "NART"):
            self.free_mailbox(handle, ok=False, err=True)
            return True
        return False

    def rx_error(self, now, lec=6):
        self.rec = min(self.rec + 1, 255)
        self.fset("ESR", "LEC", lec)
        self.update_state()

    def update_state(self):
        was_p, was_w, was_bo = self.passive, self.warn_state, self.bus_off
        self.passive = self.tec >= 128 or self.rec >= 128
        self.warn_state = self.tec >= 96 or self.rec >= 96
        if self.tec > 255 and not self.bus_off:
            self.bus_off = True
            self.eng.event("can", f"{self.name}: BUS-OFF (TEC={self.tec})")
            if self.fget("MCR", "ABOM"):
                self.recover_until = self.eng.now + int(128 * 11 * NS / max(self.bitrate_hz(), 1))
                self.eng.event("can", f"{self.name}: ABOM=1, automatyczne odzyskiwanie po 128x11 bitach")
            else:
                self.eng.event("can", f"{self.name}: ABOM=0, program musi ustawić i zdjąć MCR.INRQ, aby wrócić na magistralę")
        if self.passive != was_p:
            self.eng.event("can", f"{self.name}: Error Passive {'WEJSCIE' if self.passive else 'WYJSCIE'} (TEC={self.tec}, REC={self.rec})")
        if (self.passive != was_p or self.warn_state != was_w or self.bus_off != was_bo):
            self.fset("MSR", "ERRI", 1)

    def force_tec(self, v):
        self.tec = int(v)
        self.update_state()

    # ---------- Rx ----------
    def rx(self, frame, now):
        self.rec = 120 if self.rec > 127 else max(0, self.rec - 1)
        self.fset("ESR", "LEC", 0)
        self.update_state()
        for e in self.filter_entries():
            if self.entry_match(e, frame):
                return self.store(frame, e["fifo"], e["fmi"], now)
        self.rx_filtered_out += 1
        return False

    def store(self, frame, fifo, fmi, now):
        q = self.fifo[fifo]
        rfr = f"RF{fifo}R"
        lock = self.fget("MCR", "RFLM")
        t = (now // 1000) & 0xFFFF
        if len(q) >= 3:
            self.rx_dropped += 1
            self.setf(rfr, f"FOVR{fifo}", 1)
            if not lock and q:
                q[-1] = (frame, fmi, t)                  # RFLM=0: nowa ramka nadpisuje ostatnio zapisana (RM0090)
                return True
            return False                                 # RFLM=1: nowa ramka odrzucona
        q.append((frame, fmi, t))
        self.rx_stored += 1
        if len(q) == 3:
            self.setf(rfr, f"FULL{fifo}", 1)
        return True

    # ---------- czas i przerwania ----------
    def next_event(self, now):
        ts = [x[0] for x in (self.sync_until, self.pending_mode) if x]
        if self.recover_until is not None:
            ts.append(self.recover_until)
        return min(ts) if ts else None

    def advance(self, now):
        if self.sync_until and now >= self.sync_until[0]:
            _, what = self.sync_until
            self.sync_until = None
            if what == "init_on":
                self.fset("MSR", "INAK", 1)
            else:
                self.fset("MSR", "INAK", 0)
                self.start_operation()
        if self.pending_mode and now >= self.pending_mode[0]:
            self.pending_mode = None
            self.fset("MSR", "SLAK", 0)
        if self.recover_until is not None and now >= self.recover_until:
            self.recover_until = None
            self.bus_off = False
            self.tec = self.rec = 0
            self.update_state()
            self.eng.event("can", f"{self.name}: wyszedł z Bus-Off, liczniki wyzerowane")

    def start_operation(self):
        self.eng.event("can", f"{self.name}: kontroler dołączony do magistrali, bitrate={self.bitrate_hz():.0f} bit/s")
        self.run_lint()

    def run_lint(self):
        br = self.bitrate_hz()
        if br <= 0:
            self.lint("bitrate = 0 (sprawdź BTR i zegar APB1 w can_kernel_hz)")
        sjw = self.fget("BTR", "SJW")
        ts2 = self.fget("BTR", "TS2")
        if sjw > ts2:
            self.lint(f"BTR: SJW ({sjw + 1} tq) większe niż TS2 ({ts2 + 1} tq); wg specyfikacji SJW nie powinno przekraczać TS2")
        m = self.master()
        if m.fget("FMR", "FINIT"):
            self.lint("FMR.FINIT=1: filtry są w trybie inicjalizacji i NIE ODBIERAJĄ ramek; ustaw FINIT=0 po konfiguracji")
        if not m.fget("FMR", "FINIT") and not self.banks():
            self.lint("żaden bank filtrów nie jest aktywny (FA1R=0): kontroler odrzuci wszystkie ramki")

    def irq_lines(self):
        ier = self.get("IER")
        def b(name):
            return (ier >> self.field("IER", name)[0]) & 1
        tsr = self.get("TSR")
        tx = bool((tsr & 0x00010101) and b("TMEIE"))
        r = []
        for i in (0, 1):
            q = len(self.fifo[i])
            rfr = self.get(f"RF{i}R")
            r.append(bool((q and b(f"FMPIE{i}")) or ((rfr >> 3) & 1 and b(f"FFIE{i}")) or ((rfr >> 4) & 1 and b(f"FOVIE{i}"))))
        errs = bool(b("ERRIE") and self.fget("MSR", "ERRI") and
                    ((self.warn_state and b("EWGIE")) or (self.passive and b("EPVIE")) or (self.bus_off and b("BOFIE")) or
                     (self.fget("ESR", "LEC") and b("LECIE"))))
        return {self.irq_names[0]: tx, self.irq_names[1]: r[0], self.irq_names[2]: r[1], self.irq_names[3]: errs}

    # ---------- widok dla UI ----------
    def ram_view(self):
        out = {"kind": "bxcan", "tx": [], "fifo": [], "filters": [], "areas": {}}
        for i in range(3):
            tir = self.get(TX_BOX.format(i, "TIR"))
            tdt = self.get(TX_BOX.format(i, "TDTR"))
            f = self.mailbox_frame(i)
            out["tx"].append({"i": i, "id": f"{f.id:X}" + ("x" if f.ext else ""), "dlc": tdt & 0xF, "data": f.data.hex(" "),
                              "pending": bool(tir & 1), "empty": bool((self.get("TSR") >> (26 + i)) & 1)})
        for i in (0, 1):
            for k, (f, fmi, t) in enumerate(self.fifo[i]):
                out["fifo"].append({"fifo": i, "i": k, "id": f"{f.id:X}" + ("x" if f.ext else ""), "dlc": f.dlc,
                                    "data": f.data.hex(" "), "fmi": fmi})
        for e in self.filter_entries():
            out["filters"].append({"bank": e["bank"], "sub": e["sub"], "fifo": e["fifo"], "kind": e["kind"], "fmi": e["fmi"],
                                   "a": f"{e['a']:X}", "m": "" if e["m"] is None else f"{e['m']:X}"})
        return out

    def stats(self):
        return {"tec": self.tec, "rec": self.rec, "passive": self.passive, "bus_off": self.bus_off,
                "bitrate": round(self.bitrate_hz()), "tx_sent": self.tx_sent, "rx_stored": self.rx_stored,
                "rx_filtered_out": self.rx_filtered_out, "rx_dropped": self.rx_dropped,
                "init": bool(self.fget("MSR", "INAK")) or bool(self.fget("MSR", "SLAK")), "lec": self.fget("ESR", "LEC"),
                "ctrl": "bxCAN"}

    def timing(self):
        return {"brp": self.fget("BTR", "BRP") + 1, "tseg1": self.fget("BTR", "TS1") + 1, "tseg2": self.fget("BTR", "TS2") + 1,
                "sjw": self.fget("BTR", "SJW") + 1, "clk": self.eng.prj.can_kernel_hz}

    live_regs = ("ESR", "RF0R", "RF1R", "MSR", "TSR")
