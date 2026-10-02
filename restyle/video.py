"""Video helpers for Face Clips: fetch a clip from a link, trim it, and swap a face into it on Replicate."""

import io
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # bundled binary, so no system ffmpeg is needed
MAX_SOURCE_SECONDS = 600   # refuse to fetch anything longer than 10 minutes
MAX_CLIP_SECONDS = 30      # longest piece sent for face swapping (cost and time grow with length)
VIDEO_HOSTS = ("youtube.com", "youtu.be")

# Replicate video face-swap models (all based on roop) and what each calls its inputs.
# One face photo in, one video in; the main face in the video is swapped.
DEFAULT_SWAP_MODEL = "xrunda/hello"
SWAP_MODELS = {
    "xrunda/hello": {"video": "source", "face": "target"},
    "okaris/roop": {"video": "target", "face": "source"},
    "arabyai-replicate/roop_face_swap": {"video": "target_video", "face": "swap_image"},
}


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([FFMPEG, "-hide_banner", *args], capture_output=True, text=True)


def duration(path: str | Path) -> float:
    """Length of a video file in seconds (0 if it can't be read)."""
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", _run(["-i", str(path)]).stderr)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def fetch_from_link(url: str, workdir: str | Path) -> Path:
    """Download a video from a YouTube link (720p or lower) into workdir and return its path."""
    import yt_dlp

    host = (urlparse(url.strip()).hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host not in VIDEO_HOSTS:
        raise ValueError("Paste a YouTube link (youtube.com or youtu.be).")

    def too_long(info, *, incomplete=False):
        if (info.get("duration") or 0) > MAX_SOURCE_SECONDS:
            return f"That video is longer than {MAX_SOURCE_SECONDS // 60} minutes. Pick a shorter clip."
        return None

    options = {
        # YouTube serves video and sound separately; fetch both (720p or lower) and merge them.
        "format": "bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(Path(workdir) / "source.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True, "overwrites": True,
        "match_filter": too_long, "ffmpeg_location": FFMPEG,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url.strip(), download=True)
        if not info or not info.get("requested_downloads"):
            raise ValueError(too_long(info or {}) or "That video couldn't be downloaded.")
        return Path(info["requested_downloads"][0]["filepath"])


def trim(source: str | Path, start: float, seconds: float, workdir: str | Path) -> bytes:
    """Cut [start, start + seconds] out of a video and return it as a web-friendly MP4 (max 720p)."""
    out = Path(workdir) / "clip.mp4"
    result = _run(["-y", "-ss", f"{max(0.0, start):.2f}", "-t", f"{min(seconds, MAX_CLIP_SECONDS):.2f}",
                   "-i", str(source), "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(out)])
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("That video couldn't be trimmed. Try a different file or link.")
    return out.read_bytes()


def _named(data: bytes, name: str) -> io.BytesIO:
    f = io.BytesIO(data)
    f.name = name  # tells Replicate the content type
    return f


def swap_face(clip: bytes, face_jpeg: bytes, model: str | None = None) -> bytes:
    """Swap the face from a photo into a video clip on Replicate; returns MP4 bytes."""
    import replicate

    model = model or os.getenv("FACECLIPS_MODEL", DEFAULT_SWAP_MODEL)
    names = SWAP_MODELS.get(model.split(":")[0])
    if not names:
        raise ValueError(f"Unknown face-swap model '{model}'. Use one of: {', '.join(SWAP_MODELS)}.")
    if ":" not in model:  # community models must be run by version; use the latest
        model = f"{model}:{replicate.models.get(model).latest_version.id}"
    out = replicate.run(model, input={names["video"]: _named(clip, "clip.mp4"),
                                      names["face"]: _named(face_jpeg, "face.jpg")})
    if isinstance(out, list):
        out = out[0]
    if hasattr(out, "read"):
        return out.read()
    with urllib.request.urlopen(str(out)) as resp:  # older clients return a URL
        return resp.read()


def new_workdir() -> str:
    return tempfile.mkdtemp(prefix="faceclips_")
