"""Find the people in a video clip, cut one of them out as their own video, and paste an edited
version back. This lets a whole-person swap model work on just one person when several are in shot.
"""

import io
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .faces import _detect
from .video import _run, duration

SAMPLE_EVERY = 0.4  # seconds between the frames used to follow each person


def _frames(clip: bytes) -> list[np.ndarray]:
    """Frames sampled through the clip (BGR), starting with the first."""
    with tempfile.TemporaryDirectory(prefix="faceclips_") as tmp:
        path = Path(tmp) / "clip.mp4"
        path.write_bytes(clip)
        cap = cv2.VideoCapture(str(path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        step = max(1, round(fps * SAMPLE_EVERY))
        frames, n = [], 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if n % step == 0:
                frames.append(frame)
            n += 1
        cap.release()
        return frames


def _even(v: float) -> int:
    return int(v) // 2 * 2


def find_people(clip: bytes) -> tuple[Image.Image | None, list[dict]]:
    """Return (first frame, people). Each person has 'face' (their face box in the first frame),
    'box' (x, y, w, h: the part of the frame holding just that person for the whole clip, or None
    if they can't be separated from the others) and 'seen' (the share of the clip, 0 to 1, in which
    their face was found). People are ordered left to right."""
    frames = _frames(clip)
    if not frames:
        return None, []
    height, width = frames[0].shape[:2]
    first = Image.fromarray(cv2.cvtColor(frames[0], cv2.COLOR_BGR2RGB))
    start = sorted(_detect(frames[0]), key=lambda f: f[0])
    # Follow each person through the clip: [x0, y0, x1, y1] covering everywhere their face goes.
    last = [f[:4].copy() for f in start]
    spans = [[f[0], f[1], f[0] + f[2], f[1] + f[3]] for f in start]
    hits = [1] * len(start)
    for frame in frames[1:]:
        found = _detect(frame)
        for i, box in enumerate(last):
            cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
            near = [f for f in found if np.hypot(f[0] + f[2] / 2 - cx, f[1] + f[3] / 2 - cy) < 1.2 * box[2]]
            if not near:
                continue
            f = min(near, key=lambda f: np.hypot(f[0] + f[2] / 2 - cx, f[1] + f[3] / 2 - cy))
            last[i] = f[:4].copy()
            hits[i] += 1
            s = spans[i]
            spans[i] = [min(s[0], f[0]), min(s[1], f[1]), max(s[2], f[0] + f[2]), max(s[3], f[1] + f[3])]

    people = []
    for i, (face, span) in enumerate(zip(start, spans)):
        fw, fh = float(face[2]), float(face[3])
        left, right = span[0] - 2.2 * fw, span[2] + 2.2 * fw
        top = span[1] - 1.3 * fh
        separable = True
        for j, other in enumerate(spans):
            if j == i:
                continue
            if other[0] >= span[2]:    # someone to the right: stop halfway between the two faces
                right = min(right, (span[2] + other[0]) / 2)
            elif other[2] <= span[0]:  # someone to the left
                left = max(left, (other[2] + span[0]) / 2)
            else:                      # their faces cross paths, so a simple cut can't separate them
                separable = False
        x, y = _even(max(0, left)), _even(max(0, top))
        w, h = _even(min(width, right) - x), _even(height - y)
        ok = separable and w >= 1.5 * fw and h >= 2 * fh
        people.append({"face": tuple(int(v) for v in face[:4]), "box": (x, y, w, h) if ok else None,
                       "seen": hits[i] / len(frames)})
    return first, people


def draw_people(frame: Image.Image, people: list[dict], chosen: int | None = None) -> Image.Image:
    """The first frame with each person's area outlined and numbered."""
    out = frame.copy()
    draw = ImageDraw.Draw(out)
    line = max(3, frame.width // 250)
    for n, p in enumerate(people):
        x, y, w, h = p["box"] or p["face"]
        colour = (255, 200, 0) if n == chosen else (255, 255, 255)
        draw.rectangle([x, y, x + w - 1, y + h - 1], outline=colour, width=line)
        draw.rectangle([x, y, x + line * 12, y + line * 9], fill=colour)
        draw.text((x + line * 3, y + line), str(n + 1), fill=(0, 0, 0), font_size=line * 6)
    return out


def crop_clip(clip: bytes, box: tuple[int, int, int, int]) -> bytes:
    """The part of the clip inside box (x, y, w, h), as its own video with the original sound.
    Small pieces are enlarged (shorter side 720px) so the swap model can still make out the person."""
    x, y, w, h = box
    grow = 720 / min(w, h)
    scale = f",scale={_even(w * grow)}:{_even(h * grow)}:flags=lanczos" if grow > 1.1 else ""
    scale += ",setsar=1"  # rounding the size can leave pixels marked slightly non-square, which models reject
    with tempfile.TemporaryDirectory(prefix="faceclips_") as tmp:
        src, out = Path(tmp) / "in.mp4", Path(tmp) / "out.mp4"
        src.write_bytes(clip)
        result = _run(["-y", "-i", str(src), "-vf", f"crop={w}:{h}:{x}:{y}{scale}", "-c:v", "libx264", "-preset", "veryfast",
                       "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(out)])
        if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("That clip couldn't be cut down to one person.")
        return out.read_bytes()


def _edge_mask(box: tuple[int, int, int, int], frame_size: tuple[int, int]) -> Image.Image:
    """White with soft dark edges, so the pasted piece fades into the frame. Edges that sit on the
    border of the frame stay hard, because there is nothing to blend into there."""
    x, y, w, h = box
    fade = max(4, min(w, h) // 14)
    mask = Image.new("L", (w + 4 * fade, h + 4 * fade), 0)
    left = 2 * fade + (fade if x > 0 else -2 * fade)
    top = 2 * fade + (fade if y > 0 else -2 * fade)
    right = 2 * fade + w - (fade if x + w < frame_size[0] else -2 * fade)
    bottom = 2 * fade + h - (fade if y + h < frame_size[1] else -2 * fade)
    ImageDraw.Draw(mask).rectangle([left, top, right, bottom], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(fade / 2))
    return mask.crop((2 * fade, 2 * fade, 2 * fade + w, 2 * fade + h))


def paste_back(clip: bytes, piece: bytes, box: tuple[int, int, int, int]) -> bytes:
    """Put an edited piece back into the clip at box, with soft edges and the clip's own sound."""
    x, y, w, h = box
    with tempfile.TemporaryDirectory(prefix="faceclips_") as tmp:
        base, top, mask, out = (Path(tmp) / n for n in ("clip.mp4", "piece.mp4", "mask.png", "out.mp4"))
        base.write_bytes(clip)
        top.write_bytes(piece)
        cap = cv2.VideoCapture(str(base))
        size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or x + w, int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or y + h)
        cap.release()
        _edge_mask(box, size).save(mask)
        graph = (f"[1:v]scale={w}:{h},format=rgb24[p];[2:v]scale={w}:{h},format=gray[m];"
                 f"[p][m]alphamerge[pa];[0:v][pa]overlay={x}:{y}:eof_action=repeat[v]")
        result = _run(["-y", "-i", str(base), "-i", str(top), "-loop", "1", "-i", str(mask), "-filter_complex", graph,
                       "-map", "[v]", "-map", "0:a?", "-t", f"{duration(base):.2f}", "-c:v", "libx264",
                       "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy",
                       "-movflags", "+faststart", str(out)])
        if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("The swapped person couldn't be put back into the clip.")
        return out.read_bytes()


def on_plain_background(cutout_png: bytes) -> bytes:
    """A cut-out (PNG with transparency) placed on plain light grey, as JPEG."""
    img = Image.open(io.BytesIO(cutout_png)).convert("RGBA")
    plain = Image.new("RGBA", img.size, (235, 235, 235, 255))
    plain.alpha_composite(img)
    buf = io.BytesIO()
    plain.convert("RGB").save(buf, format="JPEG", quality=93)
    return buf.getvalue()
