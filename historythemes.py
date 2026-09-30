"""History Themes - upload a photo of one or more people and restyle their outfits in a chosen era or theme.

Run with:  streamlit run historythemes.py
"""

import hmac
import os
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from restyle.agent import edit_image, prepare_image  # noqa: E402
from restyle.faces import keep_faces  # noqa: E402
from restyle.usage import Tracker  # noqa: E402

st.set_page_config(page_title="History Themes", page_icon="🎭", layout="wide")

st.markdown("# Wazzup!!! 👋")
st.warning("**This is a prototype test site.** It's for trying out ideas only: pictures are AI-generated "
           "and may not look like the people in them. Only upload photos of people who are happy for you "
           "to use them. Features, limits and saved usage can change or be reset at any time.", icon="🧪")

# ---------------------------------------------------------------- themes
# name: (emoji, outfit, hair and accessories, matching background)
THEMES: dict[str, dict[str, tuple[str, str, str, str]]] = {
    "Decades": {
        "1920s Jazz Age": ("🎷", "1920s Jazz Age evening wear: drop-waist beaded flapper dresses with long pearls, "
                           "and three-piece pinstripe suits with bow ties and pocket squares",
                           "finger-wave bobs, feathered headbands, slicked-back hair and fedoras",
                           "an Art Deco speakeasy with brass and dim golden light"),
        "1950s Rock 'n' Roll": ("🎸", "1950s rock 'n' roll fashion: full circle skirts with petticoats, fitted "
                                "cardigans and polka dots, and rolled-cuff jeans with leather jackets or letterman "
                                "jackets", "victory rolls, headscarves, ponytails with ribbons and greased pompadours",
                                "a chrome-and-neon American diner with a jukebox"),
        "1960s Mod": ("🌼", "1960s London Mod fashion: bold geometric shift dresses, A-line mini skirts, go-go "
                      "boots, slim tailored suits with skinny ties and turtlenecks",
                      "bouffant hair, sleek bobs with bangs, mop-top haircuts and big sunglasses",
                      "a colourful 1960s Carnaby Street scene"),
        "1970s Disco": ("🕺", "1970s disco fashion: shiny wide-collar shirts, flared bell-bottom trousers, sequinned "
                        "jumpsuits, platform shoes and glitter", "big feathered hair, afros and sideburns",
                        "a disco dance floor with a glitter ball and coloured lights"),
        "1980s": ("📼", "bold 1980s fashion: neon colours, big shoulder pads, windbreakers, acid-wash denim "
                  "jackets, leg warmers and chunky sneakers", "big teased hair, side ponytails, mullets and "
                  "sweatbands", "a retro 1980s arcade with neon lights"),
        "1990s": ("💿", "1990s fashion: grunge flannel shirts, oversized denim, slip dresses over T-shirts, "
                  "baggy jeans, crop tops, bucket hats and chunky sneakers", "curtain haircuts, butterfly clips, "
                  "scrunchies and frosted tips", "a 1990s mall or skate park"),
        "Y2K 2000s": ("📱", "Y2K early-2000s fashion: velour tracksuits, low-rise jeans, metallic tops, trucker "
                      "caps, tinted sunglasses and platform sneakers", "spiky gelled hair, zigzag parts and "
                      "tiny hair clips", "a glossy early-2000s music video set"),
    },
    "Ancient world": {
        "Ancient Roman": ("🏛️", "Ancient Roman clothing: draped white wool togas with purple borders, tunics, "
                          "stolas and pallas, leather sandals and laurel wreaths",
                          "classical Roman curled hairstyles and laurel wreaths", "a marble Roman forum with columns"),
        "Ancient Greek": ("🏺", "Ancient Greek clothing: flowing pleated chitons and himations in linen, gold "
                          "brooches, cord belts and leather sandals", "braided and pinned-up Greek hairstyles "
                          "with gold bands", "a sunlit Greek temple overlooking the Aegean sea"),
        "Ancient Egyptian": ("🐪", "Ancient Egyptian royal court clothing: white pleated linen kalasiris and "
                             "shendyt, broad beaded gold collars, gold cuffs and sandals",
                             "straight black bob wigs, gold headbands and kohl eyeliner",
                             "a sandstone temple with hieroglyphs beside the Nile"),
        "Stone Age": ("🦴", "playful Stone Age cave-dweller outfits: rough fur and hide tunics with bone and "
                      "stone jewellery", "wild tousled hair", "a prehistoric cave with a campfire"),
    },
    "Historical eras": {
        "Viking": ("🛡️", "Viking Age Norse clothing: wool tunics and apron dresses with oval brooches, fur "
                   "cloaks, leather belts and boots", "braids, beards and simple metal circlets",
                   "a Norse longhouse or a fjord with a longship"),
        "Medieval": ("🏰", "medieval European clothing: velvet gowns with long sleeves, knights' tabards over "
                     "chainmail, surcoats and hooded cloaks", "braided hair, circlets and hoods",
                     "a stone castle great hall with banners"),
        "Renaissance": ("🎨", "Italian Renaissance clothing: rich brocade gowns with square necklines, puffed and "
                        "slashed sleeves, doublets and hose, velvet caps", "braided updos with pearls and "
                        "velvet berets", "a Renaissance palazzo courtyard"),
        "Tudor": ("👑", "Tudor court clothing: jewelled gowns with French hoods, ruffs, fur-trimmed doublets "
                  "and puffed sleeves", "French hoods, pearl headdresses and flat caps", "a Tudor palace hall "
                  "with tapestries"),
        "French Court (1700s)": ("🎀", "18th-century French court fashion: pastel silk gowns with wide panniers, "
                                 "lace and bows, embroidered frock coats and breeches", "towering powdered "
                                 "wigs and tricorne hats", "a gilded Versailles-style hall of mirrors"),
        "Regency": ("💌", "Regency-era clothing: empire-waist muslin gowns with long gloves and spencers, and "
                    "tailcoats with high collars, waistcoats and cravats", "curled updos with ribbons and "
                    "top hats", "an elegant English ballroom"),
        "Victorian": ("🎩", "Victorian clothing: bustled gowns with high lace collars and corseted bodices, "
                      "frock coats, waistcoats with pocket watches", "neat updos, bonnets, top hats and "
                      "moustaches", "a Victorian parlour with gas lamps"),
        "Wild West": ("🤠", "1880s Wild West clothing: cowboy hats, leather vests, chaps and boots, prairie "
                      "dresses and bandanas", "cowboy hats and braids", "a dusty frontier town main street"),
    },
    "Just for fun": {
        "Pirates": ("🏴‍☠️", "Golden Age pirate outfits: long coats with brass buttons, ruffled shirts, sashes, "
                    "tricorne hats and boots", "bandanas, tricorne hats and braided beards",
                    "the deck of a tall pirate ship"),
        "Steampunk": ("⚙️", "steampunk outfits: brown leather corsets and vests, brass goggles, gears, "
                      "pocket watches and long coats", "top hats with goggles", "a Victorian airship workshop"),
        "Space Age Retro": ("🚀", "1960s retro-futurist Space Age outfits: silver metallic jumpsuits, white "
                            "go-go boots, bubble helmets held under the arm", "sleek futuristic bobs",
                            "a 1960s vision of a moon base"),
        "Year 2200": ("🤖", "sleek future sci-fi outfits for the year 2200: glowing trims, iridescent fabrics "
                      "and high-tech jackets", "sleek futuristic hairstyles", "a gleaming futuristic city"),
        "Fairytale Royalty": ("🧚", "fairytale royal outfits: ball gowns and princely jackets with gold "
                              "embroidery, capes and crowns", "crowns and tiaras", "an enchanted castle ballroom"),
    },
}
ALL_THEMES = {name: t for group in THEMES.values() for name, t in group.items()}

