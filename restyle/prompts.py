"""System prompt and per-task prompt builders."""

import json
from dataclasses import dataclass, field

SYSTEM_PROMPT = """You are a senior interior designer who redesigns real rooms from photos.

How you work:
- Study the photo closely: room type, approximate size and proportions, windows and natural light,
  flooring, fixed architectural features, and existing pieces worth keeping.
- Use the get_style_guide tool for each style a concept is based on.
- Price every item realistically for the requested currency and budget level, then use the
  check_budget tool on each concept's total and adjust if it is well over.
- Every piece must physically fit the space you can see.
- For each item's x and y, give the point IN THE PHOTO (percent from left, percent from top) where
  it would stand or hang, so it can be pinned on the photo. Floor items go on the visible floor.
  Use null only if the spot is out of frame.
- Give each item a short unique slug id."""

BUDGET_WORDS = {
    "low": "thrifty (second-hand, flat-pack, high street)",
    "mid": "mid-range (quality high street and mid-tier brands)",
    "high": "investment (designer and heirloom pieces)",
}


@dataclass
class Preferences:
    styles: list[str] = field(default_factory=list)
    budget: str = "mid"          # low | mid | high
    currency: str = "AUD"
    notes: str = ""

    def as_text(self) -> str:
        lines = [
            f"Budget level: {self.budget} - {BUDGET_WORDS.get(self.budget, self.budget)}.",
            f"Currency for all prices: {self.currency}.",
        ]
        if self.styles:
            lines.append(f"Styles the owner likes: {', '.join(self.styles)}.")
        if self.notes.strip():
            lines.append(f"Owner's notes: {self.notes.strip()[:1500]}")
        return "\n".join(lines)


def generate_prompt(prefs: Preferences) -> str:
    lean = " Lean on the styles the owner likes, but make the three feel distinct." if prefs.styles else ""
    return f"""The attached photo shows a room the owner wants to redesign.

{prefs.as_text()}

Analyse the room, then propose 3 genuinely different design concepts for this exact room.{lean}"""


def refine_prompt(analysis: dict, prefs: Preferences, concept: dict, locked_ids: list[str], instruction: str) -> str:
    clean = {k: v for k, v in concept.items() if k != "after_image"}
    clean["items"] = [{k: v for k, v in i.items() if k != "locked"} for i in concept["items"]]
    locked = (
        f"\nThe owner has LOCKED these items; keep them exactly as they are (same id, name, description, position): {', '.join(locked_ids)}.\n"
        if locked_ids else ""
    )
    return f"""You are refining a design concept for the room in the attached photo.

Room analysis: {json.dumps(analysis)}
{prefs.as_text()}

Current concept:
{json.dumps(clean)}
{locked}
Requested change: {instruction}

Revise the concept to deliver the change while keeping it coherent and fitting this room. Keep items
that still work (with their ids); replace or add others as needed; keep 6-9 items. Rename the concept
if its character changes. Update x/y so every item is pinned where it would sit in the photo."""


def new_concept_prompt(analysis: dict, prefs: Preferences, existing_names: list[str]) -> str:
    return f"""The attached photo shows a room the owner wants to redesign.

Room analysis: {json.dumps(analysis)}
{prefs.as_text()}

The owner has already seen these concepts: {', '.join(existing_names)}.
Propose ONE new concept that feels clearly different from all of them."""


def after_image_prompt(concept: dict) -> str:
    """Edit instruction for the image model: restyle the photo as this concept."""
    palette = ", ".join(c["name"] for c in concept["palette"])
    items = "; ".join(f'{i["name"]} ({i["description"]}) {i["placement"]}' for i in concept["items"])
    prompt = (
        f"Redesign this room as a finished, professionally styled interior in the '{concept['name']}' look: "
        f"{concept['summary']} Colour palette: {palette}. Materials: {', '.join(concept['materials'])}. "
        f"Lighting: {concept['lighting']} Furnish it with: {items}. "
        "Keep the exact same room: camera angle, walls, windows, doors, ceiling and floor area stay where "
        "they are. Photorealistic interior photograph, natural light, no people, no text."
    )
    return prompt[:2000]
