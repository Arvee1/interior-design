# Restyle

Upload a photo of a room in Streamlit and get three interior design concepts. Each concept has a colour palette, materials, lighting, and a costed furniture list with every piece pinned onto your photo. You can refine each concept by typing changes, using quick adjustments or sliders, keeping, swapping or removing pieces, and undoing.

It is built on LangChain `create_agent`. The agent has:

- **Tools:** a style-guide lookup and a budget check.
- **Middleware:** `ModelRetryMiddleware` for transient API errors.
- **Structured output:** Pydantic schemas.

## Setup (VS Code)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # then put your REPLICATE_API_TOKEN in .env
streamlit run app.py
```

Streamlit opens at http://localhost:8501. Use Python 3.10 or later.

## Project layout

```
app.py                 Streamlit UI (upload, concepts, refine panel, shopping list)
restyle/
  agent.py             create_agent harness + image preparation (no Streamlit dependency)
  schemas.py           Pydantic models the agent must return
  prompts.py           System prompt and prompt builders
  tools.py             get_style_guide and check_budget tools (edit STYLE_GUIDES freely)
  render.py            Photo pins, palette swatches, CSV export
.streamlit/config.toml Theme and 50 MB upload limit
```

## How it works

1. The uploaded photo is orientation-corrected, downscaled to a maximum of 1568 px, and sent as a base64 image content block.
2. The **generate** step asks the agent for a `DesignResult`, which contains the room analysis and three concepts. While it works, the agent calls `get_style_guide` for the styles it uses and `check_budget` on each concept's total.
3. The **refine** step sends the current concept, the IDs of items you've kept, and your instruction back with the photo. The agent returns a revised `DesignConcept`. If the model drops a kept item, the app puts it back.
4. The app holds the concepts and undo history in `st.session_state`. Each agent call is stateless, so there is no checkpointer to manage.

## Changing the model

Set `RESTYLE_MODEL` in `.env` to any LangChain `provider:model` string that supports images. Examples are `openai:gpt-5.5` or `google_genai:gemini-3.6-flash`. You also need to install that provider's package (`langchain-openai`, `langchain-google-genai`) and set its API key.

## Deploying to Streamlit Community Cloud

Push the repo to GitHub, excluding `.env`. Create the app in Streamlit Cloud and add this under **Secrets**:

```toml
REPLICATE_API_TOKEN = "r8_..."
ALLOWED_USERS = ["arvee"]          # usernames that can sign in (not case-sensitive)
SERPER_API_KEY = "..."             # optional: match pieces to Harvey Norman products
```

## Harvey Norman products

With `SERPER_API_KEY` set (free key from serper.dev), the design agent picks pieces Harvey Norman is likely to stock. The app then searches Google Shopping in Australia for each one and attaches the closest Harvey Norman listing: product name, photo, price and link. The real price replaces the estimate, and the CSV export includes the product and link. Turn it off with the sidebar toggle. Harvey Norman's own site blocks automated access, so the app never contacts it directly. A generate uses about 20 searches; repeat searches are cached.

## Access and limits

Only usernames listed in `ALLOWED_USERS` can use the app. For local runs, put them in `.env` as `ALLOWED_USERS=arvee,guest`. Each user can upload 5 photos, generate 5 after pictures and make 10 design changes. The sidebar shows how many of each they've used. Change the numbers in `restyle/usage.py`.

Counts are saved in `.usage.json`. On Streamlit Community Cloud that file resets when the app is rebooted or redeployed.

The app reads the key from Streamlit secrets automatically. If no key is found anywhere, it shows a key field in the sidebar.

## Tracing (optional)

To trace each agent run, including tool calls, in LangSmith, add these to `.env`:

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
```

## Before and after pictures

In the **Before & after** tab, click **Generate after picture** to restyle your photo as the selected concept. This uses an image-editing model on Replicate (`black-forest-labs/flux-kontext-pro` by default; change it with `RESTYLE_IMAGE_MODEL`) and needs `REPLICATE_API_TOKEN`. Each picture costs a few cents. Refining or removing items clears the picture so it never shows an outdated design; undo brings it back.

The after picture is an AI impression of the concept, so it won't match every listed piece exactly.

# History Themes (second app)

