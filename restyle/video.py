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
SWAP_MODELS = {
    "okaris/roop": {"video": "target", "face": "source", "extra": {"keep_fps": True, "keep_frames": False}},
    "arabyai-replicate/roop_face_swap": {"video": "target_video", "face": "swap_image", "extra": {}},
    # Not updated since 2023; its runs now fail with "Got error trying to upload output files".
    "xrunda/hello": {"video": "source", "face": "target", "extra": {}},
}
DEFAULT_SWAP_MODELS = ["okaris/roop", "arabyai-replicate/roop_face_swap"]  # tried in order


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
    # YouTube sometimes refuses the normal route (HTTP 403), especially from cloud servers.
    # If so, retry with its streaming (HLS) format, which is a separate delivery path and is refused less.
    attempts = [{}, {"format": "bv*[protocol^=m3u8][height<=720]+ba[protocol^=m3u8]/b[protocol^=m3u8]"}]
    error = None
    for extra in attempts:
        try:
            with yt_dlp.YoutubeDL({**options, **extra}) as ydl:
                info = ydl.extract_info(url.strip(), download=True)
        except yt_dlp.utils.DownloadError as e:
            error = str(e)
            continue
        if not info or not info.get("requested_downloads"):
            raise ValueError(too_long(info or {}) or "That video couldn't be downloaded.")
        return Path(info["requested_downloads"][0]["filepath"])
    if any(sign in (error or "") for sign in ("403", "Sign in to confirm", "not a bot", "file is empty")):
        raise ValueError("YouTube blocked this download. It often refuses requests from hosted sites. "
                         "Run the app on your own computer, or download the video there and use "
                         "\"Upload a video file\" instead.")
    raise ValueError(f"That video couldn't be downloaded: {(error or 'unknown error')[:200]}")


def trim(source: str | Path, start: float, seconds: float, workdir: str | Path) -> bytes:
    """Cut [start, start + seconds] out of a video and return it as a web-friendly MP4 (max 720p)."""
    out = Path(workdir) / "clip.mp4"
    result = _run(["-y", "-ss", f"{max(0.0, start):.2f}", "-t", f"{min(seconds, MAX_CLIP_SECONDS):.2f}",
                   "-i", str(source), "-vf", "scale=-2:'min(720,ih)',setsar=1", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(out)])
    if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("That video couldn't be trimmed. Try a different file or link.")
    return out.read_bytes()


def _named(data: bytes, name: str) -> io.BytesIO:
    f = io.BytesIO(data)
    f.name = name  # tells Replicate the content type
    return f


def _read_video(out) -> bytes:
    """Bytes of the MP4 in a Replicate output (a file, a URL, or a list of either)."""
    items = out if isinstance(out, (list, tuple)) else [out]
    if not items:
        raise RuntimeError("the model returned no video.")
    item = next((i for i in items if str(getattr(i, "url", i)).lower().split("?")[0].endswith(".mp4")), items[0])
    if hasattr(item, "read"):
        return item.read()
    with urllib.request.urlopen(str(item)) as resp:  # older clients return a URL
        return resp.read()


def restore_audio(swapped: bytes, clip: bytes) -> bytes:
    """Put the original clip's sound onto the swapped video (face-swap models often drop it).
    Returns the swapped video unchanged if the clip has no sound or the merge fails."""
    with tempfile.TemporaryDirectory(prefix="faceclips_") as tmp:
        video_in, audio_in, out = Path(tmp) / "swapped.mp4", Path(tmp) / "clip.mp4", Path(tmp) / "out.mp4"
        video_in.write_bytes(swapped)
        audio_in.write_bytes(clip)
        if "Audio:" in _run(["-i", str(video_in)]).stderr or "Audio:" not in _run(["-i", str(audio_in)]).stderr:
            return swapped  # the result already has sound, or there is none to add
        result = _run(["-y", "-i", str(video_in), "-i", str(audio_in), "-map", "0:v:0", "-map", "1:a:0",
                       "-c:v", "copy", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(out)])
        if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            return swapped
        return out.read_bytes()


