"""Per-user usage limits, persisted to a small JSON file so a page refresh doesn't reset them.

On Streamlit Community Cloud the file lives as long as the app's container, so counts
reset when the app is rebooted or redeployed.
"""

import json
import os
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()


class Tracker:
    """Usage counts per user for one app, each kind capped by `limits`."""

    def __init__(self, limits: dict[str, int], labels: dict[str, str], file: str | Path):
        self.limits, self.labels, self.file = limits, labels, Path(file)

    def _load(self) -> dict:
        try:
            return json.loads(self.file.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, user: str) -> dict[str, int]:
        saved = self._load().get(user, {})
        return {kind: int(saved.get(kind, 0)) for kind in self.limits}

    def remaining(self, user: str, kind: str) -> int:
        return max(0, self.limits[kind] - self.get(user)[kind])

    def record(self, user: str, kind: str) -> None:
        with _LOCK:
            data = self._load()
            counts = data.setdefault(user, {})
            counts[kind] = int(counts.get(kind, 0)) + 1
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.file)


# The Restyle app's limits (module-level API used by app.py).
LIMITS = {"uploads": 5, "renders": 5, "refines": 10}
LABELS = {"uploads": "Photos uploaded", "renders": "After pictures", "refines": "Design changes"}
_default = Tracker(LIMITS, LABELS, os.getenv("RESTYLE_USAGE_FILE", _ROOT / ".usage.json"))
get, remaining, record = _default.get, _default.remaining, _default.record
