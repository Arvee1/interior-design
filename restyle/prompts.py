"""System prompt and per-task prompt builders."""

import json
from dataclasses import dataclass, field

SYSTEM_PROMPT = """You are a senior interior designer who redesigns real rooms from photos.

What you may change: ONLY movable furniture, appliances, lamps, rugs, soft furnishings, plants,
art and decor. Everything built into the room stays exactly as it is: windows, doors, walls
(including their colour and finish), floors, ceilings, built-in joinery and fixed light points.
Never remove, move, cover up, resize or replace a window or door, and never place a piece where it
would block one. Palettes, materials and tips apply to the furniture and decor only; do not suggest
painting, wallpaper, new flooring, renovations or building work.

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
    store: str = ""              # e.g. "Harvey Norman": pick pieces this retailer stocks

    def as_text(self) -> str:
        lines = [
            f"Budget level: {self.budget} - {BUDGET_WORDS.get(self.budget, self.budget)}.",
            f"Currency for all prices: {self.currency}.",
        ]
        if self.styles:
            lines.append(f"Styles the owner likes: {', '.join(self.styles)}.")
        if self.store:
            lines.append(
                f"Choose pieces that {self.store} (Australia) is likely to stock, priced like their range. "
                "Give each item a plain, searchable product name as a shopper would type it, e.g. "
                "'3 seater fabric sofa' or 'round marble coffee table', so it can be found in their catalogue."
            )
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

Revise the concept to deliver the change while keeping it coherent and fitting this room. Even if the
request asks for it, only change furniture, appliances and decor: keep windows, doors, walls, floors
and ceilings as they are, and say so in the tips if the owner asked for building work. Keep items
that still work (with their ids); replace or add others as needed; keep 6-9 items. Rename the concept
if its character changes. Update x/y so every item is pinned where it would sit in the photo."""


def new_concept_prompt(analysis: dict, prefs: Preferences, existing_names: list[str]) -> str:
    return f"""The attached photo shows a room the owner wants to redesign.

Room analysis: {json.dumps(analysis)}
{prefs.as_text()}

The owner has already seen these concepts: {', '.join(existing_names)}.
Propose ONE new concept that feels clearly different from all of them."""


def after_image_prompt(concept: dict) -> str:
    """Edit instruction for the image model: restyle the photo as this concept.

    The keep-the-room rule goes first so trimming a long furniture list can never cut it off."""
    rule = (
        "Change ONLY the movable furniture, appliances, lamps, rugs, soft furnishings, plants, art and decor. "
        "Keep every window and door exactly as it is: same position, size, shape, frame and glazing, fully "
        "visible and not blocked or covered. Also keep the walls and their colour, the floor, the ceiling, "
        "built-in fixtures and the camera angle unchanged. "
    )
    palette = ", ".join(c["name"] for c in concept["palette"])
    items = "; ".join(f'{i["name"]} ({i["description"]}) {i["placement"]}' for i in concept["items"])
    design = (
        f"Restyle the furniture and decor in the '{concept['name']}' look: {concept['summary']} "
        f"Furniture and decor colours: {palette}. Materials: {', '.join(concept['materials'])}. "
        f"Furnish it with: {items}. "
    )
    ending = "Photorealistic interior photograph, natural light, no people, no text."
    return rule + design[:2000 - len(rule) - len(ending)] + ending
