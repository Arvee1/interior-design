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
cp .env.example .env               # then put your ANTHROPIC_API_KEY in .env
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
ANTHROPIC_API_KEY = "sk-ant-..."
```

The app reads the key from Streamlit secrets automatically. If no key is found anywhere, it shows a key field in the sidebar.

## Tracing (optional)

To trace each agent run, including tool calls, in LangSmith, add these to `.env`:

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
```

## Limitations

The app suggests designs but does not generate "after" images. If you want a render, add an image-generation tool to `restyle/tools.py` and display its output in `app.py`.
