"""Parody Clips - swap a person in a video clip for a different character, for comedic parodies.

Upload a clip (or paste a YouTube link when running locally) and a photo of a character, pick who in
the clip to replace, and only that person is swapped: everyone else in the scene stays as they are.
For scenes with several people, swap one, then use the result as the source and swap the next.

Swap options (all on Replicate, shared with Face Clips via restyle/video.py):
  - Whole person, chosen from the people found in the clip: Wan 2.2 Animate Replace
    (wan-video/wan-2.2-animate-replace). Only the chosen person's part of the frame is sent to the
    model and the result is blended back, so other people are left untouched.
  - Whole person, described in words ("the man in the red jacket"): Kling 3.0 Omni video edit.
  - Head only (face, hair and head shape): Kling 3.0 Omni.
  - Face only: roop face swap.

Setup (local):
    pip install -r requirements.txt
    Create .env next to this file with:
        REPLICATE_API_TOKEN=r8_...          # from replicate.com/account/api-tokens
        ALLOWED_USERS=alice,bob             # who can sign in

Run:
    streamlit run parodyclips.py

Streamlit Community Cloud: deploy with parodyclips.py as the main file and put the same values in
the app's Secrets (Settings > Secrets):
    REPLICATE_API_TOKEN = "r8_..."
    ALLOWED_USERS = ["alice", "bob"]
Each user gets SWAPS_PER_USER swaps (override with a PARODYCLIPS_SWAPS secret). Counts are kept
in .parodyclips_usage.json, which resets when the app is rebooted or redeployed. YouTube links
only work locally: YouTube blocks downloads from hosted sites like Streamlit Cloud.

ffmpeg comes bundled with imageio-ffmpeg (a system ffmpeg on PATH is used if there is one).
Clips and results are kept in ./outputs, one folder per session, and cleared after a day.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
import io
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import streamlit as st
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

load_dotenv()

from restyle import people, video  # noqa: E402
from restyle.faces import _bgr, _detect as detect_faces  # noqa: E402  (YuNet face detector)
from restyle.usage import Tracker  # noqa: E402

# ---------------------------------------------------------------- options and limits
MODES = {
    "main": "Whole person: pick who in the clip",
    "described": "Whole person: I'll describe who",
    "head": "Head only (face and hair)",
    "face": "Face only",
}
RESOLUTIONS = ["720", "480"]  # Wan 2.2 Animate's allowed values
CUTOUT_COST = 0.02  # background removal on the character photo (whole person and head options)
MIN_SECONDS, MAX_SECONDS = float(video.MIN_CLIP_SECONDS), float(video.MAX_CLIP_SECONDS)
MAX_MB = 200  # also capped by Streamlit's server.maxUploadSize (see max_upload_mb)
RUN_TIMEOUT_S = 45 * 60  # stop waiting after this long (2-minute clips in parts can take 20+ minutes)
OUT_DIR = Path(__file__).resolve().parent / "outputs"
KEEP_FILES_HOURS = 24  # session folders older than this are deleted
SWAPS_PER_USER = 5


# ---------------------------------------------------------------- secrets, sign-in, usage
def _secret(name: str):
    """A value from Streamlit secrets (Streamlit Cloud), or None."""
    try:
        return st.secrets.get(name)
    except Exception:  # no secrets file locally
        return None


def load_secrets() -> None:
    """Copy REPLICATE_API_TOKEN from Streamlit secrets into the environment, where replicate reads it."""
    if not os.getenv("REPLICATE_API_TOKEN") and _secret("REPLICATE_API_TOKEN"):
        os.environ["REPLICATE_API_TOKEN"] = str(_secret("REPLICATE_API_TOKEN"))


def allowed_users() -> list[str]:
    """ALLOWED_USERS from Streamlit secrets (a name or a list of names), or comma-separated in .env."""
    raw = _secret("ALLOWED_USERS") or os.getenv("ALLOWED_USERS", "")
    names = raw.split(",") if isinstance(raw, str) else list(raw)
    return [n.strip().lower() for n in names if str(n).strip()]


def _log_in() -> None:
    S = st.session_state
    typed = S.get("login_name", "").strip().lower()
    if any(hmac.compare_digest(typed, u) for u in allowed_users()):
        S.user = typed
    else:
        S.login_error = True


def _log_out() -> None:
    st.session_state.clear()


def require_login() -> str:
    """Show the sign-in form and stop the page until a listed user signs in; returns the username."""
    S = st.session_state
    if S.get("user"):
        return S.user
    st.title("Parody Clips")
    if not allowed_users():
        st.error("No users are set up. Add ALLOWED_USERS to the app's Streamlit secrets (or .env locally).")
        st.stop()
    with st.form("login"):
        st.text_input("Username", key="login_name")
        st.form_submit_button("Sign in", type="primary", on_click=_log_in)
    if S.pop("login_error", False):
        st.error("That username isn't recognised.")
    st.stop()


def _swap_limit() -> int:
    try:
        return int(_secret("PARODYCLIPS_SWAPS") or os.getenv("PARODYCLIPS_SWAPS") or SWAPS_PER_USER)
    except ValueError:
        return SWAPS_PER_USER


usage = Tracker(
    limits={"swaps": _swap_limit()},
    labels={"swaps": "Swaps"},
    file=os.getenv("PARODYCLIPS_USAGE_FILE", Path(__file__).resolve().parent / ".parodyclips_usage.json"),
)


def max_upload_mb() -> int:
    """The smaller of our limit and Streamlit's upload cap (server.maxUploadSize in .streamlit/config.toml)."""
    try:
        return min(MAX_MB, int(st.get_option("server.maxUploadSize")))
    except Exception:
        return MAX_MB


