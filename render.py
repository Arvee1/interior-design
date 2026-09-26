"""Visual helpers for the Streamlit UI."""

import csv
import html
import io

from PIL import Image, ImageDraw, ImageFont

BRASS = (176, 138, 62)
GREEN = (47, 74, 62)
WHITE = (255, 255, 255)
DARK = (27, 20, 7)


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def draw_pins(img: Image.Image, items: list[dict], highlight: str | None = None) -> Image.Image:
    """Draw numbered pins on a copy of the photo at each item's x/y (percent)."""
    out = img.copy()
    draw = ImageDraw.Draw(out)
    w, h = out.size
    r = max(14, int(w * 0.02))
    font = _font(int(r * 1.05))
    for n, item in enumerate(items, start=1):
        if item.get("x") is None or item.get("y") is None:
            continue
        cx, cy = item["x"] / 100 * w, item["y"] / 100 * h
        hot = item["id"] == highlight
        rr = int(r * 1.3) if hot else r
        fill = WHITE if hot else BRASS
        outline = GREEN if item.get("locked") else WHITE
        draw.ellipse((cx - rr - 2, cy - rr - 2, cx + rr + 2, cy + rr + 2), fill=(0, 0, 0))
        draw.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=fill, outline=outline, width=max(3, r // 5))
        label = str(n)
        box = draw.textbbox((0, 0), label, font=font)
        draw.text((cx - (box[2] - box[0]) / 2 - box[0], cy - (box[3] - box[1]) / 2 - box[1]),
                  label, fill=DARK, font=font)
    return out


def palette_html(palette: list[dict]) -> str:
    blocks = "".join(
        f'<div title="{html.escape(p["name"])} {p["hex"]} ({html.escape(p["role"])})" '
        f'style="flex:1;background:{p["hex"]};position:relative;min-width:0">'
        f'<span style="position:absolute;left:6px;bottom:6px;font-size:11px;padding:1px 5px;'
        f'background:rgba(255,255,255,.85);color:#1E2422;border-radius:3px;white-space:nowrap;'
        f'overflow:hidden;text-overflow:ellipsis;max-width:calc(100% - 12px)">{html.escape(p["name"])}</span></div>'
        for p in palette
    )
    return f'<div style="display:flex;height:90px;border:1px solid #D3D8D1;margin:4px 0 8px">{blocks}</div>'


def shopping_list_csv(concept: dict, currency: str) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["#", "Item", "Category", "Description", "Placement", "Priority",
                     f"Price low ({currency})", f"Price high ({currency})", "Kept"])
    for n, i in enumerate(concept["items"], start=1):
        writer.writerow([n, i["name"], i["category"], i["description"], i["placement"], i["priority"],
                         round(i["price_low"]), round(i["price_high"]), "yes" if i.get("locked") else ""])
    return buf.getvalue().encode("utf-8")
