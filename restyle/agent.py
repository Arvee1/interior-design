"""The design agent: a LangChain create_agent harness with tools, retry middleware
and structured output. Streamlit-independent, so you can reuse it elsewhere."""

import base64
import io
import json
import os
import re
from functools import lru_cache

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRetryMiddleware
from PIL import Image, ImageOps
from pydantic import ValidationError

from .prompts import SYSTEM_PROMPT, Preferences, generate_prompt, new_concept_prompt, refine_prompt
from .schemas import DesignConcept, DesignResult
from .tools import BUDGET_BANDS_AUD, FX_TO_AUD, STYLE_GUIDES, check_budget, get_style_guide

# "replicate:<owner>/<model>" runs on Replicate (needs REPLICATE_API_TOKEN); any other
# value is a LangChain "provider:model" string (e.g. "anthropic:claude-sonnet-5").
DEFAULT_MODEL = "replicate:anthropic/claude-sonnet-5"
REPLICATE_MAX_TOKENS = 16000  # three full concepts can exceed Replicate's 8192 default
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


def _replicate_context(prefs: Preferences) -> str:
    """Replicate's Claude models can't call tools, so hand over the tools' knowledge up front."""
    guides = "\n\n".join(
        f"## {name.title()}\n" + "\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in g.items())
        for name, g in STYLE_GUIDES.items()
    )
    band, rate = BUDGET_BANDS_AUD.get(prefs.budget), FX_TO_AUD.get(prefs.currency.upper())
    budget = (
        f"A typical {prefs.budget} budget room costs {band[0] / rate:,.0f}-{band[1] / rate:,.0f} {prefs.currency} "
        "in total. Keep each concept's total near that range."
        if band and rate else "Use your judgement on the budget."
    )
    return f"Style guides (use these instead of the get_style_guide tool):\n\n{guides}\n\nBudget: {budget}"


def _extract_json(text: str) -> str:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


class RoomDesigner:
    def __init__(self, model: str | None = None):
        self.model = model or os.getenv("RESTYLE_MODEL", DEFAULT_MODEL)

    @property
    def provider(self) -> str:
        return self.model.split(":", 1)[0]

    def _run(self, output: str, prompt: str, image_b64: str, prefs: Preferences):
        if self.provider == "replicate":
            return self._run_replicate(output, prompt, image_b64, prefs)
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

    def _run_replicate(self, output: str, prompt: str, image_b64: str, prefs: Preferences):
        import replicate

        schema = DesignResult if output == "result" else DesignConcept
        full_prompt = (
            f"{prompt}\n\n{_replicate_context(prefs)}\n\n"
            "Reply with ONLY a JSON object (no prose, no code fences) that matches this JSON schema:\n"
            f"{json.dumps(schema.model_json_schema())}"
        )
        error, retry_note = None, ""
        for _ in range(3):  # retry on API errors or output that doesn't fit the schema
            try:
                chunks = replicate.run(self.model.split(":", 1)[1], input={
                    "prompt": full_prompt + retry_note,
                    "system_prompt": SYSTEM_PROMPT,
                    "image": io.BytesIO(base64.b64decode(image_b64)),
                    "max_tokens": REPLICATE_MAX_TOKENS,
                })
                return schema.model_validate_json(_extract_json("".join(str(c) for c in chunks)))
            except ValidationError as e:
                error = str(e)[:500]
                retry_note = f"\n\nYour previous reply didn't match the schema ({error}). Return valid JSON only."
            except replicate.exceptions.ReplicateError as e:
                error = str(e)[:500]
        raise RuntimeError(f"Replicate model {self.model} failed after 3 attempts: {error}")

    def generate(self, image_b64: str, prefs: Preferences) -> DesignResult:
        return self._run("result", generate_prompt(prefs), image_b64, prefs)

    def refine(self, image_b64: str, prefs: Preferences, analysis: dict, concept: dict,
               locked_ids: list[str], instruction: str) -> DesignConcept:
        return self._run("concept", refine_prompt(analysis, prefs, concept, locked_ids, instruction), image_b64, prefs)

    def new_concept(self, image_b64: str, prefs: Preferences, analysis: dict,
                    existing_names: list[str]) -> DesignConcept:
        return self._run("concept", new_concept_prompt(analysis, prefs, existing_names), image_b64, prefs)
