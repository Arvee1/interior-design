"""Parody Clips - full-body character replacement in short video clips, for comedic parodies.

Uses Replicate's hosted Wan 2.2 Animate model in replace mode (wan-video/wan-2.2-animate-replace):
upload a clip and a photo of a character, and the person in the clip is replaced by that character,
keeping the original motion, camera and scene. For scenes with several people, chain swaps: swap
one person, then use that result as the source for the next.

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
in .parodyclips_usage.json, which resets when the app is rebooted or redeployed.

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

import requests
import streamlit as st
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

load_dotenv()

from restyle.usage import Tracker  # noqa: E402

# ---------------------------------------------------------------- model and limits
MODEL = "wan-video/wan-2.2-animate-replace"
# From the model's input schema (latest version 33ec6b98..., checked 2026-11-08).
RESOLUTIONS = ["720", "480"]  # enum; default "720"
# From the model page, per second of output video. Prices can change: shown as approx.
PRICE_PER_SECOND = {"720": 0.05, "480": 0.02}
# The schema gives no duration or size limits, so these are sensible defaults.
MIN_SECONDS, MAX_SECONDS = 2.0, 30.0
MAX_MB = 200  # also capped by Streamlit's server.maxUploadSize (see max_upload_mb)
RUN_TIMEOUT_S = 30 * 60  # stop waiting after this long
OUT_DIR = Path(__file__).resolve().parent / "outputs"
KEEP_FILES_HOURS = 24  # session folders older than this are deleted
SWAPS_PER_USER = 5  # 5 swaps of up to 30s at 720p is at most about $7.50 per user


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


def ffmpeg_exe() -> str:
    """System ffmpeg if installed, else the one bundled with imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _ffmpeg(*args: str) -> None:
    """Run ffmpeg quietly; raise with its last error lines if it fails."""
    done = subprocess.run([ffmpeg_exe(), "-hide_banner", "-y", *args], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError("ffmpeg failed: " + " | ".join(done.stderr.strip().splitlines()[-3:]))


# ---------------------------------------------------------------- video info and checks
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


def validate_clip(info: ClipInfo, start: float = 0.0, end: float | None = None) -> list[str]:
    """Problems that would stop the clip (start..end) being sent; empty if it's fine."""
    length = (end if end is not None else info.duration) - start
    problems = []
    if length < MIN_SECONDS:
        problems.append(f"The clip is {length:.1f}s long; it needs to be at least {MIN_SECONDS:.0f}s.")
    if length > MAX_SECONDS:
        problems.append(f"The clip is {length:.1f}s long; the limit is {MAX_SECONDS:.0f}s. Use the trim tool "
                        "to cut out one shot.")
    if info.size_mb > max_upload_mb():
        problems.append(f"The file is {info.size_mb:.0f} MB; the limit is {max_upload_mb()} MB.")
    if min(info.width, info.height) < 64:
        problems.append("The video is too small to work with.")
    return problems


def aspect_warning(photo: Image.Image, info: ClipInfo) -> str | None:
    """A warning if the character photo's shape is very different from the video's."""
    photo_ar = photo.width / photo.height
    ratio = max(photo_ar, info.aspect) / max(min(photo_ar, info.aspect), 1e-6)
    if ratio > 1.8:
        return (f"The photo's shape ({photo_ar:.2f}) is very different from the video's "
                f"({info.aspect:.2f}). It usually still works, but a photo framed like the person in the clip "
                "(e.g. full body for a full-body shot) gives better results.")
    return None


# ---------------------------------------------------------------- video processing
def trim_clip(src: Path, dst: Path, start: float, end: float) -> Path:
    """Cut start..end out of src into an mp4 the model accepts: H.264, square pixels, even size,
    rotation applied, AAC audio. Also used without trimming, to normalise .mov and phone clips."""
    _ffmpeg("-ss", f"{start:.3f}", "-i", str(src), "-t", f"{max(end - start, 0.1):.3f}",
            "-vf", "scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1",
            "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst))
    return dst


def restore_audio(video_path: Path, audio_source: Path, dst: Path) -> Path:
    """If the model dropped the sound, put the source clip's audio back on. Returns the path to use."""
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


# ---------------------------------------------------------------- Replicate
class SwapError(Exception):
    """A failure with a message that's fine to show in the UI."""


def _friendly(e: Exception) -> str:
    text = str(e)
    low = text.lower()
    status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status_code", None)
    if status in (401, 403) or "unauthenticated" in low or "invalid token" in low:
        return "Replicate didn't accept the API token. Check REPLICATE_API_TOKEN in your .env file."
    if status == 402 or "billing" in low or "insufficient credit" in low:
        return "Replicate says the account needs credit or billing set up."
    if status == 429 or "rate limit" in low:
        return "Replicate is rate-limiting requests. Wait a minute and try again."
    if any(w in low for w in ("nsfw", "safety", "sensitive", "flagged", "content policy")):
        return ("The model's safety filter rejected this clip or photo. Try a different photo or shot.")
    if "timed out" in low or "timeout" in low:
        return "Replicate took too long to respond. Try again, or try a shorter clip or 480p."
    return f"The swap didn't work: {text[:400]}"


def _save_output(output, dst: Path) -> Path:
    """Write the model's output (FileOutput, URL string, or a list of either) to dst."""
    if isinstance(output, (list, tuple)):
        if not output:
            raise SwapError("The model returned no video.")
        output = output[0]
    if hasattr(output, "read"):
        dst.write_bytes(output.read())
        return dst
    url = str(getattr(output, "url", output))
    if not url.startswith("http"):
        raise SwapError("The model didn't return a video link.")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dst


def run_swap(clip: Path, photo: Path, dst: Path, resolution: str, go_fast: bool, merge_audio: bool,
             seed: int | None, on_tick=None) -> Path:
    """Replace the person in `clip` with the character in `photo`; returns the saved mp4.
    `on_tick(seconds)` is called about once a second while waiting, for an elapsed-time display."""
    import replicate

    params = {"resolution": resolution, "go_fast": go_fast, "merge_audio": merge_audio}
    if seed is not None:
        params["seed"] = int(seed)

    def call():
        with open(clip, "rb") as v, open(photo, "rb") as p:
            return replicate.run(MODEL, input={"video": v, "character_image": p, **params})

    started = time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(call)
    try:
        # wait() rather than result(timeout=...): a real network timeout raised by the call is also a
        # TimeoutError, and must not be mistaken for "still running".
        while not concurrent.futures.wait([future], timeout=1).done:
            elapsed = time.monotonic() - started
            if elapsed > RUN_TIMEOUT_S:
                raise SwapError(f"Stopped waiting after {RUN_TIMEOUT_S // 60} minutes. The job may still "
                                "finish on Replicate (check replicate.com/predictions); it won't be shown here.")
            if on_tick:
                on_tick(elapsed)
        output = future.result()
    except SwapError:
        raise
    except Exception as e:
        raise SwapError(_friendly(e)) from e
    finally:
        pool.shutdown(wait=False)
    try:
        return _save_output(output, dst)
    except SwapError:
        raise
    except Exception as e:
        raise SwapError(f"The video was made but couldn't be downloaded: {e}") from e


# ---------------------------------------------------------------- helpers for the UI
def _save_upload(data: bytes, suffix: str) -> Path:
    """Store uploaded bytes in this session's folder, named by content so re-runs don't duplicate them."""
    path = session_dir() / f"upload-{hashlib.sha1(data).hexdigest()[:12]}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return path


def _prepare_photo(data: bytes) -> tuple[Path, Image.Image]:
    """Upright, RGB, at most 2048px, saved as JPEG for the model."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((2048, 2048))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return _save_upload(buf.getvalue(), ".jpg"), img


def _cost(seconds: float, resolution: str) -> str:
    return f"~${seconds * PRICE_PER_SECOND[resolution]:.2f}"


def _fmt_info(info: ClipInfo) -> str:
    sound = "with sound" if info.has_audio else "no sound"
    return (f"{info.duration:.1f}s · {info.width}×{info.height} · {info.aspect_label()} · "
            f"{info.size_mb:.1f} MB · {sound}")


# ---------------------------------------------------------------- UI
def render_ui(user: str) -> None:
    """Draw the page for a signed-in user and handle a Generate click."""
    S = st.session_state
    S.setdefault("history", [])  # dicts: step, label, clean (no badge), final, source
    work = session_dir()
    swaps_left = usage.remaining(user, "swaps")

    st.title("Parody Clips 🎬")
    st.caption("Swap a person in a short clip for a different character, keeping the motion and scene.")
    st.info("For parody only. Every download is labelled **AI PARODY** by default: don't pass a clip off as "
            "real, and only use footage you have the rights to.", icon="🎭")

    token = os.getenv("REPLICATE_API_TOKEN")
    if not token:
        st.error("**REPLICATE_API_TOKEN is missing.** On Streamlit Cloud, add it in the app's Settings > "
                 "Secrets as `REPLICATE_API_TOKEN = \"r8_...\"`. Locally, put `REPLICATE_API_TOKEN=r8_...` in a "
                 "`.env` file next to parodyclips.py. Get a token at replicate.com/account/api-tokens, then "
                 "restart the app.")

    # ---------------- sidebar: options and cost
    with st.sidebar:
        st.header("Options")
        resolution = st.radio("Resolution", RESOLUTIONS, horizontal=True, format_func=lambda r: f"{r}p",
                              help="720p looks better; 480p is cheaper and faster.")
        go_fast = st.toggle("Go fast", value=False, help="Extra optimisations for faster generation; "
                                                         "can slightly lower quality.")
        merge_audio = st.toggle("Keep the clip's audio", value=True,
                                help="Ask the model to merge the clip's sound into the result. If it comes "
                                     "back silent anyway, the original audio is put back on.")
        fixed_seed = st.toggle("Fixed seed", value=False, help="Use the same seed to repeat a result.")
        seed = st.number_input("Seed", min_value=0, max_value=2**31 - 1, value=42, step=1,
                               disabled=not fixed_seed)
        watermark = st.toggle("Add 'AI PARODY' label to downloads", value=True)
        st.divider()
        st.subheader("Cost (approx)")
        st.caption(f"From the model page: ${PRICE_PER_SECOND['720']:.2f}/s of video at 720p, "
                   f"${PRICE_PER_SECOND['480']:.2f}/s at 480p. Check replicate.com/{MODEL} for current prices.")
        cost_box = st.empty()
        if S.history:
            st.divider()
            if st.button("Clear history", use_container_width=True):
                S.history = []
                st.rerun()
        st.divider()
        st.subheader("Your usage")
        used, limit = usage.get(user)["swaps"], usage.limits["swaps"]
        st.progress(min(used, limit) / limit, text=f"Swaps: {used} of {limit}")
        st.caption(f"Signed in as **{user}**")
        st.button("Sign out", on_click=_log_out, use_container_width=True)

    # ---------------- inputs
    clip_col, photo_col = st.columns(2, gap="large")
    with clip_col:
        st.subheader("1. Source clip")
        chain = False
        if S.history:
            last = S.history[-1]
            chain = st.checkbox(f"Use last result as source ({last['label']})", value=False,
                                help="For scenes with several people: swap the next person on top of the "
                                     "previous result.")
        if chain:
            src_path = Path(S.history[-1]["clean"])
            st.video(str(src_path))
        else:
            up = st.file_uploader("Upload a clip (mp4 or mov)", type=["mp4", "mov"],
                                  help=f"{MIN_SECONDS:.0f}-{MAX_SECONDS:.0f} seconds, up to {max_upload_mb()} MB.")
            src_path = _save_upload(up.getvalue(), Path(up.name).suffix.lower()) if up else None
            if src_path:
                st.video(up.getvalue())
        info = None
        if src_path:
            try:
                info = probe_video(src_path)
                st.caption(_fmt_info(info))
            except Exception as e:
                st.error(str(e))

        start, end = 0.0, info.duration if info else 0.0
        if info and info.duration > 0.5:
            trim = st.checkbox("Trim the clip", value=info.duration > MAX_SECONDS,
                               help="Cut one shot out of a longer clip before swapping.")
            if trim:
                start, end = st.slider("Keep from / to (seconds)", 0.0, round(info.duration, 1),
                                       (0.0, round(min(info.duration, MAX_SECONDS), 1)), step=0.1,
                                       key=f"trim_{src_path.name}")
                st.caption(f"Keeping {end - start:.1f}s.")

    with photo_col:
        st.subheader("2. Character photo")
        label = st.text_input("Character name (for the history)", placeholder="e.g. Roger")
        pic = st.file_uploader("Upload a photo (jpg or png)", type=["jpg", "jpeg", "png"])
        photo_path = photo = None
        if pic:
            try:
                photo_path, photo = _prepare_photo(pic.getvalue())
                st.image(photo, width=220)
                st.caption("Best results: one person, clearly visible, framed like the person in the clip.")
            except Exception:
                st.error("That photo couldn't be opened. Try a JPEG or PNG.")

    # ---------------- checks, cost, generate
    problems = validate_clip(info, start, end) if info else []
    for p in problems:
        st.error(p)
    if info and photo is not None:
        warn = aspect_warning(photo, info)
        if warn:
            st.warning(warn)
    seconds = (end - start) if info else 0.0
    cost_box.metric("This run", _cost(seconds, resolution) if info else "—",
                    help="Estimated from the clip length and resolution.")

    if not swaps_left:
        st.warning(f"You've used all {usage.limits['swaps']} swaps.")
    ready = bool(token and info and photo_path and not problems and swaps_left)
    if st.button("Generate", type="primary", disabled=not ready, use_container_width=True):
        step = len(S.history) + 1
        name = label.strip() or f"Character {step}"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        timer = st.empty()
        try:
            with st.spinner("Swapping. Wan 2.2 Animate usually takes a few minutes..."):
                timer.caption("Preparing the clip...")
                sent = trim_clip(src_path, work / f"step{step}-{stamp}-source.mp4", start, end)
                raw = run_swap(sent, photo_path, work / f"step{step}-{stamp}-raw.mp4", resolution, go_fast,
                               merge_audio, seed if fixed_seed else None,
                               on_tick=lambda s: timer.caption(f"Elapsed: {int(s) // 60}:{int(s) % 60:02d}"))
                usage.record(user, "swaps")  # counted once Replicate has made (and charged for) the video
                timer.caption("Finishing up...")
                clean = restore_audio(raw, sent, work / f"step{step}-{stamp}-audio.mp4")
                final = add_watermark(clean, work / f"step{step}-{stamp}-parody.mp4") if watermark else clean
            S.history.append({"step": step, "label": f"Step {step} – {name}", "clean": str(clean),
                              "final": str(final), "source": str(sent)})
            timer.empty()
            st.rerun()
        except SwapError as e:
            timer.empty()
            st.error(str(e))
        except Exception as e:  # ffmpeg or file problems
            timer.empty()
            st.error(f"Something went wrong preparing the video: {e}")

    # ---------------- results
    if S.history:
        st.divider()
        latest = S.history[-1]
        st.subheader(f"Result: {latest['label']}")
        st.video(latest["final"])
        st.download_button("Download", Path(latest["final"]).read_bytes(), mime="video/mp4",
                           file_name=Path(latest["final"]).name, type="primary")
        if len(S.history) > 1:
            st.subheader("History")
            cols = st.columns(3)
            for i, item in enumerate(reversed(S.history[:-1])):
                with cols[i % 3]:
                    st.caption(item["label"])
                    st.video(item["final"])
                    st.download_button("Download", Path(item["final"]).read_bytes(), mime="video/mp4",
                                       file_name=Path(item["final"]).name, key=f"dl_{item['step']}")


def main() -> None:
    st.set_page_config(page_title="Parody Clips", page_icon="🎬", layout="wide")
    load_secrets()
    render_ui(require_login())


if __name__ == "__main__":
    main()
