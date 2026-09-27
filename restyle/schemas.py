"""Structured output schemas. The agent returns these via create_agent(response_format=...)."""

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class PaletteColor(BaseModel):
    name: str = Field(description="Evocative colour name, e.g. 'Olive grove'")
    hex: str = Field(description="Hex code in the form #RRGGBB")
    role: str = Field(description="One of: main, accent, wood, metal, textile (furniture and decor only)")

    @field_validator("hex", mode="before")
    @classmethod
    def valid_hex(cls, v):
        v = str(v).strip()
        if not v.startswith("#"):
            v = "#" + v
        return v if _HEX.match(v) else "#999999"


class FurnitureItem(BaseModel):
    id: str = Field(description="Short unique slug, e.g. 'boucle-armchair'")
    name: str = Field(description="Name of the piece, e.g. 'Boucle armchair'")
    category: str = Field(
        description="seating, tables, storage, lighting, appliances, textiles, decor, plants, art or other"
    )
    description: str = Field(description="One sentence: material, colour, shape of the specific piece")
    placement: str = Field(description="Where in the room it goes, clear of windows and doors")
    x: Optional[float] = Field(
        default=None,
        description="Horizontal position in THE PHOTO where the item would sit, 0-100 (percent from left). Null if off-frame.",
    )
    y: Optional[float] = Field(
        default=None,
        description="Vertical position in THE PHOTO where the item would sit, 0-100 (percent from top). Null if off-frame.",
    )
    price_low: float = Field(description="Low end of a realistic retail price in the requested currency")
    price_high: float = Field(description="High end of a realistic retail price in the requested currency")
    priority: Literal["essential", "nice"] = Field(description="'essential' or 'nice' (optional extra)")

    @field_validator("x", "y", mode="before")
    @classmethod
    def clamp_pct(cls, v):
        if v is None:
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return max(2.0, min(98.0, v))


class DesignConcept(BaseModel):
    name: str = Field(description="2-3 word concept name")
    tagline: str = Field(description="One evocative line")
    summary: str = Field(description="2-3 sentences describing the look and feel")
    palette: list[PaletteColor] = Field(description="Exactly 5 colours")
    materials: list[str] = Field(description="3-6 key materials and finishes for the furniture and decor")
    items: list[FurnitureItem] = Field(description="6-9 furniture and decor pieces")
    lighting: str = Field(description="One or two sentences on the lighting plan, using lamps and existing light points")
    tips: list[str] = Field(description="3-4 practical layout or styling tips specific to this room")


class RoomAnalysis(BaseModel):
    room_type: str = Field(description="e.g. 'Living room', 'Main bedroom'")
    summary: str = Field(description="1-2 sentences describing the room as it is now")
    keep: list[str] = Field(description="Existing features worth keeping")
    constraints: list[str] = Field(description="Things the design must work around")
    fixed_features: list[str] = Field(
        default_factory=list,
        description="EVERY window, door, shutter, blind, window frame, built-in and other fixed feature visible "
                    "in the photo, one per entry, each with its location and look so it can be recognised, "
                    "e.g. 'tall window with white plantation shutters on the left wall'",
    )


class DesignResult(BaseModel):
    analysis: RoomAnalysis
    concepts: list[DesignConcept] = Field(description="Exactly 3 clearly different concepts")
