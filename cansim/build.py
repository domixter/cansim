import json, os, re, shutil, subprocess, sys
from dataclasses import dataclass, field
from pathlib import Path

from .regs import parse_device
from .rewrite import instrument

SHIM = Path(__file__).parent / "shim"
THIRD_PARTY = Path(__file__).resolve().parent.parent / "third_party"
# domyslne ustawienia rodzin; zegary trzeba dopasowac do swojego clock.c (symulator nie emuluje drzewa zegarow)
FAMILIES = {
    "h7": {"device_header": "stm32h743xx.h", "device_define": "STM32H743xx", "can_peripheral": "FDCAN1",
           "can_kernel_hz": 64000000, "timer_clk_hz": 128000000, "core_hz": 256000000},
    "f4": {"device_header": "stm32f407xx.h", "device_define": "STM32F407xx", "can_peripheral": "CAN1",
           "can_kernel_hz": 42000000, "timer_clk_hz": 84000000, "core_hz": 168000000},
}
CONFIG_KEYS = ("name", "root", "family", "device_header", "device_define", "sources", "include_dirs", "defines", "init",
               "can_peripheral", "can_kernel_hz", "timer_clk_hz", "core_hz", "replace_functions", "watch", "quiet_funcs",
               "cmsis_dir")
LIBEXT = {"win32": ".dll", "darwin": ".dylib"}.get(sys.platform, ".so")


@dataclass
class Project:
    root: str
    name: str = "projekt"
    family: str = "h7"                      # h7 (FDCAN) albo f4 (bxCAN)
    cmsis_dir: str = ""                     # katalog cmsis_device_xx (z podkatalogiem Include)
    device_header: str = "stm32h743xx.h"
    device_define: str = "STM32H743xx"
    sources: list = field(default_factory=list)
    include_dirs: list = field(default_factory=lambda: ["."])
    defines: list = field(default_factory=list)
    replace_functions: dict = field(default_factory=dict)
    init: list = field(default_factory=list)       # funkcje wolane po starcie, np. ["main"]
    periodic: list = field(default_factory=list)   # [{"fn": "TIM7_IRQHandler", "period_ms": 25}]
    can_kernel_hz: int = 64000000                  # zegar jadra FDCAN (u Was PLL1 Q = 64 MHz)
    timer_clk_hz: int = 128000000                  # zegar timerow APB (z clock.c: APB1 64 MHz x2)
    core_hz: int = 256000000                       # SystemCoreClock widziany przez kod i SysTick
    watch: list = field(default_factory=list)      # [{"name": "can_tx_dropped", "type": "u32"}]
    ram_macros: list = field(default_factory=list)
    nodes: list = field(default_factory=list)      # wezly magistrali, np. [{"type": "odrive", "node_id": 1}]
    can_peripheral: str = "FDCAN1"
    trace_periphs: list = field(default_factory=lambda: ["FDCAN1", "FDCAN2", "CAN1", "CAN2"])
    quiet_funcs: list = field(default_factory=list)  # funkcje pomijane w dzienniku wywolan

    @classmethod
    def load(cls, path):
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data, path.parent)

    @classmethod
    def from_dict(cls, data, base):
        """Sciezki wzgledne liczone od `base`. Brakujace pola: domyslne dla rodziny; cmsis_dir: third_party/ programu."""
        data = dict(data)
        base = Path(base)
        fam = data.get("family", "h7")
        for k, v in FAMILIES.get(fam, {}).items():
            data.setdefault(k, v)
        data.setdefault("root", str(base))
        data["root"] = str((base / os.path.expanduser(data["root"])).resolve())
        cm = data.get("cmsis_dir", "")
        if cm:
            data["cmsis_dir"] = str((base / os.path.expanduser(cm)).resolve())
        else:
            data["cmsis_dir"] = str(THIRD_PARTY / f"cmsis_device_{fam}")
        data.setdefault("init", ["main"])
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_config(self):
        return {k: getattr(self, k) for k in CONFIG_KEYS}


