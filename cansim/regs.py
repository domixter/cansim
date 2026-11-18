"""Czytanie oficjalnego naglowka CMSIS ST (stm32h743xx.h itp.): peryferia, rejestry, pola bitowe, IRQ.

Dzieki temu nazwy i pozycje bitow pochodza od ST, a nie z pamieci autora symulatora.
"""
import re
from dataclasses import dataclass, field

SIZES = {"uint8_t": 1, "uint16_t": 2, "uint32_t": 4, "int32_t": 4, "uint64_t": 8}


@dataclass
class Reg:
    name: str
    offset: int
    size: int = 4
    count: int = 1          # >1 dla tablic (np. AFR[2])


@dataclass
class Device:
    header: str
    periphs: dict = field(default_factory=dict)   # nazwa -> typ struktury (np. FDCAN1 -> FDCAN_GlobalTypeDef)
    bases: dict = field(default_factory=dict)     # nazwa -> adres bazowy (liczba)
    layouts: dict = field(default_factory=dict)   # typ -> {"size": n, "regs": [Reg]}
    fields: dict = field(default_factory=dict)    # (prefix, reg) -> [(pole, pos, szerokosc)]
    irqs: dict = field(default_factory=dict)      # nazwa IRQn -> numer
    defines: dict = field(default_factory=dict)   # makra liczbowe (np. FDCAN1_BASE)

    def type_prefix(self, tname):
        return re.sub(r"_(Global)?TypeDef$", "", tname)

    def reg_at(self, tname, off):
        """Rejestr (i indeks) lezacy pod offsetem."""
        for r in self.layouts[tname]["regs"]:
            span = r.size * r.count
            if r.offset <= off < r.offset + span:
                idx = (off - r.offset) // r.size
                return r, idx
        return None, 0

    def decode(self, pname, regname, value):
        """Lista (pole, wartosc, pos, szerokosc) dla rejestru; pusta, gdy brak danych."""
        prefix = self.type_prefix(self.periphs[pname])
        regname = reg_alias(regname)
        out = []
        for fname, pos, width in self.fields.get((prefix, regname), []):
            out.append((fname, (value >> pos) & ((1 << width) - 1), pos, width))
        return out


def reg_alias(regname):
    """Rejestry w podstrukturach (bxCAN) maja w naglowku ST pola nazwane inaczej: sTxMailBox[1].TIR -> TI1R itd."""
    m = re.match(r"(\w+)\[(\d+)\]\.(\w+)$", regname)
    if not m:
        return regname
    arr, i, r = m.groups()
    if arr == "sTxMailBox" or arr == "sFIFOMailBox":
        return f"{r[:-1]}{i}R" if r in ("TIR", "RIR") else f"{r[:-1]}{i}R"
    if arr == "sFilterRegister":
        return f"F{i}{r[1:]}" if r.startswith("FR") else regname
    return regname


def _strip_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _parse_structs(text):
    """typedef struct {...} Nazwa; -> {Nazwa: [(typ, nazwa, liczba)]}"""
    clean = _strip_comments(text)
    structs = {}
    for m in re.finditer(r"typedef\s+struct\s*\{(.*?)\}\s*(\w+)\s*;", clean, flags=re.S):
        members = []
        for line in m.group(1).split(";"):
            line = " ".join(line.split())
            if not line:
                continue
            line = re.sub(r"^(__IO|__I|__O|__IM|__OM|__IOM|volatile|const)\s+", "", line)
            line = re.sub(r"^(__IO|__I|__O|__IM|__OM|__IOM|volatile|const)\s+", "", line)
            mm = re.match(r"(\w+)\s+(\w+)(?:\s*\[\s*(\w+)\s*\])?$", line)
            if not mm:
                continue
            typ, name, cnt = mm.groups()
            members.append((typ, name, int(cnt, 0) if cnt else 1))
        structs[m.group(2)] = members
    return structs


