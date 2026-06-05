from __future__ import annotations

import html
from typing import Any


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def svg_header(width: int, height: int) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'


def svg_rect(x: float, y: float, width: float, height: float, fill: str) -> str:
    return f'<rect x="{x:g}" y="{y:g}" width="{width:g}" height="{height:g}" fill="{fill}"/>'


def svg_text(
    x: float,
    y: float,
    text: Any,
    *,
    size: int = 12,
    anchor: str = "start",
    weight: str = "400",
    rotate: int | None = None,
) -> str:
    transform = f' transform="rotate({rotate} {x:.2f} {y:.2f})"' if rotate is not None else ""
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-family="Inter, Arial, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
        f'fill="#111827"{transform}>{escape(text)}</text>'
    )