# Sports: pick a sport, then a team. Kits are described by colours so the model can draw them.
_NRL_KIT = ("rugby league playing kit: a fitted V-neck jersey with a player number, shorts, long socks "
            "and football boots")
SPORTS: dict[str, dict] = {
    "Basketball (NBA)": {
        "emoji": "🏀",
        "kit": "NBA basketball uniform: a sleeveless jersey with the team name and a player number, matching "
               "shorts and basketball shoes",
        "hair": "sweatbands and headbands",
        "background": "an NBA arena basketball court with a packed crowd",
        "teams": {
            "Atlanta Hawks": "red, white and volt green", "Boston Celtics": "green and white",
            "Brooklyn Nets": "black and white", "Charlotte Hornets": "teal and purple",
            "Chicago Bulls": "red, black and white", "Cleveland Cavaliers": "wine and gold",
            "Dallas Mavericks": "royal blue, navy and silver", "Denver Nuggets": "navy, yellow and red",
            "Detroit Pistons": "red, royal blue and white", "Golden State Warriors": "royal blue and gold",
            "Houston Rockets": "red, silver and black", "Indiana Pacers": "navy blue and gold",
            "LA Clippers": "navy, red and blue", "Los Angeles Lakers": "purple and gold",
            "Memphis Grizzlies": "navy, light blue and gold", "Miami Heat": "red, black and yellow",
            "Milwaukee Bucks": "green, cream and blue", "Minnesota Timberwolves": "midnight blue, blue and green",
            "New Orleans Pelicans": "navy, gold and red", "New York Knicks": "blue, orange and white",
            "Oklahoma City Thunder": "blue, orange and navy", "Orlando Magic": "blue, black and silver",
            "Philadelphia 76ers": "blue, red and white", "Phoenix Suns": "purple, orange and black",
            "Portland Trail Blazers": "red, black and white", "Sacramento Kings": "purple, silver and black",
            "San Antonio Spurs": "black, silver and white", "Toronto Raptors": "red, black and silver",
            "Utah Jazz": "purple, gold and black", "Washington Wizards": "navy, red and white",
        },
    },
    "Rugby League (NRL)": {
        "emoji": "🏉",
        "kit": _NRL_KIT,
        "hair": "strapping tape and headgear",
        "background": "a floodlit rugby league stadium with a big crowd",
        "teams": {
            "Brisbane Broncos": "maroon and gold", "Canberra Raiders": "lime green, white and blue",
            "Canterbury-Bankstown Bulldogs": "blue and white", "Cronulla-Sutherland Sharks": "sky blue, black and white",
            "Dolphins": "red, white and gold", "Gold Coast Titans": "sky blue, gold and navy",
            "Manly Warringah Sea Eagles": "maroon and white", "Melbourne Storm": "purple, navy and gold",
            "Newcastle Knights": "red and blue", "New Zealand Warriors": "navy blue, red and green",
            "North Queensland Cowboys": "navy blue, gold and white", "Parramatta Eels": "blue and gold",
            "Penrith Panthers": "black with red, green and white", "South Sydney Rabbitohs": "cardinal red and myrtle green",
            "St George Illawarra Dragons": "red and white", "Sydney Roosters": "navy blue, red and white",
            "Wests Tigers": "orange, black and white",
        },
    },
    "State of Origin": {
        "emoji": "🔥",
        "kit": _NRL_KIT,
        "hair": "strapping tape and headgear",
        "background": "a packed State of Origin stadium at night",
        "teams": {"Queensland Maroons": "maroon and gold", "New South Wales Blues": "sky blue and navy"},
    },
    "AFL": {
        "emoji": "🏈",
        "kit": "AFL playing kit: a sleeveless guernsey with a player number, shorts, long socks and football boots",
        "hair": "headbands",
        "background": "the MCG on grand final day with a huge crowd",
        "teams": {
            "Adelaide Crows": "navy, red and gold", "Brisbane Lions": "maroon, blue and gold",
            "Carlton": "navy blue and white", "Collingwood": "black and white stripes",
            "Essendon": "black with a red sash", "Fremantle": "purple and white",
            "Geelong Cats": "navy blue and white hoops", "Gold Coast Suns": "red and gold",
            "GWS Giants": "charcoal and orange", "Hawthorn": "brown and gold stripes",
            "Melbourne Demons": "navy blue with a red yoke", "North Melbourne": "royal blue and white stripes",
            "Port Adelaide": "black, white and teal", "Richmond": "black with a yellow sash",
            "St Kilda": "red, white and black", "Sydney Swans": "red and white",
            "West Coast Eagles": "royal blue and gold", "Western Bulldogs": "royal blue, red and white",
        },
    },
}


