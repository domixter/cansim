"""Pobiera oficjalne naglowki ST do third_party/ (git albo ZIP z GitHub). Uzycie: python tools/fetch_cmsis.py [h7] [f4]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cansim.cmsis import REPOS, fetch  # noqa: E402

ok = all(fetch(f) for f in (sys.argv[1:] or REPOS))
print("OK" if ok else "Nie udalo sie pobrac wszystkich naglowkow (sprawdz internet).")
sys.exit(0 if ok else 1)
