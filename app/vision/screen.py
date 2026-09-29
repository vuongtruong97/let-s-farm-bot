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
# Every other UI template lands 1:1 on a 1920x1080 emulator, but Hay Day draws
# the dialog X at a different size per dialog (the silo one is 0.92 of the
# capture). This is the template that keeps the bot off diamond prompts, so it
# keeps the sweep.
SCREEN_SCALES = {"popup_close": (1.0, 0.85, 0.92, 1.08, 1.15)}
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

    def detect(self, source, *, full: bool = False) -> ScreenDetection:
        """Classify one frame. full=True also collects the objects of every
        screen template — useful for overlays, wasteful inside a poll loop."""
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        if not full:
            return self._detect_fast(image)
        names = tuple(n for n in self.matcher.names if _is_screen_template(n))
        matches = [m for m in (self._match(image, n) for n in names) if m is not None]
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

    def _detect_fast(self, image: np.ndarray) -> ScreenDetection:
        """Same priority order as the full pass, but stops at the first family
        that decides the screen. Matching all nine screen templates costs ~2s a
        frame, which eats a whole poll budget and makes an open stall look shut.
        """
        for name, screen in (
            ("shop_header", GameScreen.PLAYER_SHOP),
            ("newspaper_ad", GameScreen.NEWSPAPER),
            ("shop_close", GameScreen.PLAYER_SHOP),
        ):
            hit = self._match(image, name)
            if hit is not None:
                return _decide(screen, hit.confidence, [match_to_object(hit)])

        popup = self._match_family(image, "popup_")
        if popup:
            best = max(popup, key=lambda m: m.confidence)
            return _decide(
                GameScreen.POPUP, best.confidence, [match_to_object(m) for m in popup]
            )

        hud = self._match_family(image, "hud_")
        objects = [match_to_object(m) for m in hud]
        if len({m.name for m in hud}) >= MIN_HUD_HITS:
            confidence = sum(m.confidence for m in hud) / len(hud)
            return _decide(GameScreen.FARM, confidence, objects)

        confidence = max((m.confidence for m in [*popup, *hud]), default=0.0)
        return _decide(GameScreen.UNKNOWN, confidence, objects)

    def _match_family(self, image: np.ndarray, prefix: str):
        names = (
            n
            for n in self.matcher.names
            if n.startswith(prefix) and _is_screen_template(n)
        )
        return [m for m in (self._match(image, n) for n in names) if m is not None]

    def _match(self, image: np.ndarray, name: str):
        return self.matcher.match_one(
            image,
            name,
            threshold=SCREEN_MATCH_THRESHOLD,
            scales=SCREEN_SCALES.get(name),
        )


def popup_close_point(screen: ScreenDetection) -> tuple[int, int] | None:
    """Centre of the most confident popup X, or None to fall back to Back."""
    closes = [o for o in screen.objects if o.type == "popup" and o.state == "close"]
    if not closes:
        return None
    close = max(closes, key=lambda o: o.confidence)
    return close.x + close.width // 2, close.y + close.height // 2


def _decide(screen: GameScreen, confidence: float, objects) -> ScreenDetection:
    result = ScreenDetection(screen, confidence, objects)
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
