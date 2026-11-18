import json
import math
import os
from pathlib import Path

# zakres zasilania strony magistrali i orientacyjne opoznienie petli TXD->RXD (najgorszy przypadek, ns)
TRANSCEIVERS = {
    "TJA1042": {"vcc": (4.5, 5.5), "loop_ns": 255, "note": "5 V; wersja TJA1042T/3 ma VIO dla logiki 3,3 V"},
    "TJA1051": {"vcc": (4.5, 5.5), "loop_ns": 250, "note": "5 V; wersja /3 ma VIO dla logiki 3,3 V"},
    "MCP2562": {"vcc": (4.5, 5.5), "loop_ns": 250, "note": "5 V; VIO 1,8-5,5 V dla logiki"},
    "SN65HVD230": {"vcc": (3.0, 3.6), "loop_ns": 120, "note": "3,3 V; współpracuje z transceiverami 5 V"},
    "wbudowany": {"vcc": None, "loop_ns": 250, "note": "transceiver na płytce urządzenia (np. ODrive)"},
}
DEFAULT_PHYS = {"pos_m": 0.0, "termination": False, "transceiver": "TJA1042", "vcc": 5.0, "stub_m": 0.1, "gnd": True,
                "osc_ppm": 50}


def empty():
    return {"name": "Nowy schemat", "bus": {"length_m": 2.0, "ns_per_m": 5.0}, "boards": [], "nodes": []}


def load(path):
    """Zwraca (system_dict, sciezka_systemu_do_zapisu). Projekt pojedynczej plytki zamienia na system."""
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    if "boards" in data:
        sysd = data
        save_path = path
    else:
        nodes = data.get("nodes", [])
        n = len(nodes)
        length = max(1.0, 0.5 * (n + 1))
        sysd = {"name": data.get("name", "system"), "bus": {"length_m": length, "ns_per_m": 5.0},
                "boards": [{"id": "b1", "name": data.get("name", "plytka"), "project": str(path), "pos_m": 0.0,
                            "termination": True}],
                "nodes": []}
        for i, nd in enumerate(nodes):
            nd = dict(nd)
            nd.setdefault("pos_m", round(length * (i + 1) / max(n, 1), 2))
            nd.setdefault("termination", i == n - 1)
            nd.setdefault("transceiver", "wbudowany" if nd.get("type") == "odrive" else "TJA1042")
            sysd["nodes"].append(nd)
        save_path = path.parent / "cansim_system.json"
    normalize(sysd, path.parent)
    return sysd, save_path


def normalize(sysd, base):
    sysd.setdefault("bus", {})
    sysd["bus"].setdefault("length_m", 2.0)
    sysd["bus"].setdefault("ns_per_m", 5.0)
    used = set()
    for kind, lst in (("b", sysd.setdefault("boards", [])), ("n", sysd.setdefault("nodes", []))):
        for i, el in enumerate(lst):
            if "id" not in el or el["id"] in used:
                k = i + 1
                while f"{kind}{k}" in used:
                    k += 1
                el["id"] = f"{kind}{k}"
            used.add(el["id"])
            for k, v in DEFAULT_PHYS.items():
                el.setdefault(k, v)
            if kind == "b" and "config" in el:
                r = Path(el["config"].get("root", "."))
                if not r.is_absolute():
                    el["config"]["root"] = str((Path(base) / r).resolve())
            elif kind == "b":
                p = Path(el["project"])
                if not p.is_absolute():
                    el["project"] = str((Path(base) / p).resolve())
    return sysd


def save(sysd, path):
    out = json.loads(json.dumps(sysd))
    base = Path(path).parent
    for b in out["boards"]:
        key, d = ("root", b["config"]) if "config" in b else ("project", b)
        try:
            d[key] = os.path.relpath(d[key], base).replace("\\", "/")
        except ValueError:
            pass                                   # inny dysk (Windows): zostaje sciezka bezwzgledna
    Path(path).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")


def new_id(sysd, prefix):
    used = {e["id"] for e in sysd["boards"] + sysd["nodes"]}
    k = 1
    while f"{prefix}{k}" in used:
        k += 1
    return f"{prefix}{k}"


# ---------------------------------------------------------------- ERC
def _item(level, title, detail, ref="", ids=()):
    return {"level": level, "title": title, "detail": detail, "ref": ref, "ids": list(ids)}


def bit_info(t):
    """Z parametrow bitu (brp, tseg1, tseg2, sjw, clk) liczy bitrate, tq, punkt probkowania."""
    n = 1 + t["tseg1"] + t["tseg2"]
    tq = t["brp"] / t["clk"] if t["clk"] else 0
    return {"n_tq": n, "tq_ns": tq * 1e9, "bitrate": 1 / (n * tq) if tq else 0, "sp": (1 + t["tseg1"]) / n, **t}


