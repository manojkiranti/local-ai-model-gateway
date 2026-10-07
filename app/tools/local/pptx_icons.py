"""The icon vocabulary create_pptx's infographic layouts draw from.

Icons are Lucide (ISC licence — `assets/icons/LICENSE`), the SAME set the
frontend already ships as `lucide-react`, so a deck preview can render the very
icon by name instead of guessing at a lookalike. They are pre-rendered to PNG
at dev time by `scripts/build_pptx_icons.py` and committed under
`assets/icons/{red,white}/<name>.png`, because python-pptx embeds raster images
only (no SVG) and rendering SVG at request time would pull cairo into the API
image. Nothing is fetched at runtime.

The model names an icon by string, and models invent names freely. An unknown
name is DROPPED (the card/step/column renders without an icon), never an error:
refusing the whole deck over a decorative glyph would send the model into a
retry loop for no gain. `ALIASES` maps the obvious plain-English guesses onto
real names so the common case still gets an icon.
"""

from __future__ import annotations

from pathlib import Path

ICON_DIR = Path(__file__).parent / "assets" / "icons"

# Curated for banking/business decks. Adding one: append it here, re-run
# scripts/build_pptx_icons.py, and add it to the frontend's deck-icons.ts —
# the preview renders icons by these exact names.
ICON_NAMES: tuple[str, ...] = (
    "trending-up", "trending-down", "chart-column", "chart-pie", "chart-line",
    "target", "shield-check", "shield-alert", "lock", "key-round",
    "users", "user", "user-check", "handshake", "building-2",
    "landmark", "store", "banknote", "wallet", "credit-card",
    "piggy-bank", "coins", "hand-coins", "circle-dollar-sign", "percent",
    "receipt", "calculator", "calendar", "clock", "circle-check",
    "triangle-alert", "circle-x", "lightbulb", "rocket", "globe",
    "smartphone", "laptop", "server", "database", "cloud",
    "file-text", "clipboard-check", "briefcase", "scale", "gavel",
    "award", "star", "heart-handshake", "leaf", "map-pin",
    "phone", "mail", "message-square", "headphones", "settings",
    "zap", "search", "eye", "refresh-cw", "layers",
    "puzzle", "graduation-cap", "truck", "package", "flag",
    "megaphone", "thumbs-up", "gift", "house", "link",
    "cpu", "bot", "sparkles", "list-checks", "workflow",
)
_NAMES = frozenset(ICON_NAMES)

ALIASES: dict[str, str] = {
    "growth": "trending-up", "increase": "trending-up", "up": "trending-up",
    "decline": "trending-down", "decrease": "trending-down", "down": "trending-down",
    "chart": "chart-column", "bar-chart": "chart-column", "analytics": "chart-column",
    "pie-chart": "chart-pie", "goal": "target", "objective": "target",
    "security": "shield-check", "secure": "shield-check", "compliance": "shield-check",
    "risk": "shield-alert", "warning": "triangle-alert", "alert": "triangle-alert",
    "key": "key-round", "team": "users", "people": "users", "customers": "users",
    "customer": "user", "person": "user", "partnership": "handshake", "deal": "handshake",
    "building": "building-2", "office": "building-2", "branch": "building-2",
    "bank": "landmark", "money": "banknote", "cash": "banknote", "card": "credit-card",
    "savings": "piggy-bank", "deposit": "piggy-bank", "loan": "hand-coins",
    "dollar": "circle-dollar-sign", "revenue": "circle-dollar-sign", "profit": "circle-dollar-sign",
    "interest": "percent", "rate": "percent", "invoice": "receipt", "date": "calendar",
    "time": "clock", "deadline": "clock", "check": "circle-check", "done": "circle-check",
    "success": "circle-check", "error": "circle-x", "fail": "circle-x", "idea": "lightbulb",
    "innovation": "lightbulb", "launch": "rocket", "world": "globe", "international": "globe",
    "mobile": "smartphone", "app": "smartphone", "computer": "laptop", "data": "database",
    "document": "file-text", "report": "file-text", "policy": "file-text",
    "checklist": "clipboard-check", "business": "briefcase", "law": "scale", "legal": "scale",
    "regulation": "gavel", "achievement": "award", "quality": "star", "csr": "heart-handshake",
    "care": "heart-handshake", "green": "leaf", "sustainability": "leaf", "location": "map-pin",
    "email": "mail", "chat": "message-square", "support": "headphones", "service": "headphones",
    "process": "workflow", "operations": "settings", "energy": "zap", "fast": "zap",
    "research": "search", "visibility": "eye", "monitoring": "eye", "update": "refresh-cw",
    "integration": "puzzle", "training": "graduation-cap", "education": "graduation-cap",
    "delivery": "truck", "product": "package", "milestone": "flag", "marketing": "megaphone",
    "approval": "thumbs-up", "reward": "gift", "home": "house", "technology": "cpu",
    "ai": "bot", "automation": "bot", "new": "sparkles", "tasks": "list-checks",
}


def resolve_icon(name: object) -> str | None:
    """A real icon name for `name`, or None (unknown/blank — render without one)."""
    if not isinstance(name, str):
        return None
    key = name.strip().lower().replace("_", "-").replace(" ", "-")
    if key in _NAMES:
        return key
    return ALIASES.get(key)


def icon_path(name: str, variant: str = "red") -> Path | None:
    """The committed PNG for a resolved icon name, or None if the asset is
    missing (a checkout that stripped assets renders icon-less, not an error)."""
    path = ICON_DIR / variant / f"{name}.png"
    return path if path.exists() else None