SKIP_DIRS = {".cansim_build", ".git", "Drivers", "build", "Debug", "Release", "__pycache__"}


def auto_config(folder, family="h7", name=None):
    """Konfiguracja plytki na podstawie folderu z kodem: wszystkie pliki .c (bez plikow startowych/systemowych ST i
    katalogow Drivers/build), katalogi z naglowkami jako include, start od main()."""
    folder = Path(folder).resolve()
    srcs, incs = [], set()
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if p.suffix.lower() == ".c" and not p.name.lower().startswith(("system_stm32", "startup_", "syscalls", "sysmem")):
            srcs.append(rel.as_posix())
        if p.suffix.lower() == ".h":
            incs.add(rel.parent.as_posix() or ".")
    cfg = {"name": name or folder.name, "root": str(folder), "family": family, "sources": srcs,
           "include_dirs": sorted(incs) or ["."], "init": ["main"], "watch": []}
    cfg.update({k: v for k, v in FAMILIES[family].items()})
    return cfg


def _gen_periph_files(dev, bdir):
    names = sorted(dev.periphs)
    ids = {n: i for i, n in enumerate(names)}
    over = ["/* wygenerowane przez cansim */", "#ifndef CANSIM_PERIPH_OVERRIDE", "#define CANSIM_PERIPH_OVERRIDE", "#include <stdint.h>"]
    for n in names:
        t = dev.periphs[n]
        over.append(f"extern uint32_t cansim_reg_{n}[(sizeof({t}) + 3) / 4];")
        over.append(f"#undef {n}\n#define {n} (({t} *)cansim_reg_{n})")
    over += ["extern uint8_t cansim_msgram[];", "#undef SRAMCAN_BASE", "#define SRAMCAN_BASE ((uintptr_t)cansim_msgram)", "#endif"]
    (bdir / "shim").mkdir(parents=True, exist_ok=True)
    (bdir / "shim" / "cansim_periph_override.h").write_text("\n".join(over) + "\n")
    c = ['#include <stm32_device.h>', "/* wygenerowane przez cansim */"]
    for n in names:
        c.append(f"uint32_t cansim_reg_{n}[(sizeof({dev.periphs[n]}) + 3) / 4];")
    c.append("uint32_t *const cansim_reg_base_tab[] = {" + ", ".join(f"cansim_reg_{n}" for n in names) + "};")
    c.append(f"const int cansim_periph_count = {len(names)};")
    (bdir / "cansim_periph.c").write_text("\n".join(c) + "\n")
    return ids


