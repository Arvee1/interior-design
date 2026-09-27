"""Restyle - upload a room photo, get interior design concepts, refine them.

Run with:  streamlit run app.py
"""

import copy
import hmac
import os

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from restyle.agent import RoomDesigner, prepare_image, render_after  # noqa: E402
from restyle.prompts import Preferences  # noqa: E402
from restyle.render import draw_pins, palette_html, shopping_list_csv  # noqa: E402
from restyle.tools import STYLE_GUIDES  # noqa: E402
from restyle import catalog, usage  # noqa: E402

st.set_page_config(page_title="Restyle", page_icon="🛋️", layout="wide")

QUICK_CHANGES = ["Warmer and cosier", "Brighter and airier", "Cut the cost", "More plants",
                 "Bolder colour", "Less clutter", "More storage", "Kid and pet friendly"]
SLIDER_STEPS = ["Much less", "Less", "As is", "More", "Much more"]

# ---------------------------------------------------------------- API key
def _secret(name: str):
    try:
        return st.secrets.get(name)
    except Exception:
        return None

for _name in ("ANTHROPIC_API_KEY", "REPLICATE_API_TOKEN", "SERPER_API_KEY"):
    if not os.getenv(_name) and _secret(_name):
        os.environ[_name] = _secret(_name)

KEY_ENV, KEY_LABEL = (("REPLICATE_API_TOKEN", "Replicate API token") if RoomDesigner().provider == "replicate"
                      else ("ANTHROPIC_API_KEY", "Anthropic API key"))

# ---------------------------------------------------------------- login
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
    st.title("Restyle")
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

# ---------------------------------------------------------------- state
defaults = {
    "image_b64": None, "image": None, "file_sig": None,
    "analysis": None, "concepts": [], "history": [],
    "active": 0, "pending": None, "error": None, "rev": 0, "highlight": None,
}
for k, v in defaults.items():
    st.session_state.setdefault(k, v)


def prefs() -> Preferences:
    store = catalog.STORE if catalog.enabled() and S.get("pref_store", True) else ""
    return Preferences(styles=S.get("pref_styles", []), budget=S.get("pref_budget", "mid"),
                       currency=S.get("pref_currency", "AUD"), notes=S.get("pref_notes", ""), store=store)


def with_products(concept: dict) -> dict:
    """Attach real Harvey Norman products (and prices) when that option is on."""
    p = prefs()
    if not p.store:
        return concept
    with st.spinner(f"Finding matching pieces at {p.store}..."):
        return catalog.match_concept(concept, p.currency)


def money(v: float) -> str:
    return f"{prefs().currency} {v:,.0f}"


def clean_concept(model_obj, locked_items: list[dict] | None = None) -> dict:
    """Pydantic -> dict, dedupe ids, and restore any locked items the model dropped."""
    c = model_obj.model_dump()
    seen = set()
    for i, item in enumerate(c["items"]):
        while item["id"] in seen:
            item["id"] = f'{item["id"]}-{i}'
        seen.add(item["id"])
        item["locked"] = False
    for keep in locked_items or []:
        match = next((i for i in c["items"] if i["id"] == keep["id"]), None)
        if match:
            match.update({**keep, "locked": True})
        else:
            c["items"].insert(0, {**keep, "locked": True})
    return c


# ---------------------------------------------------------------- callbacks
def queue(action: dict):
    S.pending = action


def queue_apply():
    parts = []
    text = S.get(f"refine_text_{S.rev}", "").strip()
    if text:
        parts.append(text)
    words = {"tone": ("cooler in tone", "warmer in tone"),
             "colour": ("quieter and more muted in colour", "bolder and more saturated in colour"),
             "spend": ("cheaper, lower spend", "more premium, higher spend")}
    adj = []
    for key, (neg, pos) in words.items():
        idx = SLIDER_STEPS.index(S.get(f"slider_{key}_{S.rev}", "As is")) - 2
        if idx:
            adj.append(("much " if abs(idx) == 2 else "a little ") + (pos if idx > 0 else neg))
    if adj:
        parts.append("Also make it " + ", ".join(adj) + ".")
    if parts:
        queue({"type": "refine", "instruction": " ".join(parts)})
    else:
        S.error = "Type a change or move a slider first."


def toggle_keep(idx: int, item_id: str):
    for item in S.concepts[idx]["items"]:
        if item["id"] == item_id:
            item["locked"] = not item.get("locked")


