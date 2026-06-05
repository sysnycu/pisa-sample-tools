from __future__ import annotations

import re
from typing import Any


def format_number(value: float) -> str:
    return f"{value:.3g}"


def slug(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or fallback


def wrap_text(text: str, *, max_chars: int, split_long_words: bool = True) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    words = text.split()
    if not words:
        return [text[:max_chars]]
    lines: list[str] = []
    current = ""
    for word in words:
        if split_long_words and len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.extend(word[index : index + max_chars] for index in range(0, len(word), max_chars))
            continue
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def panel_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)