def build(prj: Project, bdir=None, opt="-O0"):
    root = Path(prj.root)
    bdir = Path(bdir or root / ".cansim_build")
    if bdir.exists():
        # Windows nie pozwala skasowac zaladowanej biblioteki (fw_*.dll uruchomionej plytki): czyscimy co sie da,
        # zablokowane kopie zostaja i sa usuwane przy nastepnym starcie programu
        for c in bdir.iterdir():
            try:
                shutil.rmtree(c) if c.is_dir() else c.unlink()
            except OSError:
                if c.is_dir():
                    shutil.rmtree(c, ignore_errors=True)
    (bdir / "src").mkdir(parents=True, exist_ok=True)
    inc = Path(prj.cmsis_dir) / "Include"
    if not (inc / prj.device_header).is_file():
        return {"ok": False, "log": f"Brak oficjalnego naglowka ST: {inc / prj.device_header}\n\n"
                f"Pobierz naglowki: przycisk 'Pobierz naglowki ST' w programie albo w terminalu:\n"
                f"  python tools/fetch_cmsis.py\n(albo ustaw cmsis_dir w ustawieniach plytki)"}
    if not prj.sources:
        return {"ok": False, "log": "Plytka nie ma zadnych plikow .c do kompilacji (ustawienia plytki: pliki zrodlowe)."}
    dev = parse_device(str(inc / prj.device_header))
    ids = _gen_periph_files(dev, bdir)
    # wrapper naglowka urzadzenia: wciaga oficjalny plik ST, potem podmienia peryferia na pamiec symulatora
    # kopie pozostalych oficjalnych naglowkow (np. stm32h7xx.h), zeby ich #include "stm32h743xx.h" trafial na wrapper, a nie omijal go
    for hf in inc.glob("*.h"):
        if hf.name != prj.device_header:
            shutil.copy(hf, bdir / "shim" / hf.name)
    for name in {prj.device_header, "stm32_device.h"}:
        body = f'#include "{(inc / prj.device_header).as_posix()}"\n#include "cansim_periph_override.h"\n' if name == prj.device_header else \
               f'#include "{prj.device_header}"\n'
        guard = "CANSIM_W_" + re.sub(r"\W", "_", name).upper()
        (bdir / "shim" / name).write_text(f"#ifndef {guard}\n#define {guard}\n{body}#endif\n")
    for f in SHIM.glob("*"):
        shutil.copy(f, bdir / "shim" / f.name)
    funcs, mapping = {}, {}
    src_set = {str(Path(s)) for s in prj.sources}
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() not in (".c", ".h") or ".cansim_build" in p.parts:
            continue
        rel = p.relative_to(root)
        text = p.read_text(encoding="latin-1")
        new, fl = instrument(text, ids, prj.replace_functions, prj.ram_macros, is_header=p.suffix.lower() == ".h",
                             fname=rel.as_posix())
        out = bdir / "src" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(new, encoding="latin-1")
        mapping[str(rel)] = fl
        funcs.update({f: str(rel) for f in fl})
    # -Bsymbolic: odwolania wewnatrz biblioteki zostaja w niej (kilka plytek = kilka bibliotek z tymi samymi nazwami)
    cmd = ["gcc", "-shared", "-fPIC"] + (["-Wl,-Bsymbolic"] if sys.platform.startswith("linux") else []) + [opt, "-g", "-w", "-std=gnu11", "-fno-strict-aliasing", "-fwrapv",
           f"-D{prj.device_define}", "-include", "string.h", "-include", "stdlib.h", "-include", "math.h",
           f"-I{bdir/'shim'}", f"-I{inc}"]
    cmd += [f"-I{bdir/'src'/d}" for d in prj.include_dirs] + [f"-D{d}" for d in prj.defines]
    cmd += [str(bdir / "src" / s) for s in prj.sources]
    cmd += [str(bdir / "cansim_periph.c"), str(bdir / "shim" / "cansim_rt.c")]
    # symbole, ktore cansim dostarcza tylko wtedy, gdy firmware ich nie definiuje (bez "weak": na Windows nie dziala)
    texts = [(bdir / "src" / x).read_text(encoding="latin-1") for x in prj.sources if (bdir / "src" / x).is_file()]
    for define, rx in (("CANSIM_FW_HAS_SYSCLK", r"^[ \t]*(?:volatile[ \t]+)?uint32_t[ \t]+SystemCoreClock\b[ \t]*(?:=|;)"),
                       ("CANSIM_FW_HAS_SYSINIT", r"^[ \t]*void[ \t]+SystemInit[ \t]*\([ \t]*void[ \t]*\)[\s]*\{"),
                       ("CANSIM_FW_HAS_SYSCLKUPD", r"^[ \t]*void[ \t]+SystemCoreClockUpdate[ \t]*\([ \t]*void[ \t]*\)[\s]*\{")):
        if any(re.search(rx, t, re.M) for t in texts):
            cmd.append(f"-D{define}")
    lib = bdir / f"firmware{LIBEXT}"
    cmd += ["-o", str(lib), "-lm"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    from .symbols import parse_project
    try:
        symbols = parse_project(prj.root, prj.sources)
    except Exception:                       # parser jest pomocniczy: jego blad nie moze zepsuc kompilacji
        symbols = {"funcs": [], "vars": []}
    return {"ok": r.returncode == 0, "lib": str(lib), "log": r.stdout + r.stderr, "cmd": " ".join(cmd),
            "funcs": funcs, "dev": dev, "ids": ids, "symbols": symbols}
