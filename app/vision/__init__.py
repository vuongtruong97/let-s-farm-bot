"""Vision: what is on the current screen. No game decisions here."""

from app.vision.detector import DetectedObject, UiDetector
from app.vision.fields import FieldDetector
from app.vision.newspaper import NewspaperDetector, ShopSlot
from app.vision.overlay import draw_overlay, save_overlay
from app.vision.regions import RelRect, ui_regions_px
from app.vision.screen import GameScreen, ScreenDetection, ScreenDetector
from app.vision.template_matcher import TemplateMatch, TemplateMatcher

__all__ = [
    "DetectedObject",
    "FieldDetector",
    "GameScreen",
    "NewspaperDetector",
    "RelRect",
    "ScreenDetection",
    "ScreenDetector",
    "ShopSlot",
    "TemplateMatch",
    "TemplateMatcher",
    "UiDetector",
    "draw_overlay",
    "save_overlay",
    "ui_regions_px",
]
