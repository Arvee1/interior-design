"""Tools the design agent can call while it works.

Edit STYLE_GUIDES to encode your own design knowledge; the agent looks these up
instead of relying only on what the model already knows.
"""

from langchain.tools import tool

STYLE_GUIDES: dict[str, dict] = {
    "japandi": {
        "principles": "Japanese restraint meets Scandinavian warmth. Low profiles, negative space, handcrafted imperfection.",
        "palette": "Warm whites, oatmeal, clay, charcoal, muted sage",
        "materials": "Light oak, ash, rattan, linen, paper, stoneware",
        "signature_pieces": "Low platform sofa, round paper pendant, slatted timber bench, ceramic vessels",
        "avoid": "Clutter, glossy finishes, bright saturated colour",
    },
    "mid-century": {
        "principles": "Clean lines, organic curves, tapered legs, function first.",
        "palette": "Walnut brown, mustard, teal, burnt orange, off-white",
        "materials": "Walnut and teak, moulded plywood, brass, wool",
        "signature_pieces": "Low credenza, lounge chair with ottoman, sputnik or globe pendant, tulip table",
        "avoid": "Heavy ornate detailing, overstuffed furniture",
    },
    "scandinavian": {
        "principles": "Light, airy, practical. Hygge through texture rather than colour.",
        "palette": "White, pale grey, soft blush, pale timber, black accents",
        "materials": "Birch, pale oak, wool, sheepskin, cotton",
        "signature_pieces": "Wishbone-style chairs, wall-mounted shelving, layered throws, simple pendant",
        "avoid": "Dark heavy furniture, visual clutter",
    },
    "coastal": {
        "principles": "Relaxed and breezy, lots of natural light, casual comfort.",
        "palette": "Sand, crisp white, sea-glass green, soft blue, driftwood",
        "materials": "Whitewashed timber, jute, rattan, linen, cane",
        "signature_pieces": "Slipcovered sofa, jute rug, rattan pendant, cane-front cabinet",
        "avoid": "Kitschy nautical motifs, heavy velvet",
    },
    "industrial": {
        "principles": "Raw, utilitarian, celebrates structure and patina.",
        "palette": "Charcoal, rust, concrete grey, cognac leather, black",
        "materials": "Blackened steel, reclaimed timber, leather, concrete, exposed brick",
        "signature_pieces": "Leather chesterfield or club chair, steel-framed shelving, factory pendants",
        "avoid": "Pastels, delicate florals",
    },
    "warm minimal": {
        "principles": "Few pieces, each considered. Softened minimalism with warmth and texture.",
        "palette": "Warm white, mushroom, tobacco, sand, bronze",
        "materials": "Limewash, travertine, boucle, walnut, linen",
        "signature_pieces": "Curved boucle sofa, travertine coffee table, sculptural floor lamp",
        "avoid": "Stark cool white, too many small objects",
    },
    "modern farmhouse": {
        "principles": "Rustic comfort with clean modern lines.",
        "palette": "White, warm grey, black, natural timber, sage",
        "materials": "Shiplap, reclaimed wood, matte black metal, linen, galvanised steel",
        "signature_pieces": "Farmhouse dining table, slipcovered sofa, black metal pendants, open shelving",
        "avoid": "Excess faux-rustic signage",
    },
    "art deco": {
        "principles": "Glamour, geometry and symmetry.",
        "palette": "Emerald, navy, black, gold, blush",
        "materials": "Velvet, brass, lacquer, marble, mirrored glass",
        "signature_pieces": "Scalloped velvet sofa, fluted side tables, geometric rug, globe sconces",
        "avoid": "Rustic finishes, casual clutter",
    },
    "boho": {
        "principles": "Collected, layered, free-spirited; mix of cultures and textures.",
        "palette": "Terracotta, ochre, cream, olive, rust",
        "materials": "Rattan, macrame, kilim, jute, carved wood",
        "signature_pieces": "Floor cushions, layered rugs, hanging plants, peacock or rattan chair",
        "avoid": "Matching sets, sterile surfaces",
    },
    "maximalist": {
        "principles": "More is more, curated with confidence: pattern on pattern, colour drenching.",
        "palette": "Jewel tones: sapphire, ruby, emerald, with plenty of contrast",
        "materials": "Velvet, patterned wallpaper, lacquer, brass, fringing",
        "signature_pieces": "Gallery wall, statement velvet sofa, patterned rug, bold lighting",
        "avoid": "Timidity; beige-on-beige",
    },
    "contemporary luxe": {
        "principles": "Polished, tailored, hotel-like calm.",
        "palette": "Greige, ivory, smoke, champagne, deep ink",
        "materials": "Marble, brushed brass, smoked glass, silk-blend textiles",
        "signature_pieces": "Tailored modular sofa, marble side tables, statement chandelier",
        "avoid": "Rustic or distressed finishes",
    },
    "mediterranean": {
        "principles": "Sun-washed, earthy and tactile; indoor-outdoor living.",
        "palette": "Chalk white, terracotta, olive, ochre, Aegean blue",
        "materials": "Lime plaster, terracotta, rough timber, wrought iron, linen",
        "signature_pieces": "Built-in style banquette, terracotta pots, rush-seat chairs, arched mirror",
        "avoid": "High-gloss surfaces, cold greys",
    },
}

# Rough whole-room furnishing budgets in AUD, per budget level.
BUDGET_BANDS_AUD = {"low": (1500, 5000), "mid": (5000, 15000), "high": (15000, 50000)}

# Approximate FX to AUD. Update occasionally; only used for rough guidance.
FX_TO_AUD = {"AUD": 1.0, "USD": 1.52, "GBP": 1.95, "EUR": 1.66, "NZD": 0.91, "CAD": 1.10}


@tool
def get_style_guide(style: str) -> str:
    """Look up the design principles, palette, materials, signature pieces and things to
    avoid for an interior style (e.g. 'japandi', 'mid-century', 'coastal'). Call this for
    each style you base a concept on."""
    key = style.strip().lower().replace("mid century", "mid-century")
    guide = STYLE_GUIDES.get(key)
    if not guide:
        return f"No guide for '{style}'. Known styles: {', '.join(STYLE_GUIDES)}. Use your own expertise."
    return "\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in guide.items())


@tool
def check_budget(total_low: float, total_high: float, budget_level: str, currency: str) -> str:
    """Check whether a concept's total furniture cost fits the owner's budget level.
    budget_level is 'low', 'mid' or 'high'. Call this after pricing a concept's items."""
    band = BUDGET_BANDS_AUD.get(budget_level.lower())
    rate = FX_TO_AUD.get(currency.upper())
    if not band or not rate:
        return "Unknown budget level or currency; use your judgement."
    lo, hi = band[0] / rate, band[1] / rate
    mid_point = (total_low + total_high) / 2
    if mid_point > hi:
        return f"Over budget: typical {budget_level} room is {lo:,.0f}-{hi:,.0f} {currency}. Swap some pieces for cheaper options."
    if mid_point < lo * 0.6:
        return f"Well under budget ({lo:,.0f}-{hi:,.0f} {currency}). Fine if intentional; could upgrade a hero piece."
    return f"Within the typical {budget_level} range of {lo:,.0f}-{hi:,.0f} {currency}."
