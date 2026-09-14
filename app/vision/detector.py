from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.vision.template_matcher import TemplateMatch, TemplateMatcher, as_bgr


@dataclass(frozen=True)
class DetectedObject:
    type: str
    x: int
    y: int
    width: int
    height: int
    confidence: float
    state: str | None = None


def match_to_object(match: TemplateMatch) -> DetectedObject:
    kind = "ui"
    state = None
    if match.name.startswith("hud_"):
        kind = "ui"
        state = match.name.removeprefix("hud_")
    elif match.name.startswith("popup_"):
        kind = "popup"
        state = match.name.removeprefix("popup_")
    elif match.name.startswith("newspaper_"):
        kind = "newspaper"
        state = match.name.removeprefix("newspaper_")
    elif match.name.startswith("shop_"):
        kind = "shop"
        state = match.name.removeprefix("shop_")
    elif match.name.startswith("item_"):
        kind = "item"
        state = match.name.removeprefix("item_")
    elif match.name.startswith("price_"):
        kind = "price"
        state = match.name.removeprefix("price_")
    return DetectedObject(
        type=kind,
        x=match.x,
        y=match.y,
        width=match.width,
        height=match.height,
        confidence=match.confidence,
        state=state or match.name,
    )


class UiDetector:
    """Detect UI elements via templates. Field/crop detection is M2."""

    def __init__(self, matcher: TemplateMatcher | None = None):
        self.matcher = matcher or TemplateMatcher()

    def detect(self, source) -> list[DetectedObject]:
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        return [match_to_object(match) for match in self.matcher.match_all(image)]