IMAGE_MODEL = "google/nano-banana-2"  # keeps people recognisable; override with HISTORYTHEMES_IMAGE_MODEL


def theme_prompt(outfit: str, hair: str, background: str, change_hair: bool, change_background: bool) -> str:
    """Edit instruction for the image model: dress everyone head to toe in the theme, swap every
    item that doesn't fit it, and keep each person's face and identity exactly as they are."""
    parts = [
        f"Restyle this photo so every person is dressed head to toe in {outfit}. Replace every item of "
        "clothing and every accessory that doesn't belong to this theme, including hats, caps, beanies, "
        "sunglasses, headphones, watches, bags, lanyards, other logos and everyday shoes: swap each one for "
        "a piece that fits the theme, or remove it. Nothing that doesn't fit the theme should be left on anyone."
    ]
    if change_hair:
        parts.append(f"Also give them hairstyles and headwear that fit the theme ({hair}).")
    else:
        parts.append("Keep everyone's natural hair, but any hat or headwear must fit the theme or be removed.")
    parts.append(f"Replace the background with {background}." if change_background
                 else "Keep the background exactly as it is.")
    parts.append(
        "Keep every person's face and identity exactly as in the original photo: the same face shape, eyes, "
        "nose, mouth, skin tone, age and expression, so each person is instantly recognisable as themselves. "
        "Keep the same number of people in the same poses, positions and body shapes, with the same camera "
        "angle, framing and lighting. Outfits are complete, well-fitted, modest and age-appropriate, like a "
        "high-quality costume photo shoot. Photorealistic, no text."
    )
    return " ".join(parts)


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
    limits={"uploads": 5, "images": 10},
    labels={"uploads": "Photos uploaded", "images": "Themed pictures"},
    file=os.getenv("HISTORYTHEMES_USAGE_FILE", Path(__file__).resolve().parent / ".historythemes_usage.json"),
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
    st.title("History Themes")
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