def session_dir() -> Path:
    """This session's own folder under outputs/, so users on a shared server never mix up files.
    Also deletes other session folders older than KEEP_FILES_HOURS."""
    S = st.session_state
    if not S.get("workdir") or not Path(S.workdir).exists():
        OUT_DIR.mkdir(exist_ok=True)
        cutoff = time.time() - KEEP_FILES_HOURS * 3600
        for old in OUT_DIR.iterdir():
            if old.is_dir() and old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        S.workdir = str(OUT_DIR / f"{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}")
        Path(S.workdir).mkdir(parents=True)
    return Path(S.workdir)


# ---------------------------------------------------------------- ffmpeg and video info
def ffmpeg_exe() -> str:
    """System ffmpeg if installed, else the one bundled with imageio-ffmpeg."""
    return shutil.which("ffmpeg") or video.FFMPEG


def _ffmpeg(*args: str) -> None:
    """Run ffmpeg quietly; raise with its last error lines if it fails."""
    done = subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", *args], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError("ffmpeg failed: " + " | ".join(done.stderr.strip().splitlines()[-3:]))


@dataclass
class ClipInfo:
    duration: float
    width: int  # as displayed (phone-video rotation applied)
    height: int
    has_audio: bool
    size_mb: float

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    def aspect_label(self) -> str:
        """e.g. '16:9 (1.78)'."""
        common = {"16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "1:1": 1.0, "21:9": 21 / 9,
                  "4:5": 4 / 5}
        name = min(common, key=lambda k: abs(common[k] - self.aspect))
        return f"{name} ({self.aspect:.2f})" if abs(common[name] - self.aspect) < 0.03 else f"{self.aspect:.2f}:1"


def probe_video(path: Path) -> ClipInfo:
    """Duration, displayed size and audio presence, read from ffmpeg's stream summary."""
    err = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    size = re.search(r"Stream #.*?Video:.*?(\d{2,5})x(\d{2,5})", err)
    if not dur or not size:
        raise ValueError("That file doesn't look like a video ffmpeg can read.")
    h_, m_, s_ = dur.groups()
    w, h = int(size.group(1)), int(size.group(2))
    rot = re.search(r"rotation of (-?[\d.]+) degrees|rotate\s*:\s*(-?\d+)", err)
    if rot and round(abs(float(rot.group(1) or rot.group(2)))) % 180 == 90:
        w, h = h, w  # portrait phone video stored sideways
    return ClipInfo(duration=int(h_) * 3600 + int(m_) * 60 + float(s_), width=w, height=h,
                    has_audio=bool(re.search(r"Stream #.*?Audio:", err)),
                    size_mb=path.stat().st_size / 1e6)


