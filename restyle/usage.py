"""Per-user usage limits, persisted to a small JSON file so a page refresh doesn't reset them.

On Streamlit Community Cloud the file lives as long as the app's container, so counts
reset when the app is rebooted or redeployed.
"""

import json
import os
import threading
from pathlib import Path

LIMITS = {"uploads": 5, "renders": 5, "refines": 10}
LABELS = {"uploads": "Photos uploaded", "renders": "After pictures", "refines": "Design changes"}

_FILE = Path(os.getenv("RESTYLE_USAGE_FILE", Path(__file__).resolve().parent.parent / ".usage.json"))
_LOCK = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(_FILE.read_text())
    except (OSError, ValueError):
        return {}


def get(user: str) -> dict[str, int]:
    saved = _load().get(user, {})
    return {kind: int(saved.get(kind, 0)) for kind in LIMITS}


def remaining(user: str, kind: str) -> int:
    return max(0, LIMITS[kind] - get(user)[kind])


def record(user: str, kind: str) -> None:
    with _LOCK:
        data = _load()
        counts = data.setdefault(user, {})
        counts[kind] = int(counts.get(kind, 0)) + 1
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_FILE)