for k, v in {"image_b64": None, "image": None, "file_sig": None, "results": [], "pending": False,
             "error": None, "upload_n": 0}.items():
    S.setdefault(k, v)


def new_photo():
    S.image_b64 = S.image = S.file_sig = None
    S.results = []
    S.upload_n += 1  # fresh uploader, so the old photo isn't counted again


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Options")
    st.toggle("Paste original faces back", value=False, key="opt_faces",
              help="The image model already keeps faces. Turn this on only if a face drifts: each person's "
                   "original face is blended back in, which can look less natural.")
    st.toggle("Change hairstyles and accessories too", value=False, key="opt_hair",
              help="More fun, but the bigger the change, the more faces can drift.")
    st.toggle("Change the background to match", value=False, key="opt_background")
    st.divider()
    if not os.getenv("REPLICATE_API_TOKEN"):
        key = st.text_input("Replicate API token", type="password",
                            help="Or set REPLICATE_API_TOKEN in .env / Streamlit secrets.")
        if key:
            os.environ["REPLICATE_API_TOKEN"] = key
            st.rerun()
    if S.image is not None:
        st.button("Start with a new photo", on_click=new_photo, use_container_width=True)
    st.divider()
    st.subheader("Your usage")
    used = usage.get(S.user)
    for kind, limit in usage.limits.items():
        st.progress(min(used[kind], limit) / limit, text=f"{usage.labels[kind]}: {used[kind]} of {limit}")
    st.caption(f"Signed in as **{S.user}**")
    st.button("Sign out", on_click=_log_out, use_container_width=True)

st.title("History Themes 🎭")
st.caption("Travel through time: dress the people in your photo for another era.")

if S.error:
    st.error(S.error)
    S.error = None

# ---------------------------------------------------------------- generate (runs before drawing the page)
if S.pending:
    S.pending = False
    job = S.job
    try:
        if not quota_left("images"):
            raise RuntimeError(f"you've used all {usage.limits['images']} themed pictures.")
        if not os.getenv("REPLICATE_API_TOKEN"):
            raise RuntimeError("add REPLICATE_API_TOKEN in .env or Streamlit secrets.")
        with st.spinner(f"Dressing everyone for {job['theme']}. This takes about 10-20 seconds..."):
            img = edit_image(S.image_b64, job["prompt"], os.getenv("HISTORYTHEMES_IMAGE_MODEL", IMAGE_MODEL))
        usage.record(S.user, "images")
        faces = None
        if S.get("opt_faces", False):
            try:
                img, faces = keep_faces(S.image, img)
            except Exception:  # never lose a paid-for picture over the face step
                faces = None
        S.results.insert(0, {**job, "image": img, "faces": faces})
    except Exception as e:  # surface API errors in the UI
        S.error = f"That didn't work: {e}"
    st.rerun()


