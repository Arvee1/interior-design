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
.streamlit/config.toml Theme and 20 MB upload limit
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

`historythemes.py` is a separate Streamlit app in the same repo. Upload a photo of one or more people, pick a theme, and it redraws everyone's outfits for that era using the same Replicate image model (FLUX Kontext Pro) as Restyle's after pictures.

- **Themes:** Decades (1920s to Y2K), Ancient world (Roman, Greek, Egyptian, Stone Age), Historical eras (Viking to Wild West) and Just for fun (Pirates, Steampunk, Space Age, Year 2200, Fairytale Royalty), or type your own. Edit `THEMES` at the top of the file to add more.
- **Real faces kept:** after each picture is made, `restyle/faces.py` finds every face in your original photo (with OpenCV's YuNet detector, `restyle/models/`, MIT licence), lines it up with the same person in the new picture and blends it back in, so people still look like themselves. Turn it off with "Keep everyone's real faces".
- **Options:** also change hairstyles and accessories (off by default, since big changes make faces drift), and swap the background for a matching scene (off by default).
- **Access and limits:** same `ALLOWED_USERS` sign-in as Restyle. Each user can upload 5 photos and create 10 themed pictures, tracked in `.historythemes_usage.json` and shown in the sidebar.
- **Run locally:** `streamlit run historythemes.py`
- **Deploy:** in Streamlit Community Cloud, create a second app from this repo with main file `historythemes.py`, and add `REPLICATE_API_TOKEN` and `ALLOWED_USERS` to its Secrets.
- Optional: set `HISTORYTHEMES_IMAGE_MODEL` (for example `black-forest-labs/flux-kontext-max`) for a stronger image model.
