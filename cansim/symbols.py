"""Symbole firmware dla UI i konsoli: funkcje (z typami argumentow) i zmienne globalne, wyczytane z plikow zrodlowych.

Parser jest heurystyczny (bez pelnej analizy C): deklaracje na najwyzszym poziomie pliku, po usunieciu komentarzy,
napisow i dyrektyw preprocesora. Zmienne i funkcje `static` sa oznaczone jako niedostepne z zewnatrz (nie ma ich
w tablicy symboli biblioteki, wiec nie da sie ich czytac ani wolac).
"""
import re

from .rewrite import mask, _define_spans

QUALIFIERS = {"volatile", "const", "static", "extern", "register", "inline", "__inline", "__IO", "__I", "__O"}

# typ C -> (klucz typu, rozmiar). Typy nieznane (struktury, enumy, typedef) -> None
CTYPES = {
    "uint8_t": "u8", "unsigned char": "u8", "char": "i8", "signed char": "i8", "int8_t": "i8", "bool": "u8", "_Bool": "u8",
    "uint16_t": "u16", "unsigned short": "u16", "int16_t": "i16", "short": "i16",
    "uint32_t": "u32", "unsigned": "u32", "unsigned int": "u32", "int32_t": "i32", "int": "i32",
    "uint64_t": "u64", "unsigned long long": "u64", "int64_t": "i64", "long long": "i64",
    "unsigned long": "ulong", "long": "long",
    "float": "f32", "double": "f64",
}


def _clean(src):
    m = list(mask(src))
    for s, e in _define_spans("".join(m)):
        for k in range(s, e):
            if m[k] != "\n":
                m[k] = " "
    return "".join(m)


def _type_key(tokens):
    t = " ".join(x for x in tokens if x not in QUALIFIERS)
    return CTYPES.get(t)


def _split_top(s, sep=","):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _params(ptxt):
    ptxt = ptxt.strip()
    if ptxt in ("", "void"):
        return []
    out = []
    for p in _split_top(ptxt):
        p = p.strip()
        if p == "...":
            out.append({"type": "...", "name": "...", "key": None})
            continue
        ptr = "*" in p or "[" in p
        toks = re.findall(r"[A-Za-z_]\w*", p)
        name = toks[-1] if len(toks) > 1 else ""
        typ_toks = toks[:-1] if len(toks) > 1 else toks
        out.append({"type": " ".join(typ_toks) + (" *" if ptr else ""), "name": name,
                    "key": None if ptr else _type_key(typ_toks)})
    return out


def _func_head(raw):
    """'static int foo(int a, float b)' -> (nazwa, tokeny typu zwracanego, tekst parametrow, offset nazwy) albo None."""
    j = len(raw.rstrip())
    if j == 0 or raw[j - 1] != ")":
        return None
    depth, k = 0, j - 1
    while k >= 0:
        if raw[k] == ")":
            depth += 1
        elif raw[k] == "(":
            depth -= 1
            if depth == 0:
                break
        k -= 1
    if k < 0:
        return None
    ptxt = raw[k + 1:j - 1]
    e = k
    while e > 0 and raw[e - 1].isspace():
        e -= 1
    b = e
    while b > 0 and (raw[b - 1].isalnum() or raw[b - 1] == "_"):
        b -= 1
    name = raw[b:e]
    if not name or not (name[0].isalpha() or name[0] == "_") or name in ("if", "for", "while", "switch", "return"):
        return None
    before = raw[:b]
    if "=" in before or ")" in before[-200:]:
        return None
    ret_toks = re.findall(r"[A-Za-z_]\w*|\*", before[-200:])
    if not ret_toks:
        return None
    return name, ret_toks, ptxt, b


def parse_file(text, fname):
    m = _clean(text)
    funcs, vars_ = [], []
    depth, start, n = 0, 0, len(m)
    import bisect
    nl = [k for k, c in enumerate(m) if c == "\n"]
    line_of = lambda pos: bisect.bisect_left(nl, pos) + 1
    for i in range(n):
        ch = m[i]
        if ch == "{":
            if depth == 0:
                raw = m[start:i]
                f = _func_head(raw)
                if f:
                    name, ret_toks, ptxt, off = f
                    static = "static" in ret_toks
                    ret = " ".join(t for t in ret_toks if t not in QUALIFIERS)
                    funcs.append({"name": name, "ret": ret, "ret_key": None if "*" in ret else _type_key(ret.split()),
                                  "params": _params(ptxt), "file": fname, "line": line_of(start + off), "static": static})
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                start = i + 1
                if m[i + 1:i + 2] == ";":         # koniec definicji struct/enum na poziomie pliku
                    pass
        elif ch == ";" and depth == 0:
            stmt = re.sub(r"\s+", " ", m[start:i]).strip()
            stmt_pos = i
            start = i + 1
            if not stmt or "(" in stmt or stmt.startswith(("typedef", "extern", "struct", "enum", "union")):
                continue
            toks0 = re.findall(r"[A-Za-z_]\w*", stmt.split("=")[0].split("[")[0])
            static = "static" in toks0
            parts = _split_top(stmt)
            first_toks = re.findall(r"[A-Za-z_]\w*", parts[0].split("=")[0].split("[")[0])
            if len(first_toks) < 2:
                continue
            base_type = first_toks[:-1]
            for k, decl in enumerate(parts):
                d = decl.split("=")[0]
                arr = re.search(r"\[([^\]]*)\]", d)
                ptr = "*" in d
                name_toks = re.findall(r"[A-Za-z_]\w*", d.split("[")[0])
                if not name_toks:
                    continue
                name = name_toks[-1]
                key = None if ptr else _type_key(base_type)
                vars_.append({"name": name, "type": " ".join(t for t in base_type if t not in QUALIFIERS) + (" *" if ptr else ""),
                              "key": key, "array": arr.group(1).strip() if arr else None, "file": fname,
                              "line": line_of(stmt_pos), "static": static})
    return funcs, vars_


def parse_project(root, sources):
    from pathlib import Path
    funcs, vars_ = [], []
    for s in sources:
        p = Path(root) / s
        try:
            f, v = parse_file(p.read_text(encoding="latin-1"), s)
        except OSError:
            continue
        funcs += f
        vars_ += v
    seen = set()
    vars_ = [v for v in vars_ if not (v["name"] in seen or seen.add(v["name"]))]
    return {"funcs": sorted(funcs, key=lambda f: f["name"].lower()), "vars": sorted(vars_, key=lambda v: v["name"].lower())}
