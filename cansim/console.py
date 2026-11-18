import re
import shlex

from .bus import Frame

ACTIONS_ODRIVE = {
    "predkosc": ("obr/s", "zadana predkosc (jakby przyszła komenda set_input_vel)"),
    "stan": ("idle|closed_loop", "stan osi: idle albo closed_loop"),
    "blad": ("kod", "wstrzyknij błąd osi (np. 1 albo 0x800)"),
    "kasuj_blad": ("", "skasuj błędy osi (jak clear_errors)"),
    "zasilanie": ("on|off", "włącz albo odłącz zasilanie ODrive"),
    "heartbeat": ("on|off", "włącz albo wycisz wysyłanie heartbeat"),
    "watchdog": ("ms", "watchdog osi: brak komend dłużej niż ms => błąd i IDLE (0 = wyłączony)"),
    "bitrate": ("kbit/s", "predkosc magistrali ODrive (np. 250, 500)"),
}
OPS = ("==", "!=", ">=", "<=", ">", "<")


def slug(name):
    return re.sub(r"[^0-9A-Za-zĄ-ż]+", "_", str(name)).strip("_")


class Console:
    def __init__(self, app):
        self.app = app
        self.cmds = {
            "pomoc": ([("komenda", "cmd")], "lista komend albo opis jednej komendy", self.c_help),
            "czekaj": ([("ms", "num")], "przesuń symulację o podaną liczbę milisekund (np. czekaj 100)", self.c_wait),
            "start": ([], "uruchom symulację w czasie rzeczywistym", self.c_start),
            "pauza": ([], "zatrzymaj symulację", self.c_pause),
            "reset": ([], "wszystkie płytki od nowa (t = 0)", self.c_reset),
            "stan": ([], "podsumowanie wszystkich węzłów", self.c_state),
            "ramki": ([("filtr: ID albo nadawca", "frame_filter"), ("ile", "num")], "ostatnie ramki na magistrali", self.c_frames),
            "wyslij": ([("wezel", "generic"), ("ID", "canid"), ("dane hex", "hex*")],
                       "wyślij ramkę z węzła generycznego, np. wyslij PC 0x2D 00 00 00 40 00 00 00 00", self.c_send),
            "odrive": ([("node_id", "odrive"), ("akcja", "odrive_action"), ("wartosc", "odrive_value")],
                       "sterowanie modelem ODrive, np. odrive 1 predkosc 2.5 / odrive * stan closed_loop", self.c_odrive),
            "wywolaj": ([("plytka", "board"), ("funkcja", "func"), ("argumenty", "fargs*")],
                        "wywołaj funkcję C w firmware płytki, np. wywolaj Sterownik_H743 can_send 0x2D 0 0 0", self.c_call),
            "ustaw": ([("plytka", "board"), ("zmienna", "var"), ("wartosc", "num")],
                      "zapisz zmienną globalną firmware (jak debugger), np. ustaw Sterownik_H743 battery_mv 24000", self.c_set),
            "pokaz": ([("plytka", "board"), ("zmienna", "var")], "odczytaj zmienną globalną firmware", self.c_get),
            "obserwuj": ([("plytka", "board"), ("zmienna", "var")], "dodaj zmienną do zakładki Zmienne", self.c_watch),
            "awaria": ([("rodzaj", "fault"), ("cel / ile", "fault_arg"), ("wartosc", "num")],
                       "awaria uszkodz 5 (5 zepsutych ramek) / awaria tec Sterownik_H743 260 (wymuś TEC)", self.c_fault),
            "oczekuj": ([("plytka", "board"), ("zmienna", "var"), ("operator", "op"), ("wartosc", "num")],
                        "sprawdzenie w scenariuszu, np. oczekuj Sterownik_H743 rx_frames > 10 => OK albo BŁĄD", self.c_expect),
            "napisz": ([("tekst", "text*")], "wypisz tekst (komentarz w wynikach scenariusza)", self.c_echo),
        }
        self.alias = {"help": "pomoc", "wait": "czekaj", "send": "wyslij", "call": "wywolaj", "set": "ustaw", "get": "pokaz",
                      "watch": "obserwuj", "expect": "oczekuj", "echo": "napisz", "frames": "ramki", "status": "stan",
                      "pause": "pauza", "fault": "awaria"}

    # ------------------------------------------------------------ odniesienia
    def _boards(self):
        return [(slug(b.get("name") or b["id"]), b) for b in self.app.sys["boards"]]

    def board(self, tok):
        for s, b in self._boards():
            if tok.lower() in (s.lower(), b["id"].lower()):
                return b
        raise ValueError(f"nie ma płytki '{tok}'. Dostępne: {', '.join(s for s, _ in self._boards()) or 'brak'}")

    def _generics(self):
        return [(slug(n.get("name") or n["id"]), n) for n in self.app.sys["nodes"] if n.get("type") == "generic"]

    def generic(self, tok):
        for s, n in self._generics():
            if tok.lower() in (s.lower(), n["id"].lower()):
                return self.app.nodes.get(n["id"])
        raise ValueError(f"nie ma węzła '{tok}'. Dodaj 'węzeł' (np. PC z USB-CAN) albo użyj: {', '.join(s for s, _ in self._generics()) or 'brak'}")

    def odrives(self, tok):
        objs = [(n, self.app.nodes.get(n["id"])) for n in self.app.sys["nodes"] if n.get("type") == "odrive"]
        if tok == "*":
            return [o for _, o in objs if o]
        try:
            nid = int(tok, 0)
        except ValueError:
            raise ValueError("podaj numer node_id ODrive (np. 1) albo *")
        hit = [o for n, o in objs if o is not None and o.node_id == nid]
        if not hit:
            raise ValueError(f"nie ma ODrive o node_id {nid}. Są: {', '.join(str(o.node_id) for _, o in objs if o) or 'brak'}")
        return hit

    def symbols(self, b):
        prj, res = self.app.builds.get(b["id"], (None, None))
        return (res or {}).get("symbols") or {"funcs": [], "vars": []}

    def var(self, b, tok):
        m = re.match(r"^([A-Za-z_]\w*)(?:\[(\w+)\])?$", tok)
        if not m:
            raise ValueError(f"zła nazwa zmiennej '{tok}'")
        name, idx = m.group(1), int(m.group(2), 0) if m.group(2) else 0
        sym = next((v for v in self.symbols(b)["vars"] if v["name"] == name), None)
        if sym is None:
            raise ValueError(f"nie znam zmiennej {name} w płytce {b.get('name')}")
        if sym["static"]:
            raise ValueError(f"{name} jest static: nie jest widoczna z zewnątrz skompilowanego kodu (usuń 'static', żeby ją podglądać)")
        if not sym["key"]:
            raise ValueError(f"{name} ma typ {sym['type']}: obsługiwane są tylko liczby (uint8..64, int, float, double)")
        return name, idx, sym

    def engine(self, b):
        e = self.app.engines.get(b["id"])
        if not e:
            raise ValueError(f"płytka {b.get('name')} nie działa (błąd kompilacji? zakładka Build)")
        return e

    # ------------------------------------------------------------ wykonanie
    def run(self, line):
        line = line.strip()
        if not line or line.startswith("#"):
            return True, ""
        try:
            toks = shlex.split(line, posix=True)
        except ValueError as e:
            return False, f"błąd składni: {e}"
        name = self.alias.get(toks[0].lower(), toks[0].lower())
        if name not in self.cmds:
            sugg = [c for c in self.cmds if c.startswith(name[:2])]
            return False, f"nieznana komenda '{toks[0]}'." + (f" Może: {', '.join(sugg)}?" if sugg else " Wpisz: pomoc")
        try:
            with self.app.lock:
                out = self.cmds[name][2](toks[1:])
            return True, out or "ok"
        except ValueError as e:
            return False, str(e)
        except Exception as e:                       # noqa
            return False, f"błąd: {e!r}"

    def script(self, text, max_ms=60000):
        results, ok_all, waited = [], True, 0
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.split()[0].lower() in ("czekaj", "wait"):
                try:
                    waited += float(s.split()[1])
                except (IndexError, ValueError):
                    pass
                if waited > max_ms:
                    results.append({"line": i, "cmd": s, "ok": False, "out": f"przekroczono limit {max_ms} ms czasu symulacji w scenariuszu"})
                    ok_all = False
                    break
            ok, out = self.run(s)
            results.append({"line": i, "cmd": s, "ok": ok, "out": out})
            ok_all &= ok
        return {"ok": ok_all, "results": results}

    # ------------------------------------------------------------ komendy
    def c_help(self, a):
        if a:
            n = self.alias.get(a[0], a[0])
            if n not in self.cmds:
                raise ValueError(f"nie ma komendy {a[0]}")
            args, desc, _ = self.cmds[n]
            return f"{n} {' '.join('<' + x[0] + '>' for x in args)}\n  {desc}"
        return "\n".join(f"{n:<10} {d}" for n, (_, d, _) in self.cmds.items()) + \
            "\nTab: podpowiedź, strzałki góra/dol: historia. Płytki po nazwie bez spacji, ODrive po numerze."

    def c_wait(self, a):
        ms = float(a[0]) if a else 100
        if not 0 < ms <= 60000:
            raise ValueError("czekaj: od 0 do 60000 ms")
        self.app.step(ms)
        return f"t = {self.app.world.now / 1e6:.3f} ms"

    def c_start(self, a):
        self.app.running = True
        return "symulacja działa"

    def c_pause(self, a):
        self.app.running = False
        return f"pauza, t = {self.app.world.now / 1e6:.3f} ms"

    def c_reset(self, a):
        self.app.reset()
        return "reset: t = 0"

    def c_state(self, a):
        st = self.app.state()
        lines = [f"t = {st['t_ns'] / 1e6:.3f} ms, obciążenie magistrali {st['bus_load']} %"]
        for p in st["parts"]:
            if p["kind"] == "board":
                lines.append(f"  {p['name']:<22} {('BUS-OFF' if p.get('bus_off') else 'INIT' if p.get('init') else 'Error Passive' if p.get('passive') else 'aktywna'):<14} TEC {p.get('tec', '-')} REC {p.get('rec', '-')} wysłane {p.get('tx_sent', 0)} odebrane {p.get('rx_stored', 0)}")
            elif p["kind"] == "odrive":
                lines.append(f"  {p['name']:<22} {p.get('state_name', ''):<14} v={p.get('vel')} obr/s zadana={p.get('input_vel')} błąd=0x{p.get('error', 0):X}{'' if p.get('online') else ' (wyłączony)'}")
            elif p["kind"] == "generic":
                lines.append(f"  {p['name']:<22} węzeł          wysłane {p.get('sent')} odebrane {p.get('received')}")
        bad = [i for i in st["erc"] if i["level"] == "error"]
        n = len(bad)
        word = "błąd" if n == 1 else "błędy" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "błędów"
        lines.append(f"ERC: {n} {word}" + (": " + "; ".join(i["title"] for i in bad) if bad else ""))
        return "\n".join(lines)

    def c_frames(self, a):
        flt = a[0].lower() if a else ""
        n = int(a[1]) if len(a) > 1 else 10
        fr = [f for f in self.app._frames(500)
              if not flt or f["id"].lower() == flt.replace("0x", "") or flt in f["src"].lower()][-n:]
        if not fr:
            return "brak ramek" + (f" pasujących do '{a[0]}'" if flt else "")
        return "\n".join(f"{f['t'] / 1e6:10.3f} ms  {f['src']:<18} 0x{f['id']:<4} [{f['dlc']}] {f['data']:<24} {f['status']}{'  ' + f['dec'] if f['dec'] else ''}" for f in fr)

    def c_send(self, a):
        if len(a) < 2:
            raise ValueError("użycie: wyslij <wezel> <ID> [dane hex]")
        node = self.generic(a[0])
        fid = int(a[1], 16) if not a[1].lower().startswith("0x") and re.fullmatch(r"[0-9A-Fa-f]+", a[1]) and len(a[1]) == 3 else int(a[1], 0)
        data = bytes.fromhex("".join(a[2:]).replace("0x", ""))
        if len(data) > 8:
            raise ValueError("klasyczny CAN: maksymalnie 8 bajtów danych")
        if fid > 0x7FF:
            node.send(Frame(fid, data, True))
        else:
            node.send(Frame(fid, data))
        return f"{node.name}: ramka 0x{fid:X} [{len(data)}] {data.hex(' ')} w kolejce do wysłania (zobaczysz ja po 'czekaj')"

    def c_odrive(self, a):
        if len(a) < 2:
            raise ValueError("użycie: odrive <node_id|*> <akcja> [wartość]. Akcje: " + ", ".join(ACTIONS_ODRIVE))
        ods = self.odrives(a[0])
        act = a[1].lower()
        val = a[2] if len(a) > 2 else None
        if act not in ACTIONS_ODRIVE:
            raise ValueError(f"nieznana akcja {act}. Akcje: {', '.join(ACTIONS_ODRIVE)}")
        if act not in ("kasuj_blad",) and val is None:
            raise ValueError(f"podaj wartość: {ACTIONS_ODRIVE[act][0]}")
        onoff = lambda v: {"on": True, "1": True, "tak": True, "off": False, "0": False, "nie": False}[v.lower()]
        for o in ods:
            if act == "predkosc":
                o.input_vel = float(val)
            elif act == "stan":
                st = {"idle": 1, "closed_loop": 8, "1": 1, "8": 8}.get(val.lower())
                if st is None:
                    raise ValueError("stan: idle albo closed_loop")
                if st == 8 and o.error:
                    raise ValueError(f"{o.name}: nie wejdzie w CLOSED_LOOP, ma błąd 0x{o.error:X} (najpierw: odrive {o.node_id} kasuj_blad)")
                o.state = st
            elif act == "blad":
                o.inject("error", int(val, 0))
            elif act == "kasuj_blad":
                o.error = 0
            elif act == "zasilanie":
                o.inject("power", onoff(val))
            elif act == "heartbeat":
                o.inject("mute_hb", not onoff(val))
            elif act == "watchdog":
                o.inject("watchdog_ms", int(val))
            elif act == "bitrate":
                br = int(float(val) * 1000) if float(val) < 5000 else int(val)
                self.app.inject(next(n["id"] for n in self.app.sys["nodes"] if self.app.nodes.get(n["id"]) is o), "bitrate", br)
        self.app.world.event("konsola", f"odrive {a[0]} {act} {val or ''}".strip())
        out = ", ".join(o.name for o in ods) + f": {act} {val or ''}".rstrip()
        if act in ("predkosc", "stan"):
            cmd = 0x0D if act == "predkosc" else 0x07
            now = self.app.world.now
            busy = sorted({l.src for l in self.app.world.bus.log[-400:] if l.status == "ok" and not l.frame.ext
                           and (l.frame.id & 0x1F) == cmd and any((l.frame.id >> 5) == o.node_id for o in ods)
                           and now - l.t_ns < 500_000_000})
            if busy:
                out += (f"\nuwaga: {', '.join(busy)} wysyła(ją) tę komendę cyklicznie i nadpisze tę wartość przy następnej ramce "
                        f"(tak samo byłoby na sprzęcie). Żeby sterować ręcznie, zmień zmienną w kodzie płytki albo zatrzymaj jej wysyłanie.")
        return out

    def c_call(self, a):
        if len(a) < 2:
            raise ValueError("użycie: wywolaj <plytka> <funkcja> [argumenty]")
        b = self.board(a[0])
        e = self.engine(b)
        prj = self.app.builds.get(b["id"], (None,))[0]
        if prj and a[1] in prj.init:
            raise ValueError(f"{a[1]} już działa (to funkcja startowa płytki); drugie wywołanie uruchomiłoby drugą kopię programu")
        sig = next((f for f in self.symbols(b)["funcs"] if f["name"] == a[1]), None)
        if sig and sig["static"]:
            raise ValueError(f"{a[1]} jest static: nie da się jej wywołać z zewnątrz (usuń 'static' w kodzie, żeby testować ją ręcznie)")
        r, err = e.call(a[1], a[2:], sig=sig)
        if err:
            raise ValueError(err)
        return f"{a[1]}({', '.join(a[2:])}) -> {r if r is not None else 'void'}"

    def c_set(self, a):
        if len(a) < 3:
            raise ValueError("użycie: ustaw <plytka> <zmienna> <wartosc>")
        b = self.board(a[0])
        name, idx, sym = self.var(b, a[1])
        err = self.engine(b).write_global(name, sym["key"], a[2], idx)
        if err:
            raise ValueError(err)
        return f"{a[1]} = {self.engine(b).read_global(name, sym['key'], idx)}"

    def c_get(self, a):
        if len(a) < 2:
            raise ValueError("użycie: pokaz <plytka> <zmienna>")
        b = self.board(a[0])
        name, idx, sym = self.var(b, a[1])
        v = self.engine(b).read_global(name, sym["key"], idx)
        return f"{a[1]} = {v}" + (f" (0x{v & 0xFFFFFFFFFFFFFFFF:X})" if isinstance(v, int) else "") + f"  [{sym['type']}]"

    def c_watch(self, a):
        if len(a) < 2:
            raise ValueError("użycie: obserwuj <plytka> <zmienna>")
        b = self.board(a[0])
        name, idx, sym = self.var(b, a[1])
        self.app.add_watch(b["id"], a[1], sym["key"])
        return f"{a[1]} dodana do zakładki Zmienne płytki {b.get('name')}"

    def c_fault(self, a):
        if not a:
            raise ValueError("użycie: awaria uszkodz <ile> | awaria tec <plytka> <wartosc>")
        if a[0] == "uszkodz":
            n = int(a[1]) if len(a) > 1 else 1
            self.app.inject("bus", "corrupt", n)
            return f"{n} następnych ramek zostanie uszkodzonych (błąd CRC u odbiorców, ponowienie przez nadawcę)"
        if a[0] == "tec":
            if len(a) < 3:
                raise ValueError("użycie: awaria tec <plytka> <wartosc>")
            b = self.board(a[1])
            self.app.inject(b["id"], "force_tec", int(a[2]))
            return f"{b.get('name')}: TEC = {a[2]} ({'Bus-Off' if int(a[2]) > 255 else 'Error Passive' if int(a[2]) >= 128 else 'Error Warning' if int(a[2]) >= 96 else 'Error Active'})"
        raise ValueError("rodzaje awarii: uszkodz, tec")

    def c_expect(self, a):
        if len(a) < 4:
            raise ValueError("użycie: oczekuj <plytka> <zmienna> <operator> <wartosc>")
        b = self.board(a[0])
        name, idx, sym = self.var(b, a[1])
        v = self.engine(b).read_global(name, sym["key"], idx)
        op, want = a[2], float(a[3], ) if sym["key"] in ("f32", "f64") else int(a[3], 0)
        if op not in OPS:
            raise ValueError(f"operator: {' '.join(OPS)}")
        ok = {"==": v == want, "!=": v != want, ">": v > want, "<": v < want, ">=": v >= want, "<=": v <= want}[op]
        if not ok:
            raise ValueError(f"BŁĄD: {a[1]} = {v}, oczekiwano {op} {a[3]}")
        return f"OK: {a[1]} = {v} {op} {a[3]}"

    def c_echo(self, a):
        return " ".join(a)

    # ------------------------------------------------------------ podpowiedzi
    def complete(self, line):
        toks = line.split()
        trailing = line.endswith(" ") or not line
        idx = len(toks) if trailing else len(toks) - 1
        prefix = "" if trailing else toks[-1]
        if idx == 0:
            items = [{"value": n, "hint": d} for n, (_, d, _) in self.cmds.items() if n.startswith(prefix.lower())]
            return {"items": items, "usage": "wpisz komendę (Tab uzupełnia)"}
        name = self.alias.get(toks[0].lower(), toks[0].lower())
        if name not in self.cmds:
            return {"items": [], "usage": "nieznana komenda, wpisz: pomoc"}
        args, desc, _ = self.cmds[name]
        usage = f"{name} " + " ".join(("[" if a[1].endswith("*") or a[1] in ("frame_filter", "cmd") else "<") + a[0] +
                                      ("]" if a[1].endswith("*") or a[1] in ("frame_filter", "cmd") else ">") for a in args)
        k = idx - 1
        if not args:
            return {"items": [], "usage": usage, "help": desc}
        spec = args[min(k, len(args) - 1)] if (k < len(args) or args[-1][1].endswith("*")) else None
        items, extra = [], ""
        try:
            items, extra = self._cands(name, spec, toks, k, prefix)
        except ValueError as e:
            extra = str(e)
        items = [i for i in items if i["value"].lower().startswith(prefix.lower())][:60]
        return {"items": items, "usage": usage, "help": desc, "arg": spec[0] if spec else "", "note": extra}

    def _cands(self, name, spec, toks, k, prefix):
        if spec is None:
            return [], "za dużo argumentów"
        kind = spec[1].rstrip("*")
        if kind == "cmd":
            return [{"value": n, "hint": d} for n, (_, d, _) in self.cmds.items()], ""
        if kind == "board":
            return [{"value": s, "hint": f"{b['id']}, płytka"} for s, b in self._boards()], ""
        if kind == "generic":
            return [{"value": s, "hint": "węzeł generyczny"} for s, n in self._generics()], ""
        if kind == "odrive":
            return [{"value": "*", "hint": "wszystkie ODrive"}] + [{"value": str(n.get("node_id")), "hint": n.get("name") or f"ODrive {n.get('node_id')}"} for n in self.app.sys["nodes"] if n.get("type") == "odrive"], ""
        if kind == "odrive_action":
            return [{"value": a, "hint": f"{v[0]}: {v[1]}" if v[0] else v[1]} for a, v in ACTIONS_ODRIVE.items()], ""
        if kind == "odrive_value":
            act = toks[2].lower() if len(toks) > 2 else ""
            choices = {"stan": ["idle", "closed_loop"], "zasilanie": ["on", "off"], "heartbeat": ["on", "off"],
                       "bitrate": ["125", "250", "500", "1000"]}.get(act)
            if choices:
                return [{"value": c, "hint": ""} for c in choices], ""
            return [], ACTIONS_ODRIVE.get(act, ("", ""))[1]
        if kind in ("func", "var", "fargs"):
            b = self.board(toks[1]) if len(toks) > 1 else None
            if not b:
                return [], "najpierw płytka"
            sy = self.symbols(b)
            if kind == "func":
                return [{"value": f["name"], "hint": f"{f['ret']} {f['name']}({', '.join(p['type'] + ' ' + p['name'] for p in f['params']) or 'void'})" + ("  [static: niedostępna]" if f["static"] else "")}
                        for f in sy["funcs"] if not f["static"]], ""
            if kind == "var":
                e = self.app.engines.get(b["id"])
                out = []
                for v in sy["vars"]:
                    if v["static"] or not v["key"]:
                        continue
                    cur = e.read_global(v["name"], v["key"]) if e else None
                    out.append({"value": v["name"], "hint": f"{v['type']}{'[' + v['array'] + ']' if v['array'] else ''} = {cur}"})
                return out, ""
            f = next((f for f in sy["funcs"] if len(toks) > 2 and f["name"] == toks[2]), None)
            if f:
                ai = k - 2
                if ai < len(f["params"]):
                    p = f["params"][ai]
                    return [], f"argument {ai + 1}/{len(f['params'])}: {p['type']} {p['name']}"
                return [], "wszystkie argumenty podane, Enter wywołuje"
            return [], "argumenty liczbowe"
        if kind == "op":
            return [{"value": o, "hint": ""} for o in OPS], ""
        if kind == "fault":
            return [{"value": "uszkodz", "hint": "zepsuj N następnych ramek"}, {"value": "tec", "hint": "wymuś licznik TEC płytki"}], ""
        if kind == "fault_arg":
            if len(toks) > 1 and toks[1] == "tec":
                return [{"value": s, "hint": "plytka"} for s, b in self._boards()], ""
            return [], "ile ramek"
        if kind == "frame_filter":
            ids = sorted({f["id"] for f in self.app._frames(300)})
            return [{"value": "0x" + i, "hint": "ID"} for i in ids], "ID ramki albo nazwa nadawcy"
        if kind == "canid":
            return [], "ID hex, np. 0x2D (ODrive 1, set_input_vel = (1<<5)|0x0D)"
        if kind == "hex":
            return [], "bajty danych hex, np. 00 00 00 40 (do 8)"
        if kind == "num":
            if name == "czekaj":
                return [{"value": v, "hint": "ms"} for v in ("10", "100", "500", "1000")], "ile ms symulacji"
            return [], spec[0]
        return [], spec[0]