`historythemes.py` is a separate Streamlit app in the same repo. Upload a photo of one or more people, pick a theme, and it redraws everyone's outfits for that era using Google's Nano Banana 2 on Replicate, which keeps people recognisable. Every hat, accessory or modern item that doesn't fit the theme is swapped or removed.

- **Themes:** Decades (1920s to Y2K), Ancient world (Roman, Greek, Egyptian, Stone Age), Historical eras (Viking to Wild West) and Just for fun (Pirates, Steampunk, Space Age, Year 2200, Fairytale Royalty), plus Sports, where you pick a sport and a team (all 30 NBA teams, all 17 NRL teams, State of Origin and all 18 AFL teams), or type your own. Edit `THEMES` and `SPORTS` at the top of the file to add more.
- **Face fallback:** if a face still drifts, turn on "Paste original faces back". `restyle/faces.py` finds each face in your original photo (OpenCV's YuNet detector, `restyle/models/`, MIT licence), lines it up with the same person in the new picture and blends it in. Off by default because pasted faces can look less natural.
- **Options:** also change hairstyles and accessories (off by default, since big changes make faces drift), and swap the background for a matching scene (off by default).
- **Access and limits:** same `ALLOWED_USERS` sign-in as Restyle. Each user can upload 5 photos and create 10 themed pictures, tracked in `.historythemes_usage.json` and shown in the sidebar.
- **Run locally:** `streamlit run historythemes.py`
- **Deploy:** in Streamlit Community Cloud, create a second app from this repo with main file `historythemes.py`, and add `REPLICATE_API_TOKEN` and `ALLOWED_USERS` to its Secrets.
- Optional: set `HISTORYTHEMES_IMAGE_MODEL` to try another model, such as `google/nano-banana-pro` (higher quality, slower) or `black-forest-labs/flux-kontext-pro`.

# Face Clips (third app)

`faceclips.py` swaps a person from your photo into a short video clip. They keep doing and saying the same thing; only the person changes.

1. **Load a video:** paste a YouTube link (fetched with `yt-dlp`; only works when run locally, because YouTube blocks hosted sites) or upload an MP4/MOV/WebM file.
2. **Pick the part to use:** choose a start time and length, up to 30 seconds. The clip is cut and scaled to 720p with a bundled `ffmpeg` (`imageio-ffmpeg`), so no system install is needed.
3. **Upload a photo and pick the person:** if the photo has several people, you choose which one to put in.
4. **Swap**, one of three ways:
   - **Whole person:** `wan-video/wan-2.2-animate-replace` replaces a person and keeps their movements, expressions, mouth movements and the original sound. When the clip has several people, you pick who: the app follows each face through the clip (`restyle/people.py`), sends only that person's part of the frame to the model and pastes the result back with soft edges, so the others are untouched. The photo's background is removed first (`bria/remove-background`, about US$0.02) so the model gets a clean cut-out. About US$0.05 per second at 720p, US$0.02 at 480p.
   - **Whole person, you say who:** `kwaivgi/kling-v3-omni-video` edits the clip from a description such as "the man driving the car". Use it when several people are in shot. Clips must be 3 to 10 seconds. About US$0.17 per second.
   - **Head only (face and hair):** Kling 3.0 Omni (the same Replicate model as "say who") is told to replace only the head (face, hair and head shape) and keep the body, clothes and movements. Uses the same pick-who-to-replace cut-out as the whole-person swap, and the photo's background is removed first. Clips must be 3 to 10 seconds. About US$0.17 per second.
   - **Face only:** a roop-based face swap (`okaris/roop`, then `arabyai-replicate/roop_face_swap` if that fails). Set `FACECLIPS_MODEL` to force one.

- **Sound:** if a model returns a silent video, the original clip's audio is added back.
- **Limits:** 5 video loads and 5 swaps per user, in `.faceclips_usage.json`, shown in the sidebar. Same `ALLOWED_USERS` sign-in as the other apps.
- **Consent:** you must tick a box confirming everyone whose picture is used has agreed.
- **Run locally:** `streamlit run faceclips.py`. **Deploy:** create another Streamlit Cloud app from this repo with main file `faceclips.py`, and add `REPLICATE_API_TOKEN` and `ALLOWED_USERS` to its Secrets.
