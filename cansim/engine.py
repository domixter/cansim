import ctypes, ctypes.util, itertools, os, shutil, threading
from collections import deque, Counter

from .bus import NS
from .models.base import Plain, RCC, USART, BasicTimer
from .models.fdcan import FDCAN
from .models.bxcan import BxCAN

RD_T = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
WR_T = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_uint32, ctypes.c_uint32)
TR_T = ctypes.CFUNCTYPE(None, ctypes.c_char_p)
NV_T = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_int, ctypes.c_uint32)
NVG_T = ctypes.CFUNCTYPE(ctypes.c_uint32, ctypes.c_int, ctypes.c_int)
WT_T = ctypes.CFUNCTYPE(None, ctypes.c_uint32)
ACCESS_NS = 40                 # koszt jednego dostepu do rejestru w czasie wirtualnym
ACCESS_YIELD_NS = 10_000       # po tylu ns kosztow zadanie oddaje sterowanie
_LIBSEQ = itertools.count()
CT = {"u8": ctypes.c_uint8, "i8": ctypes.c_int8, "u16": ctypes.c_uint16, "i16": ctypes.c_int16, "u32": ctypes.c_uint32,
      "i32": ctypes.c_int32, "u64": ctypes.c_uint64, "i64": ctypes.c_int64, "ulong": ctypes.c_ulong, "long": ctypes.c_long,
      "f32": ctypes.c_float, "f64": ctypes.c_double}


class Task:
    def __init__(self, eng, name, fn, args=()):
        self.eng, self.name, self.fn, self.args = eng, name, fn, args
        self.resume = threading.Semaphore(0)
        self.wake_ns = eng.now
        self.done = False
        self.result = None
        self.error = None
        self.budget = 0
        self.started = False
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"fw-{name}")
        self.thread.start()

    def _run(self):
        self.resume.acquire()
        self.eng.tls.task = self
        try:
            self.result = self.fn(*self.args)
        except BaseException as e:           # noqa
            self.error = repr(e)
        self.done = True
        self.eng.sched.release()