def validate_clip(info: ClipInfo, start: float = 0.0, end: float | None = None,
                  check_size: bool = True) -> list[str]:
    """Problems that would stop the part start..end being used; empty if it's fine."""
    length = (end if end is not None else info.duration) - start
    problems = []
    if length < MIN_SECONDS:
        problems.append(f"The part is {length:.1f}s long; it needs to be at least {MIN_SECONDS:.0f}s.")
    if length > MAX_SECONDS + 0.05:
        problems.append(f"The part is {length:.1f}s long; the limit is {MAX_SECONDS:.0f}s.")
    if check_size and info.size_mb > max_upload_mb():
        problems.append(f"The file is {info.size_mb:.0f} MB; the limit is {max_upload_mb()} MB.")
    if min(info.width, info.height) < 64:
        problems.append("The video is too small to work with.")
    return problems


# ---------------------------------------------------------------- video processing
def trim_clip(src: Path, dst: Path, start: float, end: float) -> Path:
    """Cut start..end out of src into an mp4 the models accept: H.264, square pixels, even size,
    shorter side at most 720px, rotation applied, AAC audio."""
    fit = ("scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1,"
           "scale='if(gt(iw,ih),-2,min(720,iw))':'if(gt(iw,ih),min(720,ih),-2)',setsar=1")
    _ffmpeg("-ss", f"{start:.3f}", "-i", str(src), "-t", f"{max(end - start, 0.1):.3f}", "-vf", fit,
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst))
    return dst


def restore_audio(video_path: Path, audio_source: Path, dst: Path) -> Path:
    """If the result came back silent, put the source clip's audio back on. Returns the path to use."""
    if probe_video(video_path).has_audio or not probe_video(audio_source).has_audio:
        return video_path
    _ffmpeg("-i", str(video_path), "-i", str(audio_source), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(dst))
    return dst


