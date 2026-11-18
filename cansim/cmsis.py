"""Pobieranie oficjalnych naglowkow ST (CMSIS device) do third_party/: przez git, a bez gita przez archiwum ZIP z GitHub."""
import io
import shutil
import subprocess
import urllib.request
import zipfile

from .build import THIRD_PARTY

REPOS = {"h7": "STMicroelectronics/cmsis_device_h7", "f4": "STMicroelectronics/cmsis_device_f4"}


def present(fam):
    return (THIRD_PARTY / f"cmsis_device_{fam}" / "Include").is_dir()


def fetch(fam, log=print):
    dest = THIRD_PARTY / f"cmsis_device_{fam}"
    if present(fam):
        log(f"{dest} juz jest")
        return True
    THIRD_PARTY.mkdir(exist_ok=True)
    repo = REPOS[fam]
    if shutil.which("git"):
        try:
            subprocess.check_call(["git", "clone", "--depth", "1", f"https://github.com/{repo}.git", str(dest)])
            return present(fam)
        except (subprocess.CalledProcessError, OSError) as e:
            log(f"git nie zadzialal ({e}), probuje ZIP")
            shutil.rmtree(dest, ignore_errors=True)
    for branch in ("master", "main"):
        url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
        try:
            log(f"pobieram {url}")
            data = urllib.request.urlopen(url, timeout=120).read()
        except OSError as e:
            log(f"  nie udalo sie: {e}")
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            top = z.namelist()[0].split("/")[0]
            tmp = THIRD_PARTY / f".tmp_{fam}"
            shutil.rmtree(tmp, ignore_errors=True)
            z.extractall(tmp)
            (tmp / top).rename(dest)
            shutil.rmtree(tmp, ignore_errors=True)
        return present(fam)
    return False