def swap_face(clip: bytes, face_jpeg: bytes, model: str | None = None) -> bytes:
    """Swap the face from a photo into a video clip on Replicate; returns MP4 bytes.
    With no model chosen (argument or FACECLIPS_MODEL), tries each default model until one works."""
    import replicate

    chosen = model or os.getenv("FACECLIPS_MODEL")
    error = None
    for name in ([chosen] if chosen else DEFAULT_SWAP_MODELS):
        spec = SWAP_MODELS.get(name.split(":")[0])
        if not spec:
            raise ValueError(f"Unknown face-swap model '{name}'. Use one of: {', '.join(SWAP_MODELS)}.")
        try:
            ref = name if ":" in name else f"{name}:{replicate.models.get(name).latest_version.id}"
            out = replicate.run(ref, input={spec["video"]: _named(clip, "clip.mp4"),
                                            spec["face"]: _named(face_jpeg, "face.jpg"), **spec["extra"]})
            return restore_audio(_read_video(out), clip)
        except Exception as e:  # a model that's broken or offline shouldn't sink the swap; try the next
            error = f"{name}: {e}"
    raise RuntimeError(error or "no face-swap model is available.")


# Whole-person replacement (official Replicate models, run by name).
PERSON_MODEL = "wan-video/wan-2.2-animate-replace"   # replaces the main person; keeps motion, expressions, lips
DESCRIBED_MODEL = "kwaivgi/kling-v3-omni-video"      # prompt-driven edit: you say who to replace
DESCRIBED_SECONDS = (3, 10)                          # clip length the prompt-driven model accepts
COST_PER_SECOND = {"480": 0.02, "720": 0.05, "described": 0.168}  # USD, from Replicate's pricing pages


CUTOUT_MODEL = "bria/remove-background"  # official model, about US$0.02 per photo


def cut_out_person(person_jpeg: bytes) -> bytes:
    """The person on a plain background, so the swap model isn't distracted by what's around them.
    Returns the photo unchanged if the cut-out step fails."""
    import replicate

    from .people import on_plain_background

    try:
        out = replicate.run(os.getenv("FACECLIPS_CUTOUT_MODEL", CUTOUT_MODEL),
                            input={"image": _named(person_jpeg, "person.jpg")})
        item = out[0] if isinstance(out, (list, tuple)) else out
        if hasattr(item, "read"):
            data = item.read()
        else:
            with urllib.request.urlopen(str(item)) as resp:
                data = resp.read()
        return on_plain_background(data)
    except Exception:
        return person_jpeg


def replace_person(clip: bytes, person_jpeg: bytes, resolution: str = "720",
                   box: tuple[int, int, int, int] | None = None) -> bytes:
    """Replace a person in the clip with the person in the photo (Wan 2.2 Animate Replace).
    They keep doing and saying the same thing; background, camera and sound stay.

    The model replaces everyone it sees, so when `box` (x, y, w, h) is given, only that part of the
    frame is sent to it and the result is pasted back, leaving the other people untouched."""
    import replicate

    from .people import crop_clip, paste_back

    piece = crop_clip(clip, box) if box else clip
    out = replicate.run(os.getenv("FACECLIPS_PERSON_MODEL", PERSON_MODEL), input={
        "video": _named(piece, "clip.mp4"),
        "character_image": _named(cut_out_person(person_jpeg), "person.jpg"),
        "resolution": resolution if resolution in ("480", "720") else "720",
        "merge_audio": True,
    })
    result = _read_video(out)
    if box:
        return paste_back(clip, result, box)
    return restore_audio(result, clip)


def for_described_model(clip: bytes) -> bytes:
    """Re-encode a clip so its shorter side is 720px, which the prompt-driven model requires, and keep
    it compact (about 2 MB for 10 seconds) because it is sent embedded in the request."""
    with tempfile.TemporaryDirectory(prefix="faceclips_") as tmp:
        src, out = Path(tmp) / "in.mp4", Path(tmp) / "out.mp4"
        src.write_bytes(clip)
        # Stop just short of the model's limit: a "10 second" clip usually runs a few frames over.
        result = _run(["-y", "-i", str(src), "-t", f"{DESCRIBED_SECONDS[1] - 0.2:.1f}",
                       "-vf", "scale='if(gt(iw,ih),-2,720)':'if(gt(iw,ih),720,-2)',setsar=1",  # models reject non-square pixels
                       "-c:v", "libx264", "-preset", "medium", "-crf", "24", "-maxrate", "1500k", "-bufsize", "3000k",
                       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)])
        if result.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("That clip couldn't be prepared for the model.")
        return out.read_bytes()


