"""Match each suggested piece to a real Harvey Norman product via Serper's Google Shopping API.

Needs SERPER_API_KEY (serper.dev). Harvey Norman's own site blocks automated access, so we
search Google Shopping listings instead of scraping it.
"""

import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from .tools import FX_TO_AUD

STORE = "Harvey Norman"
SERPER_URL = "https://google.serper.dev/shopping"


def enabled() -> bool:
    return bool(os.getenv("SERPER_API_KEY"))


def _price(text) -> float | None:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(text or ""))
    return float(m.group().replace(",", "")) if m else None


@lru_cache(maxsize=512)
def search(query: str) -> tuple[dict, ...]:
    """Harvey Norman listings on Google Shopping (Australia) for a query. Cached per process."""
    req = urllib.request.Request(
        SERPER_URL,
        data=json.dumps({"q": f"{query} {STORE}", "gl": "au", "hl": "en", "location": "Australia"}).encode(),
        headers={"X-API-KEY": os.environ["SERPER_API_KEY"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        results = json.load(resp).get("shopping", [])
    return tuple(r for r in results if STORE.lower() in str(r.get("source", "")).lower())


def match_item(item: dict) -> dict:
    """Return the item with the closest Harvey Norman product attached (or unchanged if none)."""
    if item.get("product_url"):  # already matched, e.g. a kept piece
        return item
    try:
        hits = search(item["name"])
    except Exception:  # one failed lookup shouldn't sink the whole concept
        return item
    if not hits:
        return item
    hit = hits[0]
    return {**item, "product_title": hit.get("title", ""), "product_price_aud": _price(hit.get("price")),
            "product_url": hit.get("link", ""), "product_image": hit.get("imageUrl", "")}


def match_concept(concept: dict, currency: str) -> dict:
    """Attach Harvey Norman products to every item, using the real price when there is one."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        items = list(pool.map(match_item, concept["items"]))
    rate = FX_TO_AUD.get(currency.upper(), 1.0)
    for item in items:
        if item.get("product_price_aud"):
            item["price_low"] = item["price_high"] = round(item["product_price_aud"] / rate, 2)
    return {**concept, "items": items}
