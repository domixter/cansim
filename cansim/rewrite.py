"""Instrumentacja zrodel C przed kompilacja na PC.

Co robi (kod uzytkownika na dysku NIE jest zmieniany, powstaja kopie):
  1. Dostep do rejestrow peryferiow `FDCAN1->IR`, `GPIOD->MODER |= x`, `CAN1->sTxMailBox[0].TIR = y`
     zamienia na wywolania CANSIM_RD/CANSIM_WR, dzieki czemu model sprzetu widzi kazdy odczyt i zapis
     (konieczne m.in. do poprawnego "zapis 1 kasuje flage").
  2. Makra wskazujace na Message RAM (0x4000AC00 / SRAMCAN_BASE) przekierowuje na tablice w pamieci PC.
  3. Na poczatku kazdej funkcji wstawia CANSIM_FN("nazwa") (sledzenie wywolan).
  4. Opcjonalnie podmienia cialo wskazanych funkcji (np. petle opozniajace) na wersje dla symulatora.
Ograniczenia: wskaznik na peryferium (`FDCAN_GlobalTypeDef *p = FDCAN1; p->IR`) i adres `&FDCAN1->DR`
nie sa przechwytywane.
"""
import re

KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "do", "else", "case"}
ASSIGN = re.compile(r"(<<=|>>=|\|=|&=|\^=|\+=|-=|\*=|/=|%=|=(?!=))")


def mask(src):
    """Kopia tekstu o tej samej dlugosci, w ktorej komentarze i literaly sa zastapione spacjami."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            for k in range(i + 1, j - 1):
                out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _define_spans(m):
    """Zakresy (start, koniec) logicznych linii preprocesora w tekscie zamaskowanym."""
    spans = []
    for mt in re.finditer(r"^[ \t]*#", m, flags=re.M):
        s = mt.start()
        e = s
        while True:
            e = m.find("\n", e)
            if e < 0:
                e = len(m)
                break
            if m[e - 1] == "\\":
                e += 1
                continue
            break
        spans.append((s, e))
    return spans


def _match_bracket(m, i, hi):
    """m[i] to otwierajacy nawias; zwraca indeks znaku PO zamykajacym."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[m[i]]]
    j = i + 1
    while j < hi and stack:
        ch = m[j]
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch == stack[-1]:
            stack.pop()
        j += 1
    return j


def _parse_chain(m, j, hi):
    """Za 'PERIPH->' : identyfikator, potem dowolnie [..] / .id."""
    mt = re.compile(r"\s*[A-Za-z_]\w*").match(m, j, hi)
    if not mt:
        return j
    j = mt.end()
    while j < hi:
        k = j
        while k < hi and m[k] in " \t":
            k += 1
        if k < hi and m[k] == "[":
            j = _match_bracket(m, k, hi)
        elif k < hi and m[k] == "." and not m.startswith("..", k):
            mt = re.compile(r"\.\s*[A-Za-z_]\w*").match(m, k, hi)
            if not mt:
                break
            j = mt.end()
        else:
            break
    return j


def _rhs_end(m, j, hi, spans_end):
    depth = 0
    while j < hi:
        ch = m[j]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return j
            depth -= 1
        elif depth == 0 and ch in ";,":
            return j
        elif depth == 0 and ch == "\n" and spans_end(j):
            return j
        j += 1
    return hi


def rewrite_registers(src, periph_ids):
    """periph_ids: {nazwa_peryferium: numer}."""
    if not periph_ids:
        return src
    names = "|".join(sorted(periph_ids, key=len, reverse=True))
    pat = re.compile(rf"\b({names})\s*->")
    m = mask(src)
    spans = _define_spans(m)

    def in_define_end(pos):
        # czy znak \n na pozycji pos konczy logiczna linie #define
        for s, e in spans:
            if s <= pos <= e:
                return pos == e
        return False

    def tr(lo, hi):
        out, i = [], lo
        while i < hi:
            mt = pat.search(m, i, hi)
            if not mt:
                out.append(src[i:hi])
                break
            s = mt.start()
            out.append(src[i:s])
            pid = periph_ids[mt.group(1)]
            cend = _parse_chain(m, mt.end(), hi)
            lv = src[s:cend]
            before = m[lo:s].rstrip(" \t\r\n(")
            if before.endswith("&") and not before.endswith("&&"):
                out.append(lv)           # adres rejestru: bez zmian
                i = cend
                continue
            k = cend
            while k < hi and m[k] in " \t":
                k += 1
            am = ASSIGN.match(m, k, hi)
            if am:
                op = am.group(1)
                rend = _rhs_end(m, k + len(op), hi, in_define_end)
                rhs = tr(k + len(op), rend)
                if op == "=":
                    out.append(f"CANSIM_WR({pid}, {lv}, {rhs.strip()})")
                else:
                    out.append(f"CANSIM_WR({pid}, {lv}, CANSIM_RD({pid}, {lv}) {op[:-1]} ({rhs.strip()}))")
                i = rend
            else:
                out.append(f"CANSIM_RD({pid}, {lv})")
                i = cend
        return "".join(out)

    return tr(0, len(src))