def described_prompt(who: str, animal: bool = False) -> str:
    who = " ".join(who.split()).strip(" .")[:200]
    if animal:
        return (
            f"Replace {who} in <<<video_1>>> with the animal shown in <<<image_1>>>, as a character with that "
            "animal's head, face, ears, fur, colours and markings, clearly recognisable as the same animal. "
            "It stands or sits upright in the same place and pose as the original person and wears the same "
            "clothes. It does exactly what the original person does: the same movements, gestures, head turns, "
            "expressions and mouth movements at the same moments, so it appears to say the same words. "
            "Change only that one person. Everyone else, the background, the camera movement, the framing "
            "and the lighting stay exactly as in <<<video_1>>>."
        )
    return (
        f"Replace {who} in <<<video_1>>> with the person shown in <<<image_1>>>: their face, hair, body and "
        "overall look. The new person does exactly what the original person does: the same movements, "
        "gestures, facial expressions and mouth movements at the same moments, so they appear to say the same "
        "words. Change only that one person. Everyone else, the background, the camera movement, the framing "
        "and the lighting stay exactly as in <<<video_1>>>."
    )


def _kling_edit(clip: bytes, image_jpeg: bytes, prompt: str) -> bytes:
    """Edit a 3-10 second clip with Kling 3.0 Omni, using one reference photo; returns MP4 bytes."""
    import replicate

    # This model checks that its inputs end in .mp4/.jpg. Uploaded files get an address with no
    # extension and are rejected, so embed them in the request (base64), which carries the file type.
    out = replicate.run(os.getenv("FACECLIPS_DESCRIBED_MODEL", DESCRIBED_MODEL), file_encoding_strategy="base64", input={
        "prompt": prompt,
        "reference_video": _named(for_described_model(clip), "clip.mp4"),
        "video_reference_type": "base",
        "reference_images": [_named(image_jpeg, "person.jpg")],
        "keep_original_sound": True,
        "mode": "standard",
    })
    return _read_video(out)


def replace_described_person(clip: bytes, person_jpeg: bytes, who: str, animal: bool = False) -> bytes:
    """Replace the person described by `who` (e.g. 'the man driving') with the person, or animal,
    in the photo (Kling 3.0 Omni video edit). The clip must be 3-10 seconds long."""
    return restore_audio(_kling_edit(clip, person_jpeg, described_prompt(who, animal)), clip)


HEAD_PROMPT = (
    "In <<<video_1>>>, replace only the person's head with the head of the person shown in <<<image_1>>>: "
    "their face, facial features, skin tone, hair, hairline and head shape, so they are clearly recognisable "
    "as the person in <<<image_1>>>. Keep everything below the neck exactly as in <<<video_1>>>: the same "
    "body, clothes, hands and movements. The new head moves exactly like the original one: the same head "
    "turns, facial expressions, eye movements and mouth movements at the same moments, so they appear to say "
    "the same words. Do not add hats, headphones, glasses or accessories. The background, camera movement, "
    "framing and lighting stay exactly as in <<<video_1>>>."
)


ANIMAL_HEAD_PROMPT = (
    "In <<<video_1>>>, replace only the person's head with the head of the animal shown in <<<image_1>>>: "
    "its face, ears, fur, colours and markings, sized to sit naturally on the person's neck and clearly "
    "recognisable as the same animal. Keep everything below the neck exactly as in <<<video_1>>>: the same "
    "human body, clothes, hands and movements. The animal head moves exactly like the original head: the "
    "same head turns, expressions, eye movements and mouth movements at the same moments, so it appears to "
    "say the same words. Do not add hats, headphones, glasses or accessories. The background, camera "
    "movement, framing and lighting stay exactly as in <<<video_1>>>."
)


def replace_head(clip: bytes, head_jpeg: bytes, box: tuple[int, int, int, int] | None = None,
                 animal: bool = False) -> bytes:
    """Replace a person's head (face, hair and head shape) with the one in the photo, keeping their body,
    clothes and movements (Kling 3.0 Omni video edit; clip must be 3-10 seconds). With `box`, only that
    person's part of the frame is sent and pasted back, so other people are left alone and the head
    takes up more of the picture the model works on."""
    from .people import crop_clip, paste_back

    piece = crop_clip(clip, box) if box else clip
    result = _kling_edit(piece, head_jpeg, ANIMAL_HEAD_PROMPT if animal else HEAD_PROMPT)
    if box:
        return paste_back(clip, result, box)
    return restore_audio(result, clip)


def new_workdir() -> str:
    return tempfile.mkdtemp(prefix="faceclips_")
