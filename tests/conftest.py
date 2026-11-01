import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in ("shared", "vehicle", "server"):
    sys.path.insert(0, str(ROOT / directory))
