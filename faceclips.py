"""Face Clips - swap a person from your photo into a short video clip (whole person or face only).

Run with:  streamlit run faceclips.py
"""

import hmac
import io
import os
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from restyle import people, video  # noqa: E402
from restyle.agent import prepare_image  # noqa: E402
from restyle.faces import _detect as detect_faces  # noqa: E402
from restyle.usage import Tracker  # noqa: E402

st.set_page_config(page_title="Face Clips", page_icon="🎬", layout="wide")

st.markdown("# Wazzup!!! 👋")
st.warning("**This is a prototype test site.** It's for private fun only. Videos are AI-generated face swaps: "
           "only use photos of people who have agreed to it, don't pass a swap off as real, and don't share "
           "clips you don't have the rights to. Features, limits and saved usage can change or be reset at "
           "any time.", icon="🧪")


# ---------------------------------------------------------------- keys, login, usage
def _secret(name: str):
    try:
        return st.secrets.get(name)
    except Exception:
        return None


for _name in ("REPLICATE_API_TOKEN",):
    if not os.getenv(_name) and _secret(_name):
        os.environ[_name] = _secret(_name)

usage = Tracker(
    limits={"videos": 5, "swaps": 5},
    labels={"videos": "Videos loaded", "swaps": "Swaps"},
    file=os.getenv("FACECLIPS_USAGE_FILE", Path(__file__).resolve().parent / ".faceclips_usage.json"),
)


def _allowed_users() -> list[str]:
    """ALLOWED_USERS from Streamlit secrets (a name or a list of names), or comma-separated in .env."""
    raw = _secret("ALLOWED_USERS") or os.getenv("ALLOWED_USERS", "")
    names = raw.split(",") if isinstance(raw, str) else list(raw)
    return [n.strip().lower() for n in names if str(n).strip()]


def _log_in():
    typed = S.get("login_name", "").strip().lower()
    if any(hmac.compare_digest(typed, u) for u in _allowed_users()):
        S.user = typed
    else:
        S.login_error = True


def _log_out():
    S.clear()


S = st.session_state
if not S.get("user"):
    st.title("Face Clips")
    if not _allowed_users():
        st.error("No users are set up. Add ALLOWED_USERS to the app's Streamlit secrets.")
        st.stop()
    with st.form("login"):
        st.text_input("Username", key="login_name")
        st.form_submit_button("Sign in", type="primary", on_click=_log_in)
    if S.pop("login_error", False):
        st.error("That username isn't recognised.")
    st.stop()


def quota_left(kind: str) -> int:
    return usage.remaining(S.user, kind)


for k, v in {"workdir": None, "source": None, "source_len": 0.0, "clip": None, "clip_len": 0.0, "clip_people": None, "face": None, "face_sig": None,
             "results": [], "error": None, "n": 0}.items():
    S.setdefault(k, v)
if S.workdir is None or not Path(S.workdir).exists():
    S.workdir = video.new_workdir()


def start_over():
    S.source = S.clip = S.face = S.face_sig = S.clip_people = None
    S.source_len, S.results = 0.0, []
    S.n += 1  # fresh uploaders


def load_source(path: Path):
    length = video.duration(path)
    if length <= 0:
        raise ValueError("That file isn't a video this app can read. Try an MP4.")
    S.source, S.source_len, S.clip = str(path), length, None
    usage.record(S.user, "videos")


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("How it works")
    st.markdown("1. Load a video: paste a YouTube link or upload a file.\n"
                f"2. Pick the part you want (up to {video.MAX_CLIP_SECONDS} seconds).\n"
                "3. Upload a photo and pick the person to put in.\n"
                "4. Swap the whole person (they keep doing and saying the same thing) or just the face.")
    st.divider()
    if not os.getenv("REPLICATE_API_TOKEN"):
        key = st.text_input("Replicate API token", type="password",
                            help="Or set REPLICATE_API_TOKEN in .env / Streamlit secrets.")
        if key:
            os.environ["REPLICATE_API_TOKEN"] = key
            st.rerun()
    if S.source:
        st.button("Start over", on_click=start_over, use_container_width=True)
    st.divider()
    st.subheader("Your usage")
    used = usage.get(S.user)
    for kind, limit in usage.limits.items():
        st.progress(min(used[kind], limit) / limit, text=f"{usage.labels[kind]}: {used[kind]} of {limit}")
    st.caption(f"Signed in as **{S.user}**")
    st.button("Sign out", on_click=_log_out, use_container_width=True)

st.title("Face Clips 🎬")
st.caption("Star in your favourite scene: swap a person from your photo into a clip.")

if S.error:
    st.error(S.error)
    S.error = None