def _label_png(height: int, dst: Path) -> Path:
    """The 'AI PARODY' badge as a transparent PNG, sized for a video of this height."""
    size = max(14, round(height * 0.035))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    text = "AI PARODY"
    box = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox((0, 0), text, font=font)
    pad = max(4, size // 3)
    img = Image.new("RGBA", (box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=pad, fill=(0, 0, 0, 150))
    draw.text((pad - box[0], pad - box[1]), text, font=font, fill=(255, 255, 255, 235))
    img.save(dst)
    return dst


def add_watermark(src: Path, dst: Path) -> Path:
    """Burn a small 'AI PARODY' label into the bottom-right corner."""
    info = probe_video(src)
    badge = _label_png(info.height, dst.with_suffix(".label.png"))
    margin = max(8, info.height // 40)
    _ffmpeg("-i", str(src), "-i", str(badge),
            "-filter_complex", f"[0:v][1:v]overlay=W-w-{margin}:H-h-{margin}",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(dst))
    return dst


# ---------------------------------------------------------------- the character photo
def _prepare_photo(data: bytes) -> Image.Image:
    """Upright, RGB, at most 1568px."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((1568, 1568))
    return img


def person_crops(photo: Image.Image, face) -> tuple[Image.Image, Image.Image, Image.Image]:
    """(whole-person crop, face crop, head crop) for one detected face. The body crop takes the area
    around and below the face, so one person can be lifted out of a group photo; the head crop is
    generous so the hair and whole head shape are included."""
    x, y, w, h = (int(v) for v in face[:4])
    body = photo.crop((max(0, x - int(w * 1.7)), max(0, y - int(h * 0.8)),
                       min(photo.width, x + w + int(w * 1.7)), min(photo.height, y + h + int(h * 7))))
    pad = int(max(w, h) * 0.6)
    face_only = photo.crop((max(0, x - pad), max(0, y - pad), min(photo.width, x + w + pad),
                            min(photo.height, y + h + pad)))
    hp = int(max(w, h) * 0.9)
    head = photo.crop((max(0, x - hp), max(0, y - int(hp * 1.2)), min(photo.width, x + w + hp),
                       min(photo.height, y + h + int(hp * 1.3))))
    return body, face_only, head


def as_jpeg(img: Image.Image, min_side: int = 0) -> bytes:
    """JPEG bytes, enlarged first if the shorter side is under min_side (some models reject small images)."""
    if min_side and min(img.size) < min_side:
        scale = min_side / min(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ---------------------------------------------------------------- Replicate
class SwapError(Exception):
    """A failure with a message that's fine to show in the UI."""


def _friendly(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status_code", None)
    if status in (401, 403) or "unauthenticated" in low or "invalid token" in low:
        return "Replicate didn't accept the API token. Check REPLICATE_API_TOKEN in the app's secrets."
    if status == 402 or "billing" in low or "insufficient credit" in low:
        return "Replicate says the account needs credit or billing set up."
    if status == 429 or "rate limit" in low:
        return "Replicate is rate-limiting requests. Wait a minute and try again."
    if any(w in low for w in ("nsfw", "safety", "sensitive", "flagged", "content policy")):
        return "The model's safety filter rejected this clip or photo. Try a different photo or shot."
    if "zero-size array" in low or "no identity" in low or "no person" in low or "no face" in low:
        return ("The model couldn't find the person in every frame. Cut a part that is one continuous shot "
                "(no camera cuts) where the person's face and upper body stay in view from the first frame, "
                "and use a clear photo of one person.")
    if "timed out" in low or "timeout" in low:
        return "Replicate took too long to respond. Try again, or try a shorter clip or 480p."
    return f"The swap didn't work: {text[:400]}"


def run_in_background(job: Callable[[], bytes], on_tick: Callable[[float], None] | None = None) -> bytes:
    """Run a slow Replicate job in a thread, calling on_tick(elapsed seconds) about once a second so the
    page can show progress. Errors come back as SwapError with a friendly message."""
    started = time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(job)
    try:
        # wait() rather than result(timeout=...): a real network timeout raised by the job is also a
        # TimeoutError, and must not be mistaken for "still running".
        while not concurrent.futures.wait([future], timeout=1).done:
            elapsed = time.monotonic() - started
            if elapsed > RUN_TIMEOUT_S:
                raise SwapError(f"Stopped waiting after {RUN_TIMEOUT_S // 60} minutes. The job may still "
                                "finish on Replicate (check replicate.com/predictions); it won't be shown here.")
            if on_tick:
                on_tick(elapsed)
        return future.result()
    except SwapError:
        raise
    except Exception as e:
        raise SwapError(_friendly(e)) from e
    finally:
        pool.shutdown(wait=False)


def run_swap(mode: str, clip: bytes, person: dict, box: tuple[int, int, int, int] | None,
             resolution: str = "720", who: str = "", extra: dict | None = None) -> bytes:
    """Do one swap and return the MP4 bytes (with the clip's sound). `person` holds the photo crops as
    JPEG bytes ('body', 'face', 'head') and 'animal'. With `box`, only that part of the frame is
    changed, so everyone else in the clip stays untouched."""
    if mode == "main":
        return video.replace_person(clip, person["body"], resolution, box, extra)
    if mode == "described":
        return video.replace_described_person(clip, person["body"], who, person["animal"])
    if mode == "head":
        return video.replace_head(clip, video.cut_out_person(person["head"]), box, person["animal"])
    return video.swap_face(clip, person["face"])


def estimate_cost(mode: str, seconds: float, resolution: str) -> float:
    """Approximate US$ for one swap, from Replicate's per-second prices."""
    costs = video.estimate_costs(seconds)
    if mode == "main":
        return costs[f"main{resolution}"] + CUTOUT_COST
    if mode == "head":
        return costs["head"] + CUTOUT_COST
    return costs[mode]


# ---------------------------------------------------------------- UI helpers
def _save_upload(data: bytes, suffix: str) -> Path:
    """Store uploaded bytes in this session's folder, named by content so re-runs don't duplicate them."""
    path = session_dir() / f"upload-{hashlib.sha1(data).hexdigest()[:12]}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return path


def _fmt_info(info: ClipInfo) -> str:
    sound = "with sound" if info.has_audio else "no sound"
    return (f"{info.duration:.1f}s · {info.width}×{info.height} · {info.aspect_label()} · "
            f"{info.size_mb:.1f} MB · {sound}")


def _set_source(path: Path, origin: str) -> None:
    """Make `path` the video to cut from ('upload', 'link' or 'result'), clearing the old cut."""
    S = st.session_state
    S.source, S.source_info, S.source_from = str(path), probe_video(path), origin
    S.clip, S.clip_len, S.clip_people = None, 0.0, None
    S.n += 1  # fresh widgets for the new source


def _start_over() -> None:
    S = st.session_state
    S.source = S.source_info = S.source_from = S.clip = S.clip_people = S.photo = S.photo_sig = None
    S.clip_len = 0.0
    S.n += 1


def _chain_from(item: dict) -> None:
    """Use a result as the source for the next swap (the next person in the scene)."""
    S = st.session_state
    _set_source(Path(item["clean"]), "result")
    S.photo = S.photo_sig = None


def _show_history() -> None:
    S = st.session_state
    if not S.history:
        return
    st.divider()
    latest = S.history[-1]
    st.subheader(f"Result: {latest['label']}")
    before, after = st.columns(2)
    before.video(latest["before"])
    before.caption("Before")
    after.video(latest["final"])
    after.caption(f"After (AI-generated): {latest['mode']}")
    after.download_button("Download", Path(latest["final"]).read_bytes(), mime="video/mp4",
                          file_name=Path(latest["final"]).name, type="primary", key=f"dl_{latest['step']}")
    st.button("Swap another person in this result", on_click=_chain_from, args=(latest,),
              help="For scenes with several people: this result becomes the video, and you pick the next "
                   "person to replace.")
    if len(S.history) > 1:
        st.subheader("History")
        cols = st.columns(3)
        for i, item in enumerate(reversed(S.history[:-1])):
            with cols[i % 3]:
                st.caption(item["label"])
                st.video(item["final"])
                st.download_button("Download", Path(item["final"]).read_bytes(), mime="video/mp4",
                                   file_name=Path(item["final"]).name, key=f"dl_{item['step']}")


# ---------------------------------------------------------------- UI
def render_ui(user: str) -> None:
    """Draw the page for a signed-in user and handle a swap."""
    S = st.session_state
    for k, v in {"source": None, "source_info": None, "source_from": None, "clip": None, "clip_len": 0.0,
                 "clip_people": None, "photo": None, "photo_sig": None, "history": [], "n": 0}.items():
        S.setdefault(k, v)
    work = session_dir()
    swaps_left = usage.remaining(user, "swaps")

    st.title("Parody Clips 🎬")
    st.caption("Swap a person in a clip for a different character. They keep the same moves, expressions "
               "and voice, and everyone else in the scene stays as they are.")
    st.info("For parody only. Every download is labelled **AI PARODY** by default: don't pass a clip off as "
            "real, and only use footage you have the rights to.", icon="🎭")

    token = os.getenv("REPLICATE_API_TOKEN")
    if not token:
        st.error("**REPLICATE_API_TOKEN is missing.** On Streamlit Cloud, add it in the app's Settings > "
                 "Secrets as `REPLICATE_API_TOKEN = \"r8_...\"`. Locally, put `REPLICATE_API_TOKEN=r8_...` in a "
                 "`.env` file next to parodyclips.py. Get a token at replicate.com/account/api-tokens, then "
                 "restart the app.")

    # ---------------- sidebar
    with st.sidebar:
        st.header("Options")
        go_fast = st.toggle("Go fast", value=False, help="Whole person (pick who) only: extra optimisations "
                                                         "for faster generation; can slightly lower quality.")
        fixed_seed = st.toggle("Fixed seed", value=False, help="Whole person (pick who) only: use the same "
                                                               "seed to repeat a result.")
        seed = st.number_input("Seed", min_value=0, max_value=2**31 - 1, value=42, step=1,
                               disabled=not fixed_seed)
        watermark = st.toggle("Add 'AI PARODY' label to downloads", value=True)
        st.divider()
        st.subheader("Cost (approx)")
        cost_box = st.empty()
        st.caption("Estimated from Replicate's per-second prices: Wan 2.2 Animate $0.05/s at 720p and $0.02/s "
                   "at 480p, Kling 3.0 Omni $0.168/s, plus about $0.02 to cut the character out of the photo.")
        if S.source:
            st.button("Start over with a new video", on_click=_start_over, use_container_width=True)
        if S.history and st.button("Clear history", use_container_width=True):
            S.history = []
            st.rerun()
        st.divider()
        st.subheader("Your usage")
        used, limit = usage.get(user)["swaps"], usage.limits["swaps"]
        st.progress(min(used, limit) / limit, text=f"Swaps: {used} of {limit}")
        st.caption(f"Signed in as **{user}**")
        st.button("Sign out", on_click=_log_out, use_container_width=True)
    cost_box.metric("This swap", "—")

    # ---------------- 1. load a video
    if not S.source:
        st.subheader("1. Load a video")
        file_tab, link_tab = st.tabs(["Upload a video file", "Paste a YouTube link"])
        with file_tab:
            up = st.file_uploader("Video file", type=["mp4", "mov", "m4v", "webm"], key=f"vid_{S.n}",
                                  help=f"Up to {max_upload_mb()} MB. You'll pick a {MIN_SECONDS:.0f}-"
                                       f"{MAX_SECONDS:.0f} second part next.")
            if up:
                try:
                    _set_source(_save_upload(up.getvalue(), Path(up.name).suffix.lower() or ".mp4"), "upload")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        with link_tab:
            url = st.text_input("YouTube link", placeholder="https://www.youtube.com/watch?v=...", key=f"url_{S.n}")
            st.caption("Links only work when you run this app on your own computer. YouTube blocks hosted sites "
                       "like Streamlit Cloud, so there, use \"Upload a video file\" instead.")
            if st.button("Get video", type="primary", disabled=not url.strip()):
                try:
                    with st.spinner("Fetching the video..."):
                        _set_source(video.fetch_from_link(url, work), "link")
                    st.rerun()
                except Exception as e:
                    st.error(f"Couldn't get that video: {str(e)[:300]}")
        _show_history()
        return

    info: ClipInfo = S.source_info
    clip_col, photo_col = st.columns(2, gap="large")

    # ---------------- 2. pick the part
    with clip_col:
        st.subheader("2. Pick the part to use")
        if S.source_from == "result":
            st.caption("Using your last result as the video. Pick the next person to replace below.")
        st.caption(_fmt_info(info))
        whole = info.duration if S.source_from == "result" else min(info.duration, 10.0)
        start, end = st.slider("Keep from / to (seconds)", 0.0, round(info.duration, 1),
                               (0.0, round(min(whole, MAX_SECONDS), 1)), step=0.1, key=f"trim_{S.n}")
        st.caption(f"{end - start:.1f}s selected ({MIN_SECONDS:.0f} to {MAX_SECONDS:.0f} seconds). Longer parts "
                   "cost more and take longer. One continuous shot, with the person in view from the start, "
                   "works best.")
        problems = validate_clip(info, start, end, check_size=S.source_from == "upload")
        for p in problems:
            st.error(p)
        est = video.estimate_costs(end - start)
        st.markdown(
            f"**Estimated cost for {end - start:.0f} seconds** (US$, charged by Replicate)\n\n"
            "| Option | Cost |\n|---|---|\n"
            f"| Whole person: pick who | ${est['main720'] + CUTOUT_COST:.2f} at 720p, "
            f"${est['main480'] + CUTOUT_COST:.2f} at 480p |\n"
            f"| Whole person: describe who | ${est['described']:.2f} |\n"
            f"| Head only | ${est['head'] + CUTOUT_COST:.2f} |\n"
            f"| Face only | about ${est['face']:.2f} |"
        )
        if st.button("Cut this part", type="primary" if S.clip is None else "secondary", disabled=bool(problems)):
            try:
                with st.spinner("Cutting the clip..."):
                    clip = trim_clip(Path(S.source), work / f"clip-{time.strftime('%H%M%S')}.mp4", start, end)
                S.clip, S.clip_len, S.clip_people = str(clip), probe_video(clip).duration, None
            except Exception as e:
                st.error(f"That part couldn't be cut: {e}")
        if S.clip:
            st.video(S.clip)
            st.caption(f"Cut: {S.clip_len:.1f}s")

    # ---------------- 3. the character photo
    person = None
    with photo_col:
        st.subheader("3. Photo of the character to put in")
        label = st.text_input("Character name (for the history)", placeholder="e.g. Roger", key=f"name_{S.n}")
        pic = st.file_uploader("Photo", type=["jpg", "jpeg", "png", "webp"], key=f"photo_{S.n}",
                               help="A person or an animal. A clear, well-lit photo showing the head and body "
                                    "works best. In a group photo you can pick who.")
        if pic and (pic.name, pic.size) != S.photo_sig:
            try:
                S.photo, S.photo_sig = _prepare_photo(pic.getvalue()), (pic.name, pic.size)
            except Exception:
                st.error("That photo couldn't be opened. Try a JPEG or PNG.")
        if S.photo is not None:
            found = sorted(detect_faces(_bgr(S.photo)), key=lambda f: f[0])[:5]
            subject = st.radio("What's in the photo?", ["person", "animal"], index=0 if found else 1,
                               horizontal=True, format_func={"person": "A person", "animal": "An animal"}.get,
                               key=f"subject_{S.n}_{S.photo_sig}")
            if subject == "animal":
                st.image(S.photo, width=220)
                st.caption("The whole photo is used. One animal on its own, with its head and body in view, "
                           "works best.")
                whole_photo = as_jpeg(S.photo, min_side=320)
                person = {"body": whole_photo, "head": whole_photo, "face": None, "animal": True}
            elif not found:
                st.image(S.photo, width=220)
                st.warning("No person found in that photo. Try a clearer photo where the face is visible, "
                           "or choose \"An animal\" above.")
            else:
                crops = [person_crops(S.photo, f) for f in found]  # left to right
                pick = 0
                if len(crops) > 1:
                    st.caption(f"Found {len(crops)} people. Choose who to put in the clip.")
                    for col, (i, (body, _, _)) in zip(st.columns(len(crops)), enumerate(crops)):
                        col.image(body, caption=f"Person {i + 1}")
                    pick = st.radio("Person", range(len(crops)), format_func=lambda i: f"Person {i + 1}",
                                    horizontal=True, label_visibility="collapsed", key=f"pick_{S.n}")
                else:
                    st.image(crops[0][0], width=220)
                person = {"body": as_jpeg(crops[pick][0], min_side=320), "face": as_jpeg(crops[pick][1]),
                          "head": as_jpeg(crops[pick][2], min_side=320), "animal": False}

    # ---------------- 4. who to replace, and swap
    st.divider()
    st.subheader("4. Who to replace")
    mode = st.radio("What to swap", list(MODES), format_func=MODES.get, key="mode")
    who, resolution, problem, box = "", "720", None, None
    lo, hi = video.DESCRIBED_SECONDS
    if mode == "main":
        st.caption("Replaces the whole person, including body and clothes, with the character from your photo. "
                   "They keep the same movements, expressions and mouth movements, and the original sound stays.")
        if person and person["animal"]:
            st.info("This option is built for people. With an animal it may give it a human-shaped body or an "
                    "odd face. If that happens, try \"I'll describe who\", which is told to keep it an animal.")
    elif mode == "described":
        st.caption("Describe the one person to replace; only they are changed. Good when people cross paths "
                   "or overlap. Uses Kling 3.0 Omni.")
        who = st.text_input("Who should be replaced?", placeholder="e.g. the man in the red jacket on the left")
        if S.clip and S.clip_len < lo:
            problem = f"For this option, cut a part of at least {lo} seconds (yours is {S.clip_len:.0f})."
        elif not who.strip():
            problem = "Describe who to replace."
    elif mode == "head":
        st.caption("Replaces the head (face, hair and head shape). The actor's body, clothes and movements stay, "
                   "and the original sound stays. Uses Kling 3.0 Omni.")
        if S.clip and S.clip_len < lo:
            problem = f"For this option, cut a part of at least {lo} seconds (yours is {S.clip_len:.0f})."
    else:
        st.caption("Keeps the actor's body, hair and clothes and replaces only the face. One face is replaced, "
                   "usually the most prominent one, so other people may be affected in group shots.")
        if person and person["animal"]:
            problem = "Face only needs a person's face. For an animal, use a whole-person option or Head only."

    if mode in ("main", "head") and S.clip:
        if S.clip_people is None:
            with st.spinner("Looking for people in the clip..."):
                S.clip_people = people.find_people(Path(S.clip).read_bytes())
        frame, found_people = S.clip_people
        if len(found_people) > 1:
            target = st.radio("Who in the clip should be replaced?", range(len(found_people)), horizontal=True,
                              format_func=lambda i: f"Person {i + 1}", key=f"target_{S.clip}")
            st.image(people.draw_people(frame, found_people, target), width=520,
                     caption="Only the highlighted area is changed. Everyone else stays as they are.")
            box = found_people[target]["box"]
            seen = found_people[target]["seen"]
            if box is None:
                st.warning("These people cross over each other in this part, so one can't be changed without "
                           "the other: everyone in shot could be replaced. Cut a part where they stay apart, or "
                           "use \"I'll describe who\".")
            elif seen < 0.85:
                st.warning(f"This person's face is only visible for about {seen:.0%} of the part. The swap can "
                           "fail or look wrong when they turn away, leave the shot or the camera cuts.")
        elif len(found_people) == 1:
            st.image(people.draw_people(frame, found_people, 0), width=520, caption="Person found in the clip")
            if found_people[0]["seen"] < 0.85:
                st.warning(f"The person's face is only visible for about {found_people[0]['seen']:.0%} of the "
                           "part. The swap can fail or look wrong when they turn away, leave the shot or the "
                           "camera cuts. A part where they stay in view works best.")
        else:
            if frame is not None:
                st.image(frame, width=520, caption="First frame")
            st.warning("No faces found at the start of this part, so the whole picture is sent and anyone in it "
                       "could be replaced. Start the part where the person's face is visible to choose who.")
    if mode == "main":
        resolution = st.radio("Quality", RESOLUTIONS, horizontal=True,
                              format_func={"720": "Sharper (720p)", "480": "Cheaper and faster (480p)"}.get)
    if S.clip and mode in ("described", "head") and video.parts_needed(S.clip_len) > 1:
        st.caption(f"Done in {video.parts_needed(S.clip_len)} parts of up to {hi} seconds and joined. The person "
                   "can look slightly different from part to part.")

    cost = estimate_cost(mode, S.clip_len, resolution) if S.clip else 0.0
    if S.clip:
        cost_box.metric("This swap", f"~${cost:.2f}", help=f"{MODES[mode]}, {S.clip_len:.0f} seconds.")

    agreed = st.checkbox("I have the rights to this footage, and I'll make clear it's an AI parody if I show "
                         "it to anyone.", key="agreed")
    ready = bool(token and S.clip and person and agreed and swaps_left and not problem)
    if st.button(f"Swap (about US${cost:.2f})" if S.clip else "Swap", type="primary", disabled=not ready,
                 use_container_width=True):
        step = len(S.history) + 1
        name = label.strip() or f"Character {step}"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        timer = st.empty()
        extra = {"go_fast": go_fast, **({"seed": int(seed)} if fixed_seed else {})}
        clip_path = Path(S.clip)
        try:
            with st.spinner("Swapping. This takes a few minutes, and 20 or more for long clips. Keep this page "
                            "open..."):
                out = run_in_background(
                    lambda: run_swap(mode, clip_path.read_bytes(), person, box, resolution, who, extra),
                    on_tick=lambda s: timer.caption(f"Elapsed: {int(s) // 60}:{int(s) % 60:02d}"))
                usage.record(user, "swaps")  # counted once Replicate has made (and charged for) the video
                timer.caption("Finishing up...")
                raw = work / f"step{step}-{stamp}-raw.mp4"
                raw.write_bytes(out)
                clean = restore_audio(raw, clip_path, work / f"step{step}-{stamp}-audio.mp4")
                final = add_watermark(clean, work / f"step{step}-{stamp}-parody.mp4") if watermark else clean
            S.history.append({"step": step, "label": f"Step {step} – {name}", "mode": MODES[mode],
                              "clean": str(clean), "final": str(final), "before": str(clip_path)})
            timer.empty()
            st.rerun()
        except SwapError as e:
            timer.empty()
            st.error(str(e))
        except Exception as e:  # ffmpeg or file problems
            timer.empty()
            st.error(f"Something went wrong preparing the video: {e}")
    if not S.clip:
        st.caption("Cut a part first (step 2).")
    elif not person:
        st.caption("Upload a photo of the character (step 3).")
    elif problem:
        st.caption(problem)
    elif not swaps_left:
        st.warning(f"You've used all {usage.limits['swaps']} swaps.")
    else:
        st.caption(f"{swaps_left} swaps left.")

    _show_history()


def main() -> None:
    st.set_page_config(page_title="Parody Clips", page_icon="🎬", layout="wide")
    load_secrets()
    render_ui(require_login())


if __name__ == "__main__":
    main()