def queue(theme: str, prompt: str):
    S.job = {"theme": theme, "prompt": prompt}
    S.pending = True


# ---------------------------------------------------------------- upload
if S.image is None:
    out_of_uploads = not quota_left("uploads")
    if out_of_uploads:
        st.warning(f"You've used all {usage.limits['uploads']} photo uploads.")
    upload = st.file_uploader("Upload a photo of one or more people", type=["jpg", "jpeg", "png", "webp"],
                              help="Clear, well-lit photos where faces and outfits are visible work best.",
                              disabled=out_of_uploads, key=f"upload_{S.upload_n}")
    if upload and (upload.name, upload.size) != S.file_sig:
        if not quota_left("uploads"):
            st.error(f"You've used all {usage.limits['uploads']} photo uploads.")
        else:
            try:
                S.image_b64, S.image = prepare_image(upload.getvalue())
                S.file_sig = (upload.name, upload.size)
                usage.record(S.user, "uploads")
                st.rerun()
            except Exception:
                st.error("That photo couldn't be opened. Try a JPEG or PNG.")
    st.stop()

# ---------------------------------------------------------------- pick a theme
photo_col, pick_col = st.columns([1, 1.2], gap="large")
with photo_col:
    st.image(S.image, caption="Your photo")

with pick_col:
    st.subheader("Pick a theme")
    group = st.radio("Category", [*THEMES, "Sports"], horizontal=True, label_visibility="collapsed", key="group")
    theme = team = None
    if group == "Sports":
        sport = st.pills("Sport", list(SPORTS), default=list(SPORTS)[0], key="sport", label_visibility="collapsed",
                         format_func=lambda n: f"{SPORTS[n]['emoji']} {n}")
        if sport:
            team = st.selectbox("Team", list(SPORTS[sport]["teams"]), key=f"team_{sport}")
    else:
        names = list(THEMES[group])
        theme = st.pills("Theme", names, default=names[0], key=f"theme_{group}", label_visibility="collapsed",
                         format_func=lambda n: f"{THEMES[group][n][0]} {n}")
    custom = st.text_input("Or describe your own theme",
                           placeholder="e.g. 1940s Hollywood glamour, or superheroes in a comic book")
    if custom.strip():
        label = custom.strip()[:80]
        prompt = theme_prompt(f"{label} costumes", f"hairstyles and accessories that suit {label}",
                              f"a setting that suits {label}", S.opt_hair, S.opt_background)
    elif team:
        label = team
        s_ = SPORTS[sport]
        outfit = (f"the {team} {s_['kit']}, in the team's colours ({s_['teams'][team]}), with the team name "
                  f"'{team}' on the front")
        prompt = theme_prompt(outfit, s_["hair"], s_["background"], S.opt_hair, S.opt_background)
    elif theme:
        label = theme
        _, outfit, hair, background = THEMES[group][theme]
        prompt = theme_prompt(outfit, hair, background, S.opt_hair, S.opt_background)
    else:
        label = prompt = None
    left = quota_left("images")
    st.button(f"Dress them for {label}" if label else "Pick a theme first", type="primary",
              use_container_width=True, disabled=not label or not left, on_click=queue, args=(label, prompt))
    st.caption(f"{left} themed pictures left." if left
               else f"You've used all {usage.limits['images']} themed pictures.")

# ---------------------------------------------------------------- results
if S.results:
    st.divider()
    st.subheader("Before and after")
    for n, r in enumerate(S.results):
        before, after = st.columns(2)
        before.image(S.image, caption="Before")
        after.image(r["image"], caption=f"After: {r['theme']}")
        if r.get("faces"):
            after.caption(f"Kept {r['faces']} original face{'s' if r['faces'] != 1 else ''}.")
        elif r.get("faces") == 0:
            after.caption("Couldn't find faces to keep in this one (side-on or small faces are harder).")
        after.download_button("Download", r["image"], mime="image/jpeg", key=f"dl_{n}_{len(S.results)}",
                              file_name=f"{r['theme'].lower().replace(' ', '-')}-{datetime.now():%Y%m%d-%H%M%S}.jpg")
    st.caption("AI-generated. If a face still looks off, try again: each go is a little different.")