def remove_item(idx: int, item_id: str):
    S.history[idx].append(copy.deepcopy(S.concepts[idx]))
    S.concepts[idx]["items"] = [i for i in S.concepts[idx]["items"] if i["id"] != item_id]
    S.concepts[idx].pop("after_image", None)  # the picture no longer matches the list
    S.rev += 1


def undo(idx: int):
    if S.history[idx]:
        S.concepts[idx] = S.history[idx].pop()
        S.rev += 1


def start_over():
    for k, v in defaults.items():
        S[k] = copy.deepcopy(v)
    S.upload_n = S.get("upload_n", 0) + 1  # fresh uploader, so the old photo isn't counted again


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Steer the ideas")
    st.caption("All optional. Leave blank for three contrasting directions.")
    st.multiselect("Styles you're drawn to", [s.title() for s in STYLE_GUIDES], key="pref_styles")
    st.radio("Budget", ["low", "mid", "high"], index=1, horizontal=True, key="pref_budget",
             format_func=lambda v: {"low": "Thrifty", "mid": "Mid-range", "high": "Investment"}[v])
    st.selectbox("Currency", ["AUD", "USD", "GBP", "EUR", "NZD", "CAD"], key="pref_currency")
    st.text_area("Anything to know?", key="pref_notes", height=110,
                 placeholder="e.g. Keeping the grey sofa. Two kids and a dog. Renting, so no painting.")
    if catalog.enabled():
        st.toggle(f"Use {catalog.STORE} products", value=True, key="pref_store",
                  help=f"Match each piece to a real {catalog.STORE} product with its price and a link.")
    st.divider()
    if os.getenv(KEY_ENV):
        st.caption(f"Model: `{RoomDesigner().model}`")
    else:
        key = st.text_input(KEY_LABEL, type="password",
                            help=f"Or set {KEY_ENV} in .env / Streamlit secrets.")
        if key:
            os.environ[KEY_ENV] = key
            st.rerun()
    if S.concepts:
        st.button("Start with a new photo", on_click=start_over, use_container_width=True)
    st.divider()
    st.subheader("Your usage")
    used = usage.get(S.user)
    for kind, limit in usage.LIMITS.items():
        st.progress(min(used[kind], limit) / limit, text=f"{usage.LABELS[kind]}: {used[kind]} of {limit}")
    st.caption(f"Signed in as **{S.user}**")
    st.button("Sign out", on_click=_log_out, use_container_width=True)

st.title("Restyle")
st.caption("Design ideas from a photo of your room")

if S.error:
    st.error(S.error)
    S.error = None

designer = RoomDesigner()

# ---------------------------------------------------------------- pending actions
if S.pending:
    action, S.pending = S.pending, None
    idx = S.active
    try:
        if action["type"] == "generate":
            with st.spinner("Studying your room and sketching three concepts. This usually takes under a minute..."):
                result = designer.generate(S.image_b64, prefs())
            S.analysis = result.analysis.model_dump()
            S.concepts = [with_products(clean_concept(c)) for c in result.concepts]
            S.history = [[] for _ in S.concepts]
            S.active = 0
        elif action["type"] == "refine":
            if not quota_left("refines"):
                raise RuntimeError(f"you've used all {usage.LIMITS['refines']} design changes.")
            current = S.concepts[idx]
            locked = [i for i in current["items"] if i.get("locked")]
            with st.spinner(f"Reworking the concept: {action['instruction'][:80]}"):
                out = designer.refine(S.image_b64, prefs(), S.analysis, current,
                                      [i["id"] for i in locked], action["instruction"])
            S.history[idx].append(copy.deepcopy(current))
            S.concepts[idx] = with_products(clean_concept(out, locked))
            usage.record(S.user, "refines")
        elif action["type"] == "new":
            with st.spinner("Sketching a new direction..."):
                out = designer.new_concept(S.image_b64, prefs(), S.analysis, [c["name"] for c in S.concepts])
            S.concepts.append(with_products(clean_concept(out)))
            S.history.append([])
            S.active = len(S.concepts) - 1
        elif action["type"] == "render":
            if not quota_left("renders"):
                raise RuntimeError(f"you've used all {usage.LIMITS['renders']} after pictures.")
            if not os.getenv("REPLICATE_API_TOKEN"):
                raise RuntimeError("the after picture needs REPLICATE_API_TOKEN in .env or Streamlit secrets.")
            with st.spinner(f"Picturing your room as {S.concepts[idx]['name']}. This takes about 10-20 seconds..."):
                S.concepts[idx]["after_image"] = render_after(S.image_b64, S.concepts[idx])
            usage.record(S.user, "renders")
        S.rev += 1
        S.highlight = None
    except Exception as e:  # surface API / validation errors in the UI
        S.error = f"That didn't work: {e}"
    st.rerun()