def redirect_message_ram(src, extra_names=()):
    """#define X ... 0x4000AC00 / SRAMCAN_BASE ...  ->  tablica w pamieci PC."""
    out = []
    names = set(extra_names)
    for line in src.split("\n"):
        mt = re.match(r"(\s*#\s*define\s+(\w+))\b(.*)$", line)
        if mt and (mt.group(2) in names or re.search(r"SRAMCAN_BASE|0x4000[Aa][Cc]00", mt.group(3))):
            line = f"{mt.group(1)} ((uintptr_t)cansim_msgram)  /* cansim: przekierowano z {mt.group(3).strip()[:40]} */"
        out.append(line)
    return "\n".join(out)


def instrument_functions(src, replace=None):
    """Wstawia CANSIM_FN(\"nazwa\"); na poczatku kazdej funkcji; podmienia cialo funkcji z `replace`."""
    replace = replace or {}
    m = mask(src)
    # linie preprocesora wylaczamy z liczenia klamer
    mm = list(m)
    for s, e in _define_spans(m):
        for k in range(s, e):
            if mm[k] != "\n":
                mm[k] = " "
    mm = "".join(mm)
    edits = []  # (pozycja, usun_do, tekst)
    depth, i, n = 0, 0, len(mm)
    found = []
    while i < n:
        ch = mm[i]
        if ch == "{":
            if depth == 0:
                j = i - 1
                while j >= 0 and mm[j] in " \t\r\n":
                    j -= 1
                if j >= 0 and mm[j] == ")":
                    # znajdz pasujacy '('
                    d, k = 0, j
                    while k >= 0:
                        if mm[k] == ")":
                            d += 1
                        elif mm[k] == "(":
                            d -= 1
                            if d == 0:
                                break
                        k -= 1
                    idm = re.search(r"([A-Za-z_]\w*)\s*$", mm[:k])
                    if idm and idm.group(1) not in KEYWORDS:
                        end = _match_bracket(mm, i, n)
                        found.append((idm.group(1), i, end))
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    for name, bpos, end in found:
        if name in replace:
            nl = mm[bpos:end].count("\n")         # zachowaj liczbe linii, zeby numery linii (#line) sie zgadzaly
            edits.append((bpos, end, "{ " + replace[name] + " }" + "\n" * nl))
        else:
            edits.append((bpos + 1, bpos + 1, f' CANSIM_FN("{name}");'))
    for pos, upto, text in sorted(edits, key=lambda e: -e[0]):
        src = src[:pos] + text + src[upto:]
    return src, [f[0] for f in found]


def add_spin_hooks(src):
    """Puste petle oczekujace (while (...) {} / while (...);) dostaja CANSIM_SPIN(), zeby czas wirtualny plynal."""
    m = mask(src)
    edits = []
    for mt in re.finditer(r"\b(while|for)\s*\(", m):
        po = mt.end() - 1
        pe = _match_bracket(m, po, len(m))
        k = pe
        while k < len(m) and m[k] in " \t\r\n":
            k += 1
        if k >= len(m):
            continue
        if m[k] == "{":
            j = k + 1
            while j < len(m) and m[j] in " \t\r\n":
                j += 1
            if j < len(m) and m[j] == "}":
                edits.append((k + 1, k + 1, " CANSIM_SPIN(); "))
        elif m[k] == ";":
            before = m[:mt.start()].rstrip()
            if mt.group(1) == "while" and before.endswith("}"):
                continue                      # koniec do { } while (...);
            edits.append((k, k + 1, "{ CANSIM_SPIN(); }"))
    for a, b, t in sorted(edits, key=lambda e: -e[0]):
        src = src[:a] + t + src[b:]
    return src


def instrument(src, periph_ids, replace=None, ram_names=(), is_header=False, fname=None):
    src = redirect_message_ram(src, ram_names)
    src = rewrite_registers(src, periph_ids)
    funcs = []
    src = add_spin_hooks(src)
    if not is_header:
        src, funcs = instrument_functions(src, replace)
    line = f'#line 1 "{fname}"\n' if fname else ""
    return '#include "cansim_rt.h"\n' + line + src, funcs
