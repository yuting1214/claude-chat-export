"""Small text helpers shared by the chat and code exporters."""
from __future__ import annotations

import re


def slugify(name: str) -> str:
    """Filesystem-safe slug: lowercase, words joined by '-', capped at 60 chars."""
    s = re.sub(r"[^\w\s-]", "", (name or "").lower()).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:60] or "untitled"