# ---------------------------------------------------------------- upload screen
if not S.concepts:
    left, right = st.columns([1.3, 1], gap="large")
    with left:
        out_of_uploads = not quota_left("uploads") and S.image is None
        if out_of_uploads:
            st.warning(f"You've used all {usage.LIMITS['uploads']} photo uploads.")
        upload = st.file_uploader("Upload a photo of your room", type=["jpg", "jpeg", "png", "webp"],
                                  help="A wide shot from a corner, in daylight, gives the best ideas.",
                                  disabled=out_of_uploads, key=f"upload_{S.get('upload_n', 0)}")
        if upload:
            sig = (upload.name, upload.size)
            if sig != S.file_sig:
                if not quota_left("uploads"):
                    st.error(f"You've used all {usage.LIMITS['uploads']} photo uploads.")
                else:
                    try:
                        S.image_b64, S.image = prepare_image(upload.getvalue())
                        S.file_sig = sig
                        usage.record(S.user, "uploads")
                        st.rerun()  # refresh the sidebar count
                    except Exception:
                        st.error("That photo couldn't be opened. Try a JPEG or PNG.")
            if S.image is not None:
                st.image(S.image)
    with right:
        st.subheader("How it works")
        st.markdown(
            "1. Upload a photo and set your preferences in the sidebar.\n"
            "2. Get three design concepts, each with a palette, materials and a costed furniture list "
            "pinned onto your photo.\n"
            "3. Refine: keep pieces you love, swap or remove others, and describe changes."
        )
        st.button("Generate design ideas", type="primary", disabled=S.image_b64 is None,
                  on_click=queue, args=({"type": "generate"},), use_container_width=True)
    st.stop()

# ---------------------------------------------------------------- studio
names = [c["name"] for c in S.concepts]
S.active = min(S.active, len(S.concepts) - 1)
sel_col, add_col = st.columns([5, 1])
with sel_col:
    picked = st.radio("Concept", range(len(names)), index=S.active, horizontal=True,
                      format_func=lambda i: names[i], label_visibility="collapsed", key=f"concept_pick_{S.rev}")
    if picked != S.active:
        S.active = picked
        S.highlight = None
        st.rerun()
with add_col:
    st.button("+ Another idea", on_click=queue, args=({"type": "new"},),
              disabled=len(S.concepts) >= 6, use_container_width=True)

idx = S.active
c = S.concepts[idx]
items = c["items"]

photo_col, detail_col = st.columns([1.1, 1], gap="large")

with photo_col:
    ba_tab, pins_tab = st.tabs(["Before & after", "Where things go"])
    with ba_tab:
        before_col, after_col = st.columns(2)
        with before_col:
            st.image(S.image, caption="Before")
        with after_col:
            if c.get("after_image"):
                st.image(c["after_image"], caption=f"After: {c['name']}")
            else:
                st.info("See this concept in your room.")
                st.button("Generate after picture", type="primary", on_click=queue,
                          args=({"type": "render"},), use_container_width=True, key=f"render_{S.rev}",
                          disabled=not quota_left("renders"))
                if not quota_left("renders"):
                    st.caption(f"You've used all {usage.LIMITS['renders']} after pictures.")
        if c.get("after_image"):
            st.caption("AI impression of the concept, not an exact render of every listed piece.")
            st.button(f"Regenerate after picture ({quota_left('renders')} left)", on_click=queue,
                      args=({"type": "render"},), key=f"rerender_{S.rev}", disabled=not quota_left("renders"))
    with pins_tab:
        st.image(draw_pins(S.image, items, S.highlight),
                 caption="Numbered pins show where each piece would go. Green ring = kept.")
    a = S.analysis or {}
    st.markdown(f"**{a.get('room_type', 'Your room')}.** {a.get('summary', '')}")
    if a.get("keep"):
        st.caption("Worth keeping: " + ", ".join(a["keep"]))
    if a.get("constraints"):
        st.caption("Working around: " + ", ".join(a["constraints"]))