# ---------------------------------------------------------------- 1. load a video
if not S.source:
    st.subheader("1. Load a video")
    if not quota_left("videos"):
        st.warning(f"You've used all {usage.limits['videos']} video loads.")
        st.stop()
    link_tab, file_tab = st.tabs(["Paste a YouTube link", "Upload a video file"])
    with link_tab:
        url = st.text_input("YouTube link", placeholder="https://www.youtube.com/watch?v=...", key=f"url_{S.n}")
        st.caption("Links only work when you run this app on your own computer. YouTube blocks hosted sites "
                   "like Streamlit Cloud, so there, use \"Upload a video file\" instead.")
        if st.button("Get video", type="primary", disabled=not url.strip()):
            try:
                with st.spinner("Fetching the video..."):
                    load_source(video.fetch_from_link(url, S.workdir))
                st.rerun()
            except Exception as e:
                st.error(f"Couldn't get that video: {str(e)[:300]}")
    with file_tab:
        up = st.file_uploader("Video file", type=["mp4", "mov", "m4v", "webm"], key=f"vid_{S.n}")
        if up:
            try:
                path = Path(S.workdir) / f"upload{Path(up.name).suffix.lower() or '.mp4'}"
                path.write_bytes(up.getvalue())
                load_source(path)
                st.rerun()
            except Exception as e:
                st.error(str(e))
    st.stop()

# ---------------------------------------------------------------- 2. pick the part
clip_col, face_col = st.columns(2, gap="large")
with clip_col:
    st.subheader("2. Pick the part to use")
    longest = min(float(video.MAX_CLIP_SECONDS), S.source_len)
    c1, c2 = st.columns(2)
    start = c1.number_input("Start (seconds)", 0.0, max(0.0, S.source_len - 1.0), 0.0, step=1.0)
    length = c2.number_input("Length (seconds)", 1.0, max(1.0, longest), min(15.0, max(1.0, longest)), step=1.0)
    st.caption(f"The video is {S.source_len:.0f} seconds long. Shorter clips are faster and cheaper to swap.")
    if st.button("Cut this part", type="primary" if S.clip is None else "secondary"):
        try:
            with st.spinner("Cutting the clip..."):
                S.clip_len = min(length, S.source_len - start)
                S.clip = video.trim(S.source, start, S.clip_len, S.workdir)
            S.results, S.clip_people = [], None
        except Exception as e:
            st.error(str(e))
    if S.clip:
        st.video(S.clip)

