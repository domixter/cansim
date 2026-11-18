import os
import re
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import system as sysmod
from .build import FAMILIES, Project, auto_config, build
from . import cmsis
from .bus import Frame
from .engine import Engine
from .nodes.generic import GenericNode
from .nodes.odrive import ODriveNode, decode as odrive_decode
from .world import World
from .console import Console

NODE_KEYS = {"odrive": {"node_id", "bitrate", "hb_ms", "enc_ms", "vel_tau_ms", "watchdog_ms", "name"},
             "generic": {"name", "bitrate"}}
PHYS_KEYS = {"pos_m", "termination", "transceiver", "vcc", "stub_m", "gnd", "osc_ppm", "name", "bitrate", "node_id"}


def decode_signals(f, spec):
    """Dekodowanie wg definicji z cansim_system.json -> "messages": {"0x100": {"name": "power", "signals":
    [["battery_mv", 0, 16, false, 1, "mV"], ...]}} (start_bit, dlugosc, ze znakiem, skala, jednostka; little-endian)."""
    v = int.from_bytes(f.data.ljust(8, b"\0"), "little")
    out = []
    for sig in spec.get("signals", []):
        name, start, ln = sig[0], int(sig[1]), int(sig[2])
        signed = bool(sig[3]) if len(sig) > 3 else False
        scale = float(sig[4]) if len(sig) > 4 else 1.0
        unit = sig[5] if len(sig) > 5 else ""
        x = (v >> start) & ((1 << ln) - 1)
        if signed and x >> (ln - 1):
            x -= 1 << ln
        val = x * scale
        out.append(f"{name}={val:g}{unit}")
    return f"{spec.get('name', '')}: " + " ".join(out)


def _func_at(text, line):
    """Nazwa funkcji C obejmujacej linie (heurystyka: najblizsza wczesniejsza definicja od kolumny 0)."""
    lines = text.splitlines()
    for i in range(min(line, len(lines)) - 1, -1, -1):
        m = re.match(r"^[A-Za-z_][\w\s\*]*?\b([A-Za-z_]\w*)\s*\([^;]*$", lines[i])
        if m and m.group(1) not in ("if", "for", "while", "switch", "return"):
            return m.group(1)
    return ""