with detail_col:
    st.header(c["name"])
    if c["tagline"]:
        st.markdown(f"*{c['tagline']}*")
    st.write(c["summary"])
    if c["palette"]:
        st.markdown(palette_html(c["palette"]), unsafe_allow_html=True)
    if c["materials"]:
        st.caption("Materials: " + ", ".join(c["materials"]))

    # ---- refine panel
    with st.container(border=True):
        st.subheader("Refine this look")
        no_refines = not quota_left("refines")
        if no_refines:
            st.warning(f"You've used all {usage.LIMITS['refines']} design changes. "
                       "You can still keep, remove and undo pieces.")
        else:
            st.caption(f"Tick Keep on pieces you love so they stay put, then ask for changes. "
                       f"{quota_left('refines')} design changes left.")
        chip_cols = st.columns(4)
        for n, q in enumerate(QUICK_CHANGES):
            chip_cols[n % 4].button(q, key=f"quick_{n}_{S.rev}", on_click=queue, disabled=no_refines,
                                    args=({"type": "refine", "instruction": q},), use_container_width=True)
        s1, s2, s3 = st.columns(3)
        s1.select_slider("Tone (cooler / warmer)", SLIDER_STEPS, value="As is", key=f"slider_tone_{S.rev}")
        s2.select_slider("Colour (quieter / bolder)", SLIDER_STEPS, value="As is", key=f"slider_colour_{S.rev}")
        s3.select_slider("Spend (less / more)", SLIDER_STEPS, value="As is", key=f"slider_spend_{S.rev}")
        st.text_area("Describe a change", key=f"refine_text_{S.rev}", height=80,
                     placeholder="e.g. Swap the coffee table for something round, or make room for a desk by the window")
        b1, b2 = st.columns([1, 1])
        b1.button(f"Undo last change ({len(S.history[idx])})", on_click=undo, args=(idx,),
                  disabled=not S.history[idx], use_container_width=True)
        b2.button("Apply changes", type="primary", on_click=queue_apply, use_container_width=True,
                  disabled=no_refines)

    # ---- items
    low = sum(i["price_low"] for i in items)
    high = sum(i["price_high"] for i in items)
    ess = sum(i["price_low"] for i in items if i["priority"] == "essential")
    st.subheader("The pieces")
    st.caption(f"Total {money(low)} to {money(high)}. Essentials from {money(ess)}.")

    for n, item in enumerate(items, start=1):
        with st.container(border=True):
            left, right = st.columns([3, 1.3])
            with left:
                pin = f"**{n}.**" if item.get("x") is not None else f"~~{n}~~"
                optional = " · _optional_" if item["priority"] == "nice" else ""
                st.markdown(f"{pin} **{item['name']}** · {item['category']}{optional}")
                st.write(item["description"])
                st.caption(item["placement"])
                if item.get("product_url"):
                    img_col, txt_col = st.columns([1, 3])
                    if item.get("product_image"):
                        img_col.image(item["product_image"], use_container_width=True)
                    txt_col.markdown(f"[{item['product_title']}]({item['product_url']})  \n"
                                     f"_at {catalog.STORE}_")
            with right:
                if item.get("product_price_aud"):
                    st.markdown(f"**{money(item['price_low'])}**")
                else:
                    st.markdown(f"**{money(item['price_low'])} to {money(item['price_high'])}**")
                st.checkbox("Keep", value=bool(item.get("locked")), key=f"keep_{idx}_{item['id']}_{S.rev}",
                            on_change=toggle_keep, args=(idx, item["id"]))
                r1, r2 = st.columns(2)
                r1.button("Swap", key=f"swap_{item['id']}_{S.rev}", help="Suggest a different piece for this spot",
                          disabled=no_refines,
                          on_click=queue, args=({"type": "refine", "instruction":
                              f'Replace "{item["name"]}" (id {item["id"]}) with a different piece that fills '
                              f"the same role, in keeping with the concept."},))
                r2.button("Remove", key=f"rm_{item['id']}_{S.rev}", on_click=remove_item, args=(idx, item["id"]))
                if item.get("x") is not None:
                    if st.button("Show on photo", key=f"hl_{item['id']}_{S.rev}"):
                        S.highlight = None if S.highlight == item["id"] else item["id"]
                        st.rerun()

    if c["lighting"]:
        st.subheader("Lighting")
        st.write(c["lighting"])
    if c["tips"]:
        st.subheader("Making it work in this room")
        st.markdown("\n".join(f"- {t}" for t in c["tips"]))

    st.download_button("Download shopping list (CSV)", shopping_list_csv(c, prefs().currency),
                       file_name=f"{c['name'].lower().replace(' ', '-')}-shopping-list.csv", mime="text/csv")
