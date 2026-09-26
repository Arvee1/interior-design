"""The design agent: a LangChain create_agent harness with tools, retry middleware
and structured output. Streamlit-independent, so you can reuse it elsewhere."""

import base64
import io
import os
from functools import lru_cache

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware
from PIL import Image, ImageOps

from .prompts import SYSTEM_PROMPT, Preferences, generate_prompt, new_concept_prompt, refine_prompt
from .schemas import DesignConcept, DesignResult
from .tools import check_budget, get_style_guide

DEFAULT_MODEL = "anthropic:claude-sonnet-5"
MAX_IMAGE_SIDE = 1568  # larger images are downscaled by the model anyway; this saves tokens


def prepare_image(raw: bytes) -> tuple[str, Image.Image]:
    """Fix orientation, downscale and re-encode as JPEG. Returns (base64, PIL image)."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii"), img


@lru_cache(maxsize=4)
def _build_agent(model: str, output: str):
    schema = DesignResult if output == "result" else DesignConcept
    return create_agent(
        model=model,
        tools=[get_style_guide, check_budget],
        system_prompt=SYSTEM_PROMPT,
        response_format=schema,
        middleware=[ModelRetryMiddleware(max_retries=3)],
    )


class RoomDesigner:
    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("RESTYLE_MODEL", DEFAULT_MODEL)

    def _run(self, output: str, prompt: str, image_b64: str):
        agent = _build_agent(self.model, output)
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image", "base64": image_b64, "mime_type": "image/jpeg"},
            ],
        }
        result = agent.invoke({"messages": [message]})
        return result["structured_response"]

    def generate(self, image_b64: str, prefs: Preferences) -> DesignResult:
        return self._run("result", generate_prompt(prefs), image_b64)

    def refine(self, image_b64: str, prefs: Preferences, analysis: dict, concept: dict,
               locked_ids: list[str], instruction: str) -> DesignConcept:
        return self._run("concept", refine_prompt(analysis, prefs, concept, locked_ids, instruction), image_b64)

    def new_concept(self, image_b64: str, prefs: Preferences, analysis: dict,
                    existing_names: list[str]) -> DesignConcept:
        return self._run("concept", new_concept_prompt(analysis, prefs, existing_names), image_b64)