def run_erc(sysd, live):
    """live: {id: {"bitrate": .., "timing": {...} albo None, "online": bool, "name": ..}} ze stanu symulacji;
    zwraca liste pozycji ERC."""
    out = []
    els = [("board", b) for b in sysd["boards"]] + [("node", n) for n in sysd["nodes"]]
    L = float(sysd["bus"]["length_m"])
    nspm = float(sysd["bus"]["ns_per_m"])
    parts = [(k, e) for k, e in els if e.get("type") != "terminator"]
    if not parts:
        return []
    name = lambda e: live.get(e["id"], {}).get("name") or e.get("name") or e.get("type", e["id"])

    # 1. terminacja
    terms = [e for k, e in els if e.get("termination") or e.get("type") == "terminator"]
    nt = len(terms)
    req = 120.0 / nt if nt else math.inf
    if nt == 2:
        out.append(_item("ok", "Terminacja: 2 x 120 Ω",
                         f"Rezystancja między CANH a CANL przy wyłączonym zasilaniu: {req:.0f} Ω (zmierz multimetrem, oczekiwane ok. 60 Ω).",
                         "ISO 11898-2: terminacja 120 Ω na obu końcach linii", [e["id"] for e in terms]))
    elif nt == 0:
        out.append(_item("error", "Brak terminacji",
                         "Bez rezystorów 120 Ω linia nie wraca szybko do stanu recesywnego i odbija sygnał. W symulacji każda ramka kończy się błędem (założenie modelu; na realnym kablu zachowanie zależy od długości i prędkości).",
                         "ISO 11898-2"))
    else:
        out.append(_item("warn" if nt == 1 or nt == 3 else "error", f"Terminacja: {nt} x 120 Ω",
                         f"Wypadkowo {req:.0f} Ω zamiast 60 Ω. "
                         + ("Jeden terminator: działa przy niskich prędkościach i krótkim kablu, ale odbicia na drugim końcu." if nt == 1
                            else "Za małe obciążenie: niższe napięcie różnicowe i większy prąd nadajników; transceivery są specyfikowane dla obciążenia ok. 45-65 Ω."),
                         "ISO 11898-2", [e["id"] for e in terms]))
    if parts and terms:
        ends = (min(e["pos_m"] for _, e in els), max(e["pos_m"] for _, e in els))
        for e in terms:
            if min(abs(e["pos_m"] - ends[0]), abs(e["pos_m"] - ends[1])) > 0.1 * max(L, 0.1) + 0.05:
                out.append(_item("warn", f"Terminator w środku linii: {name(e)}",
                                 f"Pozycja {e['pos_m']} m, końce linii: {ends[0]} m i {ends[1]} m. Terminatory powinny być na dwóch najdalszych końcach.",
                                 "ISO 11898-2", [e["id"]]))

    # 2. bitrate
    rates, rate_ids = {}, {}
    for k, e in parts:
        lv = live.get(e["id"], {})
        if lv.get("bitrate"):
            rates.setdefault(round(lv["bitrate"]), []).append(name(e))
            rate_ids.setdefault(round(lv["bitrate"]), []).append(e["id"])
    if len(rates) > 1:
        major = max(rates, key=lambda r: len(rates[r]))
        out.append(_item("error", "Różne prędkości na jednej magistrali",
                         "; ".join(f"{r / 1000:g} kbit/s: {', '.join(v)}" for r, v in rates.items())
                         + ". Węzeł z innym bitrate widzi błędy i (w Error Active) niszczy ramki flaga błędu; "
                           "jego licznik REC szybko rośnie, a ramki które nadaje nie dostają ACK.",
                         "ISO 11898-1", [i for r, v in rate_ids.items() if r != major for i in v]))
    elif rates:
        out.append(_item("ok", f"Wspólny bitrate {list(rates)[0] / 1000:g} kbit/s", ", ".join(list(rates.values())[0])))
    bitrate = max(rates, key=lambda r: len(rates[r])) if rates else 0

    # 3. dlugosc kabla / propagacja, punkt probkowania, tolerancja oscylatora
    loops = [TRANSCEIVERS.get(e.get("transceiver"), TRANSCEIVERS["TJA1042"])["loop_ns"] for _, e in parts] or [250]
    t_prop = 2 * (L * nspm + max(loops))     # najgorszy przypadek: sygnal tam i z powrotem + petla transceivera
    sps = []
    for k, e in parts:
        t = live.get(e["id"], {}).get("timing")
        if not t:
            continue
        bi = bit_info(t)
        sps.append((name(e), bi["sp"]))
        tq = bi["tq_ns"]
        prop_max = (bi["tseg1"] - bi["sjw"]) * tq        # PS1 >= SJW, reszta TSEG1 moze pokryc propagacje
        lvl = "ok" if t_prop <= prop_max else "error"
        out.append(_item(lvl, f"Opóźnienie propagacji: {name(e)}",
                         f"Wymagane ok. {t_prop:.0f} ns (2 x ({L} m x {nspm} ns/m + {max(loops)} ns pętli transceivera)); "
                         f"dostępne przed punktem próbkowania (TSEG1 - SJW) = {prop_max:.0f} ns.",
                         "Bosch CAN 2.0 / ISO 11898-1: segment propagacji musi pokryć podwójne opóźnienie linii", [e["id"]]))
        prop_tq = math.ceil(t_prop / tq) if tq else 0
        ps1 = max(bi["tseg1"] - prop_tq, 1)
        ps2 = bi["tseg2"]
        n = bi["n_tq"]
        df = min(min(ps1, ps2) / (2 * (13 * n - ps2)), bi["sjw"] / (20 * n))
        worst = max(x.get("osc_ppm", 50) for _, x in parts)
        out.append(_item("ok" if df * 1e6 >= worst else "warn", f"Tolerancja zegara: {name(e)}",
                         f"Dopuszczalna odchyłka oscylatora df = {df * 100:.3f} % ({df * 1e6:.0f} ppm); najgorszy oscylator w systemie: {worst} ppm."
                         + (" Kwarc (ok. 50 ppm) wystarcza; wewnętrzny RC (HSI, ok. 1 %) może nie wystarczyć." if df < 0.01 else ""),
                         "ISO 11898-1: df <= min(PS1,PS2)/(2(13*NBT-PS2)) i df <= SJW/(20*NBT)", [e["id"]]))
        if not 0.75 <= bi["sp"] <= 0.90:
            out.append(_item("warn", f"Punkt próbkowania {bi['sp'] * 100:.1f} %: {name(e)}",
                             "Zalecany zakres 75-90 %, typowo 87,5 % (CiA 301).", "CiA 301", [e["id"]]))
        if bi["sjw"] > bi["tseg2"]:
            out.append(_item("warn", f"SJW > TSEG2: {name(e)}", f"SJW={bi['sjw']} tq, TSEG2={bi['tseg2']} tq.", "ISO 11898-1", [e["id"]]))
    if len(sps) > 1 and max(s for _, s in sps) - min(s for _, s in sps) > 0.05:
        out.append(_item("warn", "Różne punkty próbkowania",
                         ", ".join(f"{n}: {s * 100:.1f} %" for n, s in sps) + ". Przy wyższych prędkościach lub długim kablu warto je wyrównać.",
                         "CiA 301"))
    if bitrate:
        lmax = {1000000: 25, 500000: 100, 250000: 250, 125000: 500}.get(bitrate)
        if lmax and L > lmax:
            out.append(_item("warn", f"Kabel {L} m przy {bitrate / 1000:g} kbit/s",
                             f"Orientacyjny limit to ok. {lmax} m.", "reguła praktyczna CiA"))
        stub_max = 0.3 * 1e6 / bitrate
        for k, e in parts:
            if e.get("stub_m", 0) > stub_max:
                out.append(_item("warn", f"Długie odgałęzienie: {name(e)}",
                                 f"{e['stub_m']} m; przy {bitrate / 1000:g} kbit/s zalecane poniżej ok. {stub_max:.1f} m (0,3 m przy 1 Mbit/s).",
                                 "ISO 11898-2 (odgałęzienia), skalowanie z prędkością: reguła praktyczna", [e["id"]]))

    # 4. masa i zasilanie transceiverow
    for k, e in parts:
        if not e.get("gnd", True):
            out.append(_item("error", f"Brak wspólnej masy: {name(e)}",
                             "CAN jest różnicowy, ale odbiornik wymaga, by napięcie wspólne mieściło się w zakresie (ISO 11898-2: -2..+7 V względem jego masy). Bez przewodu GND różnica potencjałów mas może go przekroczyć. Poprowadź GND razem z CANH/CANL (skrętka + masa).",
                             "ISO 11898-2", [e["id"]]))
        tr = TRANSCEIVERS.get(e.get("transceiver"))
        if tr and tr["vcc"] and not (tr["vcc"][0] <= float(e.get("vcc", 5)) <= tr["vcc"][1]):
            out.append(_item("error", f"Zasilanie transceivera {e['transceiver']}: {name(e)}",
                             f"Podano {e.get('vcc')} V, wymagane {tr['vcc'][0]}-{tr['vcc'][1]} V. {tr['note']}.",
                             f"karta katalogowa {e['transceiver']}", [e["id"]]))

    # 5. stan pracy
    online = [e for k, e in parts if live.get(e["id"], {}).get("online")]
    if len(online) < 2:
        out.append(_item("warn", "Mniej niż 2 aktywne węzły",
                         "Samotny nadawca nie dostaje ACK: TEC rośnie do 128 (Error Passive) i ramka jest powtarzana w nieskończoność.",
                         "ISO 11898-1"))
    return out