def _layout(structs, tname, cache):
    """Rozklad struktury: [(sciezka, offset, rozmiar, liczba)] + calkowity rozmiar."""
    if tname in cache:
        return cache[tname]
    off, regs, align = 0, [], 1
    for typ, name, cnt in structs[tname]:
        if typ in SIZES:
            sz = SIZES[typ]
            off = (off + sz - 1) // sz * sz
            regs.append(Reg(name, off, sz, cnt))
            off += sz * cnt
            align = max(align, sz)
        elif typ in structs:
            sub = _layout(structs, typ, cache)
            a = sub["align"]
            off = (off + a - 1) // a * a
            for i in range(cnt):
                for r in sub["regs"]:
                    nm = f"{name}[{i}].{r.name}" if cnt > 1 else f"{name}.{r.name}"
                    regs.append(Reg(nm, off + i * sub["size"] + r.offset, r.size, r.count))
            off += sub["size"] * cnt
            align = max(align, a)
    size = (off + align - 1) // align * align
    cache[tname] = {"size": size, "regs": regs, "align": align}
    return cache[tname]


def parse_device(header_path):
    with open(header_path, encoding="latin-1") as fh:
        text = fh.read()
    dev = Device(header=header_path)
    structs = _parse_structs(text)
    cache = {}
    for tname in structs:
        lay = _layout(structs, tname, cache)
        dev.layouts[tname] = {"size": lay["size"], "regs": lay["regs"]}
    # makra liczbowe typu #define FDCAN1_BASE (D2_APB1PERIPH_BASE + 0xA000UL) wyliczamy iteracyjnie
    raw = {}
    for m in re.finditer(r"^#define\s+(\w+)\s+(\(?[^\n/]*?\)?)\s*(?:/\*.*)?$", text, flags=re.M):
        raw[m.group(1)] = m.group(2).strip()

    def val(name, depth=0):
        if name in dev.defines:
            return dev.defines[name]
        expr = raw.get(name)
        if expr is None or depth > 12:
            return None
        e = re.sub(r"(0x[0-9A-Fa-f]+|\d+)[UuLl]+\b", r"\1", expr)
        toks = re.findall(r"(?<![\w])[A-Za-z_]\w*", e)
        for t in toks:
            v = val(t, depth + 1)
            if v is None:
                return None
            e = re.sub(rf"\b{t}\b", str(v), e)
        try:
            v = eval(e, {"__builtins__": {}}, {})
            if isinstance(v, int):
                dev.defines[name] = v
                return v
        except Exception:
            return None
        return None

    for m in re.finditer(r"^#define\s+(\w+)\s+\(\(\s*(\w+)\s*\*\s*\)\s*(\w+)\s*\)", text, flags=re.M):
        pname, tname, base = m.groups()
        if tname in dev.layouts:
            dev.periphs[pname] = tname
            b = val(base)
            if b is not None:
                dev.bases[pname] = b
    for n in list(raw):
        val(n)
    for m in re.finditer(r"^\s*(\w+_IRQn)\s*=\s*(-?\d+)", text, flags=re.M):
        dev.irqs[m.group(1)] = int(m.group(2))
    # pola bitowe z _Pos i komentarza z maska
    pos = {}
    for m in re.finditer(r"^#define\s+(\w+)_Pos\s+\((\d+)U?\)", text, flags=re.M):
        pos[m.group(1)] = int(m.group(2))
    for m in re.finditer(r"^#define\s+(\w+)_Msk\s+\([^\n]*?/\*!<\s*0x([0-9A-Fa-f]+)", text, flags=re.M):
        base, maskhex = m.group(1), int(m.group(2), 16)
        if base not in pos:
            continue
        parts = base.split("_", 2)
        if len(parts) < 3:
            continue
        p, r, f = parts
        width = bin(maskhex >> pos[base]).count("1") if maskhex else 1
        dev.fields.setdefault((p, r), []).append((f, pos[base], max(width, 1)))
    for k in dev.fields:
        dev.fields[k].sort(key=lambda x: -x[1])
    return dev