# ---------------------------------------------------------------- 3. the person
def person_crops(photo, face) -> tuple:
    """(whole-person crop, face crop, head crop) for one detected face: the body crop takes the area
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


def as_jpeg(img, min_side: int = 0) -> bytes:
    if min_side and min(img.size) < min_side:  # some models reject small images
        scale = min_side / min(img.size)
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


person_jpeg = face_jpeg = head_jpeg = None
with face_col:
    st.subheader("3. Upload a photo of the person to put in")
    photo = st.file_uploader("Photo", type=["jpg", "jpeg", "png", "webp"], key=f"face_{S.n}",
                             help="A clear, well-lit photo. Showing the upper body or whole body works best "
                                  "for swapping the whole person.")
    if photo and (photo.name, photo.size) != S.face_sig:
        try:
            _, S.face = prepare_image(photo.getvalue())
            S.face_sig = (photo.name, photo.size)
        except Exception:
            st.error("That photo couldn't be opened. Try a JPEG or PNG.")
    if S.face is not None:
        found = sorted(detect_faces(cv2.cvtColor(np.array(S.face), cv2.COLOR_RGB2BGR)), key=lambda f: f[0])[:5]
        if not found:
            st.image(S.face, width=220)
            st.warning("No person found in that photo. Try a clearer photo where the face is visible.")
        else:
            crops = [person_crops(S.face, f) for f in found]  # left to right
            pick = 0
            if len(crops) > 1:
                st.caption(f"Found {len(crops)} people. Choose who to put in the clip.")
                for col, (i, (body, _, _)) in zip(st.columns(len(crops)), enumerate(crops)):
                    col.image(body, caption=f"Person {i + 1}")
                pick = st.radio("Person", range(len(crops)), format_func=lambda i: f"Person {i + 1}",
                                horizontal=True, label_visibility="collapsed")
            else:
                st.image(crops[0][0], width=220)
            person_jpeg, face_jpeg = as_jpeg(crops[pick][0], min_side=320), as_jpeg(crops[pick][1])
            head_jpeg = as_jpeg(crops[pick][2], min_side=320)

# ---------------------------------------------------------------- 4. swap
st.divider()
st.subheader("4. Swap")
MODES = {
    "main": "Whole person: the main person in the clip",
    "described": "Whole person: I'll say who to replace",
    "head": "Head only (face and hair)",
    "face": "Face only",
}
mode = st.radio("What to swap", list(MODES), format_func=MODES.get, key="mode")
who, resolution, problem, box = "", "720", None, None
lo, hi = video.DESCRIBED_SECONDS
if mode == "head":
    st.caption(f"Replaces the head (face, hair and head shape) with the one from your photo. The actor's body, "
               f"clothes and movements stay, and the original sound stays. Uses Kling 3.0. The clip must be "
               f"{lo} to {hi} seconds long.")
    if S.clip and not lo <= S.clip_len <= hi:
        problem = f"For this option, cut a clip between {lo} and {hi} seconds (yours is {S.clip_len:.0f})."
if mode == "main":
    st.caption("Replaces a person with the person from your photo. They keep the same movements, expressions "
               "and mouth movements, and the original sound stays.")
if mode in ("main", "head"):
    if S.clip:
        if S.clip_people is None:
            with st.spinner("Looking for people in the clip..."):
                S.clip_people = people.find_people(S.clip)
        frame, found_people = S.clip_people
        if len(found_people) > 1:
            target = st.radio("Who in the clip should be replaced?", range(len(found_people)), horizontal=True,
                              format_func=lambda i: f"Person {i + 1}")
            st.image(people.draw_people(frame, found_people, target), width=520,
                     caption="Only the highlighted area is changed. Everyone else stays as they are.")
            box = found_people[target]["box"]
            seen = found_people[target]["seen"]
            if seen < 0.85:
                st.warning(f"This person's face is only visible for about {seen:.0%} of the clip. The swap can "
                           "fail or look wrong when they turn away, leave the shot or the camera cuts. "
                           "A part where they stay in view works best.")
            if box is None:
                st.warning("These people cross over each other in this clip, so one can't be changed without "
                           "the other. Cut a part where they stay apart, or everyone in shot will be replaced.")
        elif len(found_people) == 1 and found_people[0]["seen"] < 0.85:
            st.warning(f"The person's face is only visible for about {found_people[0]['seen']:.0%} of the clip. "
                       "The swap can fail or look wrong when they turn away, leave the shot or the camera "
                       "cuts. A part where they stay in view works best.")
        elif not found_people:
            st.caption("No faces found at the start of the clip, so the whole picture is sent to the model. "
                       "Start the clip where the person's face is visible to choose who to replace.")
if mode == "main":
    resolution = st.radio("Quality", ["720", "480"], horizontal=True,
                          format_func=lambda r: {"720": "Sharper (720p)", "480": "Cheaper and faster (480p)"}[r])
    cost = S.clip_len * video.COST_PER_SECOND[resolution]
elif mode == "head":
    cost = S.clip_len * video.COST_PER_SECOND["described"]
elif mode == "described":
    st.caption(f"Use this when there are several people in the clip. Describe the one to replace. "
               f"The clip must be {lo} to {hi} seconds long.")
    who = st.text_input("Who should be replaced?", placeholder="e.g. the man driving the car")
    cost = S.clip_len * video.COST_PER_SECOND["described"]
    if S.clip and not lo <= S.clip_len <= hi:
        problem = f"For this option, cut a clip between {lo} and {hi} seconds (yours is {S.clip_len:.0f})."
    elif not who.strip():
        problem = "Describe who to replace."
else:
    st.caption("Keeps the actor's body, hair and clothes and replaces only the face. One face in the clip is "
               "replaced, usually the most prominent one.")
    cost = 0.12
if S.clip:
    st.caption(f"Estimated cost on Replicate: about US${cost:.2f} for this clip.")

agreed = st.checkbox("Everyone whose picture I'm using has agreed to this, and I'll make clear it's AI-generated "
                     "if I show it to anyone.")
left = quota_left("swaps")
ready = bool(S.clip and person_jpeg and agreed and left and not problem)
if st.button("Swap", type="primary", disabled=not ready, use_container_width=True):
    try:
        if not os.getenv("REPLICATE_API_TOKEN"):
            raise RuntimeError("add REPLICATE_API_TOKEN in .env or Streamlit secrets.")
        with st.spinner("Swapping. This usually takes a few minutes, longer for longer clips..."):
            if mode == "main":
                out = video.replace_person(S.clip, person_jpeg, resolution, box)
            elif mode == "described":
                out = video.replace_described_person(S.clip, person_jpeg, who)
            elif mode == "head":
                out = video.replace_head(S.clip, video.cut_out_person(head_jpeg), box)
            else:
                out = video.swap_face(S.clip, face_jpeg)
        usage.record(S.user, "swaps")
        S.results.insert(0, {"video": out, "label": MODES[mode]})
        st.rerun()
    except Exception as e:  # surface API errors in the UI
        if "zero-size array" in str(e):  # the swap model found no person in some frames
            st.error("The model couldn't find a person in every frame of this clip. Cut a part that is one "
                     "continuous shot (no camera cuts) where the person's face and upper body stay in view, "
                     "then try again.")
        else:
            st.error(f"That didn't work: {str(e)[:300]}")
if not S.clip:
    st.caption("Cut a clip first (step 2).")
elif not person_jpeg:
    st.caption("Upload a photo of the person (step 3).")
elif problem:
    st.caption(problem)
elif not left:
    st.caption(f"You've used all {usage.limits['swaps']} swaps.")
else:
    st.caption(f"{left} swaps left.")

for n, result in enumerate(S.results):
    before, after = st.columns(2)
    before.video(S.clip)
    before.caption("Before")
    after.video(result["video"])
    after.caption(f"After (AI-generated). {result['label']}")
    after.download_button("Download", result["video"], mime="video/mp4", key=f"dl_{n}_{len(S.results)}",
                          file_name=f"face-clip-{datetime.now():%Y%m%d-%H%M%S}.mp4")
