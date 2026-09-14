from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from app.storage.logger import get_logger
from app.vision.detector import DetectedObject, UiDetector, match_to_object
from app.vision.template_matcher import TemplateMatcher, as_bgr

log = get_logger("VISION")

MIN_HUD_HITS = 2
# Screen class must not use config.template_threshold (0.99 rejects a real stall).
SCREEN_MATCH_THRESHOLD = 0.80
SCREEN_PREFIXES = ("hud_", "popup_", "shop_", "newspaper_")
SKIP_SCREEN_PREFIXES = ("item_", "seed_", "price_", "newspaper_stand")


class GameScreen(str, Enum):
    FARM = "FARM"
    BARN = "BARN"
    SILO = "SILO"
    PRODUCTION = "PRODUCTION"
    ORDER_BOARD = "ORDER_BOARD"
    TRUCK_ORDER = "TRUCK_ORDER"
    ANIMAL_AREA = "ANIMAL_AREA"
    SHOP = "SHOP"
    NEWSPAPER = "NEWSPAPER"
    PLAYER_SHOP = "PLAYER_SHOP"
    POPUP = "POPUP"
    LOADING = "LOADING"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass
class ScreenDetection:
    screen: GameScreen
    confidence: float
    objects: list[DetectedObject] = field(default_factory=list)


class ScreenDetector:
    """Classify the current screenshot. Vision only — no actions."""

    def __init__(self, matcher: TemplateMatcher | None = None):
        self.matcher = matcher or TemplateMatcher()
        self.ui = UiDetector(self.matcher)

    def detect(self, source) -> ScreenDetection:
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        names = tuple(n for n in self.matcher.names if _is_screen_template(n))
        matches = self.matcher.match_all(
            image, names=names, threshold=SCREEN_MATCH_THRESHOLD
        )
        objects = [match_to_object(m) for m in matches]

        popup = [m for m in matches if m.name.startswith("popup_")]
        hud = [m for m in matches if m.name.startswith("hud_")]
        news_ui = [m for m in matches if m.name == "newspaper_ad"]
        shop_header = [m for m in matches if m.name == "shop_header"]
        shop_close = [m for m in matches if m.name == "shop_close"]

        # FARMSHOP banner, then ads, then the stall X (same red X as popups).
        # Other-player stalls often miss the header crop; do not treat that X as POPUP.
        if shop_header:
            best = max(shop_header, key=lambda m: m.confidence)
            result = ScreenDetection(GameScreen.PLAYER_SHOP, best.confidence, objects)
            _log(result)
            return result

        if news_ui:
            best = max(news_ui, key=lambda m: m.confidence)
            result = ScreenDetection(GameScreen.NEWSPAPER, best.confidence, objects)
            _log(result)
            return result

        if shop_close:
            best = max(shop_close, key=lambda m: m.confidence)
            result = ScreenDetection(GameScreen.PLAYER_SHOP, best.confidence, objects)
            _log(result)
            return result

        if popup:
            best = max(popup, key=lambda m: m.confidence)
            result = ScreenDetection(GameScreen.POPUP, best.confidence, objects)
            _log(result)
            return result

        hud_names = {m.name for m in hud}
        if len(hud_names) >= MIN_HUD_HITS:
            confidence = sum(m.confidence for m in hud) / len(hud)
            result = ScreenDetection(GameScreen.FARM, confidence, objects)
            _log(result)
            return result

        confidence = max((m.confidence for m in matches), default=0.0)
        result = ScreenDetection(GameScreen.UNKNOWN, confidence, objects)
        _log(result)
        return result


def _is_screen_template(name: str) -> bool:
    if name.startswith(SKIP_SCREEN_PREFIXES):
        return False
    return name.startswith(SCREEN_PREFIXES)


def _log(result: ScreenDetection) -> None:
    bits = " ".join(
        f"{obj.state}={obj.confidence:.2f}" for obj in result.objects if obj.state
    )
    log.info(f"{result.screen.value} conf={result.confidence:.2f} {bits}".rstrip())