TEMPLATES = Path(__file__).parent / "templates"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class App:
    def __init__(self, path=None):
        self.lock = threading.RLock()
        if path:
            self.sys, self.sys_path = sysmod.load(path)
        else:
            self.sys, self.sys_path = sysmod.empty(), None
        self.builds = {}            # id plytki -> (Project, wynik build)
        self.world = None
        self.engines = {}           # id plytki -> Engine
        self.nodes = {}             # id wezla -> obiekt wezla
        self.running = False
        self.speed = 1.0
        self.erc = []
        self._erc_t = -1
        self._regseen = {}          # do liczenia aktywnosci rejestrow miedzy odswiezeniami UI
        self._regshow = set()
        self.console = Console(self)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.rebuild()

    # ---------- budowanie ----------
    def _board_cfg(self, bid):
        return next((b for b in self.sys["boards"] if b["id"] == bid), None)

    def _base(self):
        return self.sys_path.parent if self.sys_path else Path.cwd()

    def _project(self, b):
        if "config" in b:
            return Project.from_dict(b["config"], self._base())
        return Project.load(b["project"])

    def _build_one(self, b):
        try:
            prj = self._project(b)
            res = build(prj, Path(prj.root) / ".cansim_build" / b["id"])
            res["log"] = res["log"] or "OK"
        except Exception:
            prj, res = None, {"ok": False, "log": traceback.format_exc()}
        self.builds[b["id"]] = (prj, res)
        return res["ok"]

    def rebuild(self, bid=None):
        with self.lock:
            if bid:
                b = self._board_cfg(bid)
                if b and self._build_one(b) and self.world is not None:
                    self._replace_board(b)
                return
            self.running = False
            for b in self.sys["boards"]:
                self._build_one(b)
            self.reset()

    def _make_engine(self, b):
        prj, res = self.builds.get(b["id"], (None, None))
        if not res or not res.get("ok"):
            return None
        try:
            if os.name == "nt" and __import__("struct").calcsize("P") == 4:
                raise RuntimeError("Python jest 32-bitowy, a gcc buduje 64-bitowa bibliotekę. Zainstaluj 64-bitowego Pythona.")
            e = Engine(prj, res, str(Path(res["lib"]).parent), self.world, name=b.get("name") or prj.name)
        except Exception:
            res["ok"] = False
            res["log"] = ("Kompilacja się udała, ale nie udało się załadować/uruchomic skompilowanego kodu:\n\n"
                          + traceback.format_exc() + "\n" + res.get("log", ""))
            self.world.event("blad", f"{b.get('name')}: nie udało się uruchomić (zakładka Build)")
            return None
        e.board_id = b["id"]
        self.engines[b["id"]] = e
        e.boot()
        return e

    def _replace_board(self, b):
        old = self.engines.pop(b["id"], None)
        if old:
            self.world.remove_board(old)
        self._make_engine(b)
        self.world.event("plytka", f"{b.get('name')}: przebudowana i uruchomiona ponownie (pozostałe węzły działają dalej)")
        self.apply_phys()

    def _make_node(self, n):
        typ = n.get("type", "odrive")
        if typ == "terminator":
            return None
        cls = ODriveNode if typ == "odrive" else GenericNode
        kw = {k: v for k, v in n.items() if k in NODE_KEYS["odrive" if typ == "odrive" else "generic"]}
        obj = cls(**kw)
        self.nodes[n["id"]] = obj
        self.world.add_node(obj)
        return obj

    def reset(self):
        with self.lock:
            self.running = False
            if self.world:
                for e in self.world.boards:
                    for t in e.tasks:
                        t.done = True
            self.world = World()
            self.engines, self.nodes = {}, {}
            for b in self.sys["boards"]:
                self._make_engine(b)
            for n in self.sys["nodes"]:
                try:
                    self._make_node(n)
                except Exception:
                    self.world.event("blad", f"węzeł {n.get('id')}: {traceback.format_exc()[-300:]}")
            self.apply_phys()

    # ---------- schemat ----------
    def apply_phys(self):
        """Fizyka, ktora wplywa na symulacje (zalozenia modelu): brak terminacji => kazda ramka z bledem."""
        if not self.world:
            return
        els = self.sys["boards"] + self.sys["nodes"]
        nt = sum(1 for e in els if e.get("termination") or e.get("type") == "terminator")
        self.world.bus.forced_error = "brak terminacji 120 Ω" if nt == 0 else ""
        for n in self.sys["nodes"]:
            o = self.nodes.get(n["id"])
            if o is not None and "bitrate" in n:
                o.bitrate = int(n["bitrate"])
        self._erc_t = -1

    def patch(self, eid, changes):
        with self.lock:
            el = next((e for e in self.sys["boards"] + self.sys["nodes"] if e["id"] == eid), None)
            if el is None:
                return "brak elementu"
            for k, v in changes.items():
                if k in PHYS_KEYS:
                    el[k] = v
            if eid in self.engines and "name" in changes:
                self.engines[eid].name = changes["name"]
            o = self.nodes.get(eid)
            if o is not None and "name" in changes:
                o.name = changes["name"]
            if o is not None and "node_id" in changes and hasattr(o, "node_id"):
                old = o.node_id
                o.node_id = int(changes["node_id"])
                if o.name == f"ODrive {old}" and "name" not in el:
                    o.name = f"ODrive {o.node_id}"
            self.apply_phys()
            return "ok"

    def set_bus(self, changes):
        with self.lock:
            if "name" in changes:
                self.sys["name"] = str(changes["name"])
            for k in ("length_m", "ns_per_m"):
                if k in changes:
                    self.sys["bus"][k] = float(changes[k])
            self._erc_t = -1
            return "ok"

    def _free_pos(self, L):
        """Srodek najwiekszej wolnej przerwy na kablu (zeby nowe elementy nie nakladaly sie na siebie)."""
        ps = sorted({0.0, float(L)} | {float(e["pos_m"]) for e in self.sys["boards"] + self.sys["nodes"]})
        a, b = max(zip(ps, ps[1:]), key=lambda x: x[1] - x[0]) if len(ps) > 1 else (0.0, float(L))
        return round((a + b) / 2, 2)

    def add(self, kind, project=None, **kw):
        with self.lock:
            L = self.sys["bus"]["length_m"]
            if kind in ("board", "board_folder", "board_template"):
                fam = kw.get("family") or "h7"
                b = {"id": sysmod.new_id(self.sys, "b"), "pos_m": self._free_pos(L) if self.sys["boards"] or self.sys["nodes"] else 0.0}
                if not self.sys["boards"] and not self.sys["nodes"]:
                    b["termination"] = True
                if kind == "board":
                    p = Path(project).expanduser()
                    if not p.is_absolute():
                        p = (self._base() / p).resolve()
                    if not p.is_file():
                        return f"nie ma pliku {p}"
                    b.update(name=kw.get("name") or p.parent.name, project=str(p))
                elif kind == "board_folder":
                    d = Path(project).expanduser().resolve()
                    if not d.is_dir():
                        return f"nie ma folderu {d}"
                    b.update(name=kw.get("name") or d.name, config=auto_config(d, fam, kw.get("name")))
                    if not b["config"]["sources"]:
                        return f"w folderze {d} nie ma plików .c"
                else:
                    d = Path(project).expanduser().resolve()
                    if d.exists() and any(d.iterdir()):
                        return f"folder {d} już istnieje i nie jest pusty; podaj nowa nazwę"
                    d.mkdir(parents=True, exist_ok=True)
                    shutil.copy(TEMPLATES / fam / "main.c", d / "main.c")
                    cfg = auto_config(d, fam, kw.get("name") or d.name)
                    cfg["watch"] = [{"name": n, "type": "u32"} for n in ("rx_frames", "tx_busy_drops", "ms_tick")]
                    b.update(name=kw.get("name") or d.name, config=cfg)
                sysmod.normalize({"boards": [b], "nodes": [], "bus": {}}, self._base())
                self.sys["boards"].append(b)
                ok = self._build_one(b)
                if ok:
                    self._make_engine(b)
                self.apply_phys()
                return "ok" if ok else "błąd kompilacji (zobacz zakładkę Build tej płytki)"
            n = {"id": sysmod.new_id(self.sys, {"odrive": "n", "generic": "n", "terminator": "t"}[kind]), "type": kind,
                 "pos_m": round(L, 2) if kind == "terminator" else (self._free_pos(L) if self.sys["boards"] or self.sys["nodes"] else 0.0)}
            if kind == "odrive":
                used = {x.get("node_id") for x in self.sys["nodes"] if x.get("type") == "odrive"}
                n["node_id"] = next(i for i in range(1, 64) if i not in used)
                n["transceiver"] = "wbudowany"
            if kind == "generic":
                n["name"] = kw.get("name") or f"Węzeł {len(self.sys['nodes']) + 1}"
            sysmod.normalize({"boards": [], "nodes": [n], "bus": {}}, self._base())
            if kind == "terminator":
                n["termination"] = False
            self.sys["nodes"].append(n)
            self._make_node(n)
            self.apply_phys()
            return "ok"

    def remove(self, eid):
        with self.lock:
            for lst in (self.sys["boards"], self.sys["nodes"]):
                for e in list(lst):
                    if e["id"] == eid:
                        lst.remove(e)
            if eid in self.engines:
                self.world.remove_board(self.engines.pop(eid))
            if eid in self.nodes:
                self.world.remove_node(self.nodes.pop(eid))
            self.apply_phys()
            return "ok"

    def save_system(self, path=None):
        with self.lock:
            if path:
                p = Path(path).expanduser()
                if p.suffix.lower() != ".json":
                    p = p / "cansim_system.json" if p.is_dir() else p.with_suffix(".json")
                self.sys_path = p.resolve()
            if not self.sys_path:
                return "podaj gdzie zapisać"
            self.sys_path.parent.mkdir(parents=True, exist_ok=True)
            sysmod.save(self.sys, self.sys_path)
            return str(self.sys_path)

    def open_system(self, path):
        with self.lock:
            if path:
                p = Path(path).expanduser().resolve()
                if not p.is_file():
                    return f"nie ma pliku {p}"
                self.sys, self.sys_path = sysmod.load(p)
            else:
                self.sys, self.sys_path = sysmod.empty(), None
            self.builds = {}
            try:
                self.rebuild()
            except Exception:
                self.world.event("blad", traceback.format_exc()[-500:])
                return "błąd podczas otwierania: " + traceback.format_exc().strip().splitlines()[-1]
            return "ok"

    def set_board_config(self, bid, changes):
        """Zmiana ustawien kompilacji plytki z UI; plytka z pliku projektu przechodzi na konfiguracje w schemacie."""
        with self.lock:
            b = self._board_cfg(bid)
            if not b:
                return "brak płytki"
            if "config" not in b:
                b["config"] = self._project(b).to_config()
                b.pop("project", None)
            fam_old = b["config"].get("family")
            for k, v in changes.items():
                if k in ("sources", "include_dirs", "defines", "init", "quiet_funcs", "watch", "replace_functions",
                         "can_kernel_hz", "timer_clk_hz", "core_hz", "family", "device_header", "device_define",
                         "can_peripheral", "root", "cmsis_dir"):
                    b["config"][k] = v
            if b["config"].get("family") != fam_old:
                b["config"].update(FAMILIES[b["config"]["family"]])
                b["config"].pop("cmsis_dir", None)
            ok = self._build_one(b)
            if ok and self.world is not None:
                self._replace_board(b)
            return "ok" if ok else "błąd kompilacji (zakładka Build)"

    def add_watch(self, bid, name, typ):
        """Dodaje zmienna do podgladu bez przebudowy plytki."""
        with self.lock:
            b = self._board_cfg(bid)
            if "config" not in b:
                b["config"] = self._project(b).to_config()
                b.pop("project", None)
            w = b["config"].setdefault("watch", [])
            if not any(x["name"] == name for x in w):
                w.append({"name": name, "type": typ})
            prj = self.builds.get(bid, (None,))[0]
            if prj is not None and not any(x["name"] == name for x in prj.watch):
                prj.watch.append({"name": name, "type": typ})

    def remove_watch(self, bid, name):
        with self.lock:
            b = self._board_cfg(bid)
            for lst in ((b.get("config") or {}).get("watch", []), (self.builds.get(bid, (None,))[0] or Project(root=".")).watch):
                lst[:] = [x for x in lst if x["name"] != name]

    def symbols(self, bid):
        res = self.builds.get(bid, (None, {}))[1] or {}
        sy = res.get("symbols") or {"funcs": [], "vars": []}
        e = self.engines.get(bid)
        out = {"funcs": sy["funcs"], "vars": []}
        for v in sy["vars"]:
            v = dict(v)
            v["value"] = e.read_global(v["name"], v["key"]) if (e and v["key"] and not v["static"]) else None
            out["vars"].append(v)
        return out

    def fetch_headers(self):
        msgs = []
        ok = all(cmsis.fetch(f, msgs.append) for f in cmsis.REPOS)
        if ok:
            self.rebuild()
        return ("OK, nagłówki pobrane" if ok else "Nie udało się: " + "; ".join(msgs[-3:]))

    def examples(self):
        out = []
        for p in sorted(EXAMPLES.glob("*/cansim_system.json")) + sorted(EXAMPLES.glob("*/cansim_project.json")):
            try:
                out.append({"path": str(p), "name": __import__("json").loads(p.read_text(encoding="utf-8")).get("name", p.parent.name)})
            except (OSError, ValueError):
                pass
        return out

    @staticmethod
    def list_dir(path):
        p = Path(path).expanduser() if path else Path.home()
        if not p.is_dir():
            p = p.parent if p.parent.is_dir() else Path.home()
        p = p.resolve()
        dirs, files = [], []
        try:
            for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if c.name.startswith("."):
                    continue
                if c.is_dir():
                    dirs.append(c.name)
                elif c.suffix.lower() in (".json", ".c", ".h"):
                    files.append(c.name)
        except OSError:
            pass
        roots = [f"{d}:\\" for d in "CDEFGH" if Path(f"{d}:\\").exists()] if os.name == "nt" else ["/"]
        return {"path": str(p), "parent": str(p.parent), "dirs": dirs, "files": files, "roots": roots,
                "home": str(Path.home()), "examples": str(EXAMPLES) if EXAMPLES.is_dir() else ""}

    # ---------- watek symulacji ----------
    def _loop(self):
        chunk_ms = 5.0
        while True:
            t0 = time.time()
            if self.running and self.world is not None:
                with self.lock:
                    try:
                        self.world.run_until(self.world.now + int(chunk_ms * self.speed * 1_000_000))
                    except Exception:
                        self.running = False
                        self.world.event("blad", traceback.format_exc()[-400:])
            time.sleep(max(chunk_ms / 1000.0 - (time.time() - t0), 0.001))

    # ---------- akcje ----------
    def step(self, ms):
        with self.lock:
            if self.world:
                self.running = False
                self.world.step_ms(ms)

    def call(self, bid, fname, args):
        with self.lock:
            e = self.engines.get(bid)
            return e.call(fname, args) if e else (None, "płytka nie jest zbudowana")

    def inject(self, target, what, value=None):
        with self.lock:
            w = self.world
            if target == "bus":
                if what == "corrupt":
                    w.bus.corrupt_next += int(value or 1)
                    w.event("awaria", f"magistrala: {int(value or 1)} następnych ramek uszkodzonych")
                return "ok"
            if target in self.nodes:
                o = self.nodes[target]
                if hasattr(o, "inject"):
                    o.inject(what, value)
                    w.event("awaria", f"{o.name}: {what}={value}")
                    if what == "bitrate":
                        next(n for n in self.sys["nodes"] if n["id"] == target)["bitrate"] = int(value)
                        self._erc_t = -1
                    return "ok"
            e = self.engines.get(target)
            if e and e.can is not None:
                if what == "force_tec":
                    e.can.tec = int(value)
                    e.can.update_state()
                    e.event("awaria", f"{e.can.name}: TEC ustawione na {value}")
                    return "ok"
            return "nieznany cel"

    def send_frame(self, nid, fid, data_hex, ext=False):
        with self.lock:
            o = self.nodes.get(nid)
            if not isinstance(o, GenericNode):
                return "wybierz węzeł generyczny"
            o.send(Frame(int(str(fid), 0), bytes.fromhex(str(data_hex).replace(" ", "")), bool(ext)))
            return "ok"

    # ---------- pliki ----------
    def _root(self, bid):
        prj = self.builds.get(bid, (None,))[0]
        if prj is None:
            b = self._board_cfg(bid)
            if not b:
                return None
            return Path(b["config"]["root"]) if "config" in b else Path(b["project"]).parent
        return Path(prj.root)

    def files(self, bid):
        root = self._root(bid)
        if not root:
            return []
        return [str(p.relative_to(root)).replace("\\", "/") for p in sorted(root.rglob("*"))
                if p.suffix.lower() in (".c", ".h", ".json") and ".cansim_build" not in p.parts]

    def _safe(self, bid, rel):
        root = self._root(bid)
        if not root:
            return None
        p = (root / rel).resolve()
        return p if root.resolve() in p.parents else None

    def read_file(self, bid, rel):
        p = self._safe(bid, rel)
        return p.read_text(encoding="latin-1") if p and p.is_file() else None

    def write_file(self, bid, rel, text):
        p = self._safe(bid, rel)
        if not p:
            return False
        p.write_text(text, encoding="latin-1", newline="")
        return True

    # ---------- eksport ----------
    def export(self, fmt):
        with self.lock:
            log = list(self.world.bus.log) if self.world else []
        out = []
        for l in log:
            f = l.frame
            if fmt == "csv":
                out.append(f"{l.t_ns / 1e9:.6f};{l.src};{f.id:X};{int(f.ext)};{f.dlc};{f.data.hex(' ')};{l.status};{l.reason}")
            elif l.status == "ok":
                ident = f"{f.id:08X}" if f.ext else f"{f.id:03X}"
                out.append(f"({l.t_ns / 1e9:.6f}) can0 {ident}#{'R' if f.rtr else f.data.hex().upper()}")
        if fmt == "csv":
            out.insert(0, "t_s;nadawca;id_hex;ext;dlc;dane;status;powod")
        return "\n".join(out) + "\n"

    # ---------- stan dla UI ----------
    def _live(self):
        live = {}
        for b in self.sys["boards"]:
            e = self.engines.get(b["id"])
            if e and e.can is not None:
                on = e.can.online
                live[b["id"]] = {"name": e.name, "online": on, "bitrate": e.can.bitrate_hz() if on else 0,
                                 "timing": e.can.timing() if on else None}
            else:
                live[b["id"]] = {"name": b.get("name"), "online": False}
        for n in self.sys["nodes"]:
            o = self.nodes.get(n["id"])
            if o is not None:
                live[n["id"]] = {"name": o.name, "online": o.online, "bitrate": o.bitrate if o.online else 0}
        return live

    def _erc(self, live):
        w = self.world
        if self._erc_t >= 0 and w.now - self._erc_t < 200_000_000:
            return self.erc
        items = sysmod.run_erc(self.sys, live)
        for (fid, ext), who in w.bus.id_collisions().items():
            items.append(sysmod._item("error", f"To samo ID 0x{fid:X} nadaje kilka węzłów",
                                      ", ".join(sorted(who)) + ". W CAN każde ID może nadawać tylko jeden węzeł; przy jednoczesnym starcie z różnymi danymi powstaje błąd bitu.",
                                      "ISO 11898-1 (unikalność identyfikatorów)"))
        load = w.bus.load_percent(w.now)
        if load > 70:
            items.append(sysmod._item("warn", f"Obciążenie magistrali {load:.0f} %",
                                      "Powyżej ok. 70 % rosną opóźnienia ramek o niższym priorytecie (reguła praktyczna).", ""))
        for e in self.engines.values():
            if e.can is not None and e.can.online is False and e.can.bus_off:
                items.append(sysmod._item("error", f"{e.name}: BUS-OFF", "Kontroler odłączony od magistrali po przekroczeniu TEC 255.", "ISO 11898-1", [e.board_id]))
        self.erc, self._erc_t = items, w.now
        return items

    def _frames(self, n=300):
        out = []
        od = {x.get("node_id") for x in self.sys["nodes"] if x.get("type") == "odrive"}
        msgs = {int(k, 0): v for k, v in self.sys.get("messages", {}).items()}
        for l in self.world.bus.log[-n:]:
            f = l.frame
            if not f.ext and f.id in msgs:
                dec = decode_signals(f, msgs[f.id])
            elif not f.ext and (f.id >> 5) in od:
                dec = odrive_decode(f)
            else:
                dec = ""
            out.append({"seq": getattr(l, "seq", 0), "t": l.t_ns, "src": l.src, "id": f"{f.id:X}", "ext": f.ext,
                        "node": f.id >> 5, "cmd": f.id & 0x1F, "dlc": f.dlc, "data": f.data.hex(" "), "status": l.status,
                        "reason": l.reason, "acc": l.accepted, "ack": l.acked, "dec": dec, "dur": l.dur_ns})
        return out

    def _board_state(self, b, periph, allregs=False):
        prj, res = self.builds.get(b["id"], (None, {"ok": False, "log": "nie zbudowano"}))
        st = {"id": b["id"], "build_ok": res["ok"], "build_log": res.get("log", "")[-6000:], "family": prj.family if prj else "",
              "config": prj.to_config() if prj else b.get("config")}
        e = self.engines.get(b["id"])
        if not e:
            return st
        can = e.can
        if can is not None:
            st["can"] = can.stats()
            st["can"]["name"] = can.name
            st["can"]["bitrate"] = round(can.bitrate_hz())
            st["ram"] = can.ram_view()
            root = Path(prj.root)
            warns = []
            for t, m, loc in can.warnings[-30:]:
                func = ""
                if loc:
                    fn, _, ln = loc.rpartition(":")
                    try:
                        func = _func_at((root / fn).read_text(encoding="latin-1"), int(ln))
                    except (OSError, ValueError):
                        pass
                warns.append({"t": t, "msg": m, "loc": loc, "func": func})
            st["warnings"] = warns
        st["periph_names"] = [m.name for m in e.models if m.touched]
        pm = e.by_name.get(periph) if periph in e.by_name else can
        if pm is not None:
            mi = e.models.index(pm)
            last = self._regseen.setdefault((b["id"], pm.name), {})
            regs = []
            for nm, off, val in pm.snapshot():
                if not allregs and off not in pm.touched and (b["id"], pm.name, off) not in self._regshow:
                    continue                                  # pokazujemy rejestry, ktorych kod uzywa
                self._regshow.add((b["id"], pm.name, off))
                base = nm.split("[")[0] if not nm.startswith("s") else nm
                live_regs = getattr(pm, "live_regs", ())
                shown = pm.read(off) if (base in live_regs and base != "PSR") else pm.mem.get(off, 0)
                if base == "PSR" and hasattr(pm, "psr"):
                    shown = pm.psr()
                rs = e.regstat.get((mi, off), [0, 0, None, None, 0])
                prev_r, prev_w = last.get(off, (rs[0], rs[1]))
                seen = (rs[4] | pm.seen.pop(off, 0)) & ~shown & 0xFFFFFFFF   # bity ustawione na chwile od ostatniego odswiezenia
                rs[4] = 0
                last[off] = (rs[0], rs[1])
                fields = e.dev.decode(pm.name, base, shown) if shown else []
                blink = [f[0] for f in e.dev.decode(pm.name, base, seen) if f[1]] if seen else []
                regs.append({"name": nm, "off": f"{off:03X}", "val": f"{shown:08X}",
                             "fields": [[f[0], f[1], f[2], f[3]] for f in fields if f[1]],
                             "reads": rs[0], "writes": rs[1], "dr": rs[0] - prev_r, "dw": rs[1] - prev_w,
                             "last_w_t": rs[2], "last_w": None if rs[3] is None else f"{rs[3]:08X}", "blink": blink})
            st["regs"] = {"periph": pm.name, "list": regs, "total": len(pm.snapshot())}
        ev = list(e.events)[-300:]
        st["events"] = ev
        st["reglog"] = list(e.reglog)[-300:]
        keys = {v["name"]: v for v in (res.get("symbols") or {}).get("vars", [])}
        watch = []
        for w in prj.watch:
            m = re.match(r"^([A-Za-z_]\w*)(?:\[(\d+)\])?$", w["name"])
            nm, ix = (m.group(1), int(m.group(2) or 0)) if m else (w["name"], 0)
            sym = keys.get(nm, {})
            typ = w.get("type") or sym.get("key") or "u32"
            watch.append({"name": w["name"], "val": e.read_global(nm, typ, ix), "type": sym.get("type", typ),
                          "static": sym.get("static", False)})
        st["watch"] = watch
        st["isr"] = dict(e.isr_count)
        st["calls"] = e.calls.most_common(25)
        st["tasks"] = [{"name": t.name, "done": t.done, "error": t.error} for t in e.tasks]
        return st

    def state(self, bid=None, periph=None, allregs=False):
        with self.lock:
            w = self.world
            s = {"name": self.sys.get("name"), "running": self.running, "speed": self.speed, "t_ns": w.now,
                 "bus_load": round(w.bus.load_percent(w.now), 1), "system": self.sys,
                 "sys_path": str(self.sys_path) if self.sys_path else "", "headers": {f: cmsis.present(f) for f in cmsis.REPOS}}
            live = self._live()
            parts = []
            for b in self.sys["boards"]:
                e = self.engines.get(b["id"])
                p = {"id": b["id"], "kind": "board", "name": (e.name if e else b.get("name")), "build_ok": self.builds.get(b["id"], (0, {"ok": False}))[1]["ok"]}
                if e and e.can is not None:
                    p.update(e.can.stats())
                    p["can_name"] = e.can.name
                    p["online"] = e.can.online
                    p["bitrate"] = round(e.can.bitrate_hz()) if e.can.online else 0
                    p["warn_count"] = len(e.can.warnings)
                    p["mcu"] = self.builds[b["id"]][0].device_define.replace("xx", "")
                parts.append(p)
            for n in self.sys["nodes"]:
                o = self.nodes.get(n["id"])
                p = {"id": n["id"], "kind": n.get("type")}
                if o is not None:
                    p.update(o.state_dict())
                    p["passive"] = getattr(o, "passive", False)
                else:
                    p["name"] = "Terminator 120 Ω" if n.get("type") == "terminator" else (n.get("name") or f"ODrive {n.get('node_id', '')}")
                    p["online"] = False if n.get("type") != "terminator" else None
                    p["node_id"] = n.get("node_id")
                parts.append(p)
            s["parts"] = parts
            s["erc"] = self._erc(live)
            s["frames"] = self._frames()
            evs = list(w.events)[-200:]
            s["world_events"] = evs
            if not bid or not self._board_cfg(bid):
                bid = self.sys["boards"][0]["id"] if self.sys["boards"] else None
            s["board"] = self._board_state(self._board_cfg(bid), periph, allregs) if bid else None
            return s