class Engine:
    """Jedna plytka (mikrokontroler z wlasnym firmware). Czas i magistrala naleza do World."""

    def __init__(self, prj, built, workdir, world=None, name=None):
        from .world import World
        self.world = world or World()
        self.name = name or prj.name
        self.prj, self.dev, self.built = prj, built["dev"], built
        self.ids = built["ids"]
        self.tls = threading.local()
        self.sched = threading.Semaphore(0)
        self.lock = threading.RLock()
        self.tasks = []
        self.events = deque(maxlen=4000)
        self.calls = Counter()
        self.reglog = deque(maxlen=3000)
        self.trace_regs = True
        self.trace_calls = True
        self.nvic_en, self.nvic_prio, self.nvic_pend = set(), {}, set()
        self.isr_count = Counter()
        self.missing_handlers = set()
        self.sys_next = None
        self.depth = 0
        self.reset_requested = False
        # kopia biblioteki o unikalnej nazwie => swieze zmienne globalne po kazdym resecie i osobne dla kazdej plytki
        for old in os.listdir(workdir):
            if old.startswith("fw_"):
                try:
                    os.remove(os.path.join(workdir, old))   # Linux: zaladowana biblioteka zostaje w pamieci
                except OSError:
                    pass
        path = os.path.join(workdir, f"fw_{os.getpid()}_{next(_LIBSEQ)}{os.path.splitext(built['lib'])[1]}")
        shutil.copy(built["lib"], path)
        self.lib = ctypes.CDLL(path)
        self.msgram = memoryview((ctypes.c_uint8 * 16384).in_dll(self.lib, "cansim_msgram")).cast("B")
        self.irq_by_name = self.dev.irqs
        self.irq_name_by_num = {v: k for k, v in self.dev.irqs.items()}
        self.models = [None] * len(self.ids)
        self.by_name = {}
        for name, i in self.ids.items():
            tname = self.dev.periphs[name]
            m = self._make_model(name, tname)
            self.models[i] = m
            self.by_name[name] = m
        self.active = [m for m in self.models if m.has_events]
        self.mcu_can = [m for m in self.models if isinstance(m, (FDCAN, BxCAN)) and m.name == prj_can_name(prj)]
        self.can = self.mcu_can[0] if self.mcu_can else None
        if self.can:
            self.can.board = self
        self.regstat = {}       # (model, offset) -> [odczyty, zapisy, czas ostatniego zapisu, ostatnio zapisana wartosc, OR wartosci]
        self._callbacks()
        self.world.add_board(self)
        try:
            ctypes.c_uint32.in_dll(self.lib, "SystemCoreClock").value = prj.core_hz
        except ValueError:
            self.event("uwaga", "SystemCoreClock nie jest widoczny z biblioteki; SysTick liczony z core_hz projektu")

    def _make_model(self, name, tname):
        if tname.startswith("FDCAN_Global"):
            return FDCAN(self, name, tname)
        if tname == "CAN_TypeDef":
            return BxCAN(self, name, tname)
        if name == "RCC":
            return RCC(self, name, tname)
        if tname.startswith("USART"):
            return USART(self, name, tname)
        if tname.startswith("TIM_") and name.startswith("TIM"):
            return BasicTimer(self, name, tname)
        return Plain(self, name, tname)

    # ---------- czas i magistrala wspolne (World) ----------
    @property
    def now(self):
        return self.world.now

    @property
    def bus(self):
        return self.world.bus

    @property
    def nodes(self):
        return self.world.nodes

    def add_node(self, node):
        return self.world.add_node(node)

    def loc(self):
        """'plik.c:linia' ostatniego dostepu do rejestru w firmware (z #line, czyli w oryginalnym pliku)."""
        try:
            f = ctypes.c_char_p.in_dll(self.lib, "cansim_loc_file").value
            ln = ctypes.c_int.in_dll(self.lib, "cansim_loc_line").value
        except ValueError:
            return ""
        return f"{f.decode(errors='replace')}:{ln}" if f and ln else ""

    # ---------- callbacki z C ----------
    def _callbacks(self):
        def rd(i, off):
            v = self.models[i].read(off)
            st = self.regstat.setdefault((i, off), [0, 0, None, None, 0])
            st[0] += 1
            st[4] |= v & 0xFFFFFFFF                      # bity widziane od ostatniego odswiezenia UI
            self._account()
            if self.trace_regs and self._traced(i):
                self._reglog("R", i, off, v)
            return v

        def wr(i, off, val):
            self.models[i].write(off, val)
            st = self.regstat.setdefault((i, off), [0, 0, None, None, 0])
            st[1] += 1
            st[2], st[3] = self.now, val & 0xFFFFFFFF
            st[4] |= self.models[i].mem.get(off, 0)
            self._account()
            if self.trace_regs and self._traced(i):
                self._reglog("W", i, off, val)

        def trace(fn):
            name = fn.decode()
            self.calls[name] += 1
            if self.trace_calls and name not in self.prj.quiet_funcs and name != "SysTick_Handler":
                self.events.append((self.now, "call", name))

        def nvic(op, irq, val):
            if op == 0:
                self.nvic_en.add(irq)
            elif op == 1:
                self.nvic_en.discard(irq)
            elif op == 2:
                self.nvic_prio[irq] = val
            elif op == 3:
                self.nvic_pend.add(irq)
            elif op == 4:
                self.nvic_pend.discard(irq)
            elif op == 9:
                self.reset_requested = True
                self.event("reset", "NVIC_SystemReset()")

        def nvic_get(op, irq):
            if op == 0:
                return int(irq in self.nvic_en)
            if op == 2:
                return self.nvic_prio.get(irq, 0)
            if op == 3:
                return int(irq in self.nvic_pend)
            return 0

        def wait(us):
            self.task_wait(us)

        self._cb = (RD_T(rd), WR_T(wr), TR_T(trace), NV_T(nvic), NVG_T(nvic_get), WT_T(wait))
        self.lib.cansim_set_callbacks.argtypes = [RD_T, WR_T, TR_T, NV_T, NVG_T, WT_T]
        self.lib.cansim_set_callbacks(*self._cb)

    def _traced(self, i):
        return self.models[i].name in self.prj.trace_periphs

    def _reglog(self, rw, i, off, v):
        m = self.models[i]
        r, idx = m.regs_by_off.get(off, (None, 0))
        nm = (r.name if r and r.count == 1 else f"{r.name}[{idx}]") if r else f"+0x{off:X}"
        self.reglog.append((self.now, rw, m.name, nm, v))

    def kick(self):
        """Miejsce na budzenie petli czasu po zdarzeniu z peryferium (obecnie petla i tak liczy zdarzenia co iteracje)."""

    def event(self, kind, text, loc=""):
        self.events.append((self.now, kind, text, loc))

    # ---------- zadania (watki firmware) ----------
    def cur_task(self):
        return getattr(self.tls, "task", None)

    def _account(self):
        t = self.cur_task()
        if t is not None:
            t.budget += ACCESS_NS
            if t.budget >= ACCESS_YIELD_NS:
                w, t.budget = t.budget, 0
                self._yield(t, self.now + w)

    def task_wait(self, us):
        t = self.cur_task()
        if t is None:                                   # wywolane z ISR: przesun czas zagniezdzonie
            if us not in (0, 0xFFFFFFFF):
                self.world.run_until(self.now + us * 1000)
            return
        if us == 0xFFFFFFFF:                            # WFI
            us = 50
        self._yield(t, self.now + max(int(us), 1) * 1000)

    def _yield(self, t, wake):
        t.wake_ns = wake
        self.sched.release()
        t.resume.acquire()

    def _resume(self, t):
        t.started = True
        t.resume.release()
        self.sched.acquire()

    def start_task(self, name, fn, *args):
        t = Task(self, name, fn, args)
        self.tasks.append(t)
        return t

    def call(self, fname, args=(), timeout_ms=2000, sig=None):
        """Wywolaj funkcje C (z UI/konsoli). sig: sygnatura z symbols.py (typy argumentow i wyniku). Zwraca (wynik, blad)."""
        f = getattr(self.lib, fname, None)
        if f is None:
            return None, f"brak funkcji {fname} w skompilowanym kodzie (funkcje static nie są dostępne z zewnątrz)"
        params = (sig or {}).get("params")
        conv = []
        try:
            if params is not None:
                if len(params) != len(args):
                    return None, f"{fname} oczekuje {len(params)} argumentów, podano {len(args)}"
                types = []
                for p, a in zip(params, args):
                    k = p.get("key")
                    if k is None:
                        return None, f"argument {p['name']} ma typ {p['type']}: wskaźniki i struktury nie są obsługiwane"
                    ct = CT[k]
                    types.append(ct)
                    conv.append(ct(float(a)) if k in ("f32", "f64") else ct(int(str(a), 0)))
                f.argtypes = types
                rk = sig.get("ret_key")
                f.restype = CT[rk] if rk else (None if sig.get("ret") == "void" else ctypes.c_long)
            else:
                conv = [ctypes.c_long(int(str(a), 0)) for a in args]
                f.restype = ctypes.c_long
        except (ValueError, KeyError) as e:
            return None, f"zły argument: {e}"
        t = self.start_task(f"call:{fname}", lambda: f(*conv))
        self.event("wywolanie", f"{fname}({', '.join(map(str, args))})")
        end = self.now + timeout_ms * 1_000_000
        while not t.done and self.now < end:
            self.world.run_until(min(end, self.now + 1_000_000))
        if not t.done:
            return None, "funkcja nie zakończyła się w limicie czasu (pętla oczekująca albo nieskończona pętla?)"
        r = t.result
        if isinstance(r, float):
            r = round(r, 6)
        return r, t.error

    def boot(self):
        for fname in self.prj.init:
            if hasattr(self.lib, fname):
                self.start_task(fname, getattr(self.lib, fname))
                self.event("start", f"uruchomiono {fname}()")
            else:
                self.event("uwaga", f"brak funkcji startowej {fname}")

    # ---------- SysTick, NVIC ----------
    def _systick_regs(self):
        a = (ctypes.c_uint32 * 3).in_dll(self.lib, "cansim_core_systick")
        return a[0], a[1]

    def _systick_period(self):
        ctrl, load = self._systick_regs()
        if not (ctrl & 1):
            return None
        try:
            core = ctypes.c_uint32.in_dll(self.lib, "SystemCoreClock").value or self.prj.core_hz
        except ValueError:
            core = self.prj.core_hz
        return max(int((load + 1) * NS / core), 1000)

    def _handler(self, irqname):
        h = irqname.replace("_IRQn", "_IRQHandler")
        f = getattr(self.lib, h, None)
        if f is None and h not in self.missing_handlers:
            self.missing_handlers.add(h)
            self.event("uwaga", f"przerwanie włączone, ale firmware nie ma funkcji {h} (na sprzęcie: Default_Handler)")
        return f

    def deliver_irqs(self):
        if ctypes.c_uint32.in_dll(self.lib, "cansim_primask").value:
            return
        for _ in range(300):
            active = set()
            for m in self.active:
                for nm, lv in m.irq_lines().items():
                    n = self.irq_by_name.get(nm)
                    if lv and n is not None and n in self.nvic_en:
                        active.add(n)
            active |= {n for n in self.nvic_pend if n in self.nvic_en}
            if not active:
                return
            n = min(active, key=lambda x: (self.nvic_prio.get(x, 0), x))
            self.nvic_pend.discard(n)
            name = self.irq_name_by_num[n]
            f = self._handler(name)
            self.isr_count[name] += 1
            if f is None:
                self.nvic_en.discard(n)
                return
            f()
        self.event("uwaga", "burza przerwan: ISR wywoływane >300 razy bez zdjęcia źródła (flaga nie kasowana?)")
        self.isr_storm = True

    # ---------- petla czasu (wolana przez World) ----------
    def resume_due(self):
        for t in list(self.tasks):
            if not t.done and t.wake_ns <= self.now:
                self._resume(t)
        self.tasks = [t for t in self.tasks if not t.done or t.error]

    def has_due_task(self):
        return any((not t.done) and t.wake_ns <= self.now for t in self.tasks)

    def next_times(self, c):
        for t in self.tasks:
            if not t.done:
                c.append(max(t.wake_ns, self.now))
        for m in self.active:
            ne = m.next_event(self.now)
            if ne is not None:
                c.append(ne)
        p = self._systick_period()
        if p:
            if self.sys_next is None:
                self.sys_next = self.now + p
            c.append(self.sys_next)
        else:
            self.sys_next = None

    def advance_models(self):
        progressed = False
        for m in self.active:
            ne = m.next_event(self.now)
            if ne is not None and ne <= self.now:
                m.advance(self.now)
                progressed = True
        return progressed

    def interrupts(self):
        if self.sys_next is not None and self.now >= self.sys_next:
            p = self._systick_period() or 1_000_000
            self.sys_next += p
            if self.sys_next < self.now:
                self.sys_next = self.now + p
            ctrl, _ = self._systick_regs()
            if ctrl & 2:
                f = getattr(self.lib, "SysTick_Handler", None)
                self.isr_count["SysTick"] += 1
                if f and not ctypes.c_uint32.in_dll(self.lib, "cansim_primask").value:
                    f()
        self.deliver_irqs()

    def run_until(self, t_end):
        self.world.run_until(t_end)

    def step_ms(self, ms):
        self.world.step_ms(ms)

    # ---------- odczyty dla UI ----------
    def _gaddr(self, name, typ, index=0):
        ct = CT.get(typ or "u32", ctypes.c_uint32)
        base = ct.in_dll(self.lib, name)
        return ct.from_address(ctypes.addressof(base) + int(index) * ctypes.sizeof(ct))

    def read_global(self, name, typ, index=0):
        try:
            v = self._gaddr(name, typ, index).value
            return round(v, 6) if isinstance(v, float) else v
        except (ValueError, TypeError):
            return None

    def write_global(self, name, typ, value, index=0):
        """Zapis zmiennej globalnej firmware (jak debugger). Zwraca None albo opis bledu."""
        try:
            obj = self._gaddr(name, typ, index)
        except ValueError:
            return f"brak zmiennej {name} w skompilowanym kodzie (zmienne static nie są dostępne z zewnątrz)"
        try:
            obj.value = float(value) if typ in ("f32", "f64") else int(str(value), 0)
        except ValueError:
            return f"zła wartość {value!r} dla typu {typ}"
        self.event("zapis", f"{name}{'[%d]' % int(index) if index else ''} = {value}")
        return None


def prj_can_name(prj):
    return prj.can_peripheral
