"""Newspaper stand, ads, and roadside shop slots. Vision only."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from app.storage.logger import get_logger
from app.vision.detector import DetectedObject, match_to_object
from app.vision.regions import (
    NEWS_OPEN_LEFT_PAGE,
    NEWS_PROMO_SLOTS,
    news_spread_slots,
    shop_crates_px,
)
from app.vision.template_matcher import TemplateMatch, TemplateMatcher, as_bgr

log = get_logger("VISION")

COIN_THRESHOLD = 0.62
ITEM_THRESHOLD = 0.72
# Shop crate (colour) vs Daily Dirt print (smaller, grey-brown).
SHOP_ITEM_SCALES = (1.0, 0.82, 0.9, 1.1, 1.25, 1.45)
NEWS_ITEM_SCALES = (1.0, 0.82, 0.9, 1.1, 1.22)
ITEM_SCALES = SHOP_ITEM_SCALES
SLOT_PAD = 90
LIME_LO = (40, 150, 140)
LIME_HI = (70, 255, 255)
MIN_LIME_PX = 80


@dataclass
class CrateIcon:
    x: int
    y: int
    width: int
    height: int
    image: np.ndarray


@dataclass(frozen=True)
class ShopSlot:
    item: str
    x: int
    y: int
    width: int
    height: int
    confidence: float
    has_coin: bool
    has_diamond: bool

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    @property
    def buyable(self) -> bool:
        return True

    def to_object(self) -> DetectedObject:
        return DetectedObject(
            type="item",
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
            confidence=self.confidence,
            state=self.item,
        )


class NewspaperDetector:
    def __init__(
        self,
        matcher: TemplateMatcher | None = None,
        buy_threshold: float = ITEM_THRESHOLD,
        news_threshold: float = ITEM_THRESHOLD,
    ):
        self.matcher = matcher or TemplateMatcher()
        self.buy_threshold = float(buy_threshold)
        self.news_threshold = float(news_threshold)

    def find_stand(self, source) -> DetectedObject | None:
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        best: TemplateMatch | None = None
        for name in self.matcher.names:
            if not name.startswith("newspaper_stand"):
                continue
            hit = self.matcher.match_one(image, name)
            if hit is None:
                continue
            if best is None or hit.confidence > best.confidence:
                best = hit
        if best is None:
            return None
        obj = match_to_object(best)
        return DetectedObject(
            type="newspaper",
            x=obj.x,
            y=obj.y,
            width=obj.width,
            height=obj.height,
            confidence=obj.confidence,
            state="stand",
        )

    def find_ads(self, source, left_page: int = NEWS_OPEN_LEFT_PAGE) -> list[DetectedObject]:
        """Player listings on the open spread. Promo / diamond / empty cells are skipped."""
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        h, w = image.shape[:2]
        ads: list[DetectedObject] = []
        for page, slot, x, y, bw, bh in news_spread_slots(w, h, left_page):
            if (page, slot) in NEWS_PROMO_SLOTS:
                continue
            roi = image[y : y + bh, x : x + bw]
            if roi.size == 0:
                continue
            coin = self._slot_coin(roi)
            if coin is False:
                continue
            ads.append(
                DetectedObject(
                    type="newspaper",
                    x=x,
                    y=y,
                    width=bw,
                    height=bh,
                    confidence=1.0 if coin is None else coin,
                    state="ad",
                )
            )
        return ads

    def _slot_coin(self, roi: np.ndarray) -> float | None | bool:
        """True-ish confidence if the cell has a coin price. False = skip. None = unknown."""
        if "price_coin" not in self.matcher.names:
            return None
        hit = self.matcher.match_one(
            roi,
            "price_coin",
            threshold=COIN_THRESHOLD,
            scales=(1.0, 0.85, 0.9, 1.1),
        )
        if hit is None:
            return False
        return hit.confidence

    def ad_item_icon(self, source, ad: DetectedObject | None = None) -> np.ndarray:
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        if ad is not None:
            y2 = ad.y + ad.height
            x2 = ad.x + ad.width
            image = image[ad.y:y2, ad.x:x2]
        return crop_ad_item_icon(image)

    def find_wishlist_ads(
        self,
        source,
        wishlist: dict[str, dict],
        left_page: int = NEWS_OPEN_LEFT_PAGE,
        ads: list[DetectedObject] | None = None,
    ) -> list[tuple[DetectedObject, str]]:
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        cells = ads if ads is not None else self.find_ads(image, left_page=left_page)
        found: list[tuple[DetectedObject, str]] = []
        for ad in cells:
            roi = image[ad.y : ad.y + ad.height, ad.x : ad.x + ad.width]
            item = self._wishlist_item_in(roi, wishlist)
            if item is not None:
                found.append((ad, item))
                log.info(f"NEWS match {item} ad={ad.x},{ad.y}")
        return found

    def _wishlist_item_in(self, roi: np.ndarray, wishlist: dict[str, dict]) -> str | None:
        best: tuple[str, float] | None = None
        for item, spec in wishlist.items():
            hit = self._match_wishlist_item(roi, item, spec, scene="news")
            if hit is None:
                continue
            if best is None or hit.confidence > best[1]:
                best = (item, hit.confidence)
        return None if best is None else best[0]

    def find_slots(self, source, wishlist: dict[str, dict]) -> list[ShopSlot]:
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        search, ox, oy = _shop_search(image)
        slots: list[ShopSlot] = []
        for item, spec in wishlist.items():
            hit = self._match_wishlist_item(search, item, spec, scene="shop")
            if hit is None:
                name = spec.get("template") or f"item_{item}"
                if name not in self.matcher.names:
                    log.info(f"slot skip {item} missing {name}.png")
                else:
                    log.info(f"slot skip {item} no match")
                continue
            log.info(f"slot {item} conf={hit.confidence:.2f}")
            slots.append(
                ShopSlot(
                    item=item,
                    x=hit.x + ox,
                    y=hit.y + oy,
                    width=hit.width,
                    height=hit.height,
                    confidence=hit.confidence,
                    has_coin=True,
                    has_diamond=False,
                )
            )
        return slots

    def _match_wishlist_item(
        self, image: np.ndarray, item: str, spec: dict, *, scene: str
    ) -> TemplateMatch | None:
        shop_name = spec.get("template") or f"item_{item}"
        news_name = spec.get("news_template") or f"{shop_name}_news"
        if scene == "shop":
            if shop_name not in self.matcher.names:
                return None
            return self.matcher.match_one(
                image,
                shop_name,
                threshold=self.buy_threshold,
                scales=SHOP_ITEM_SCALES,
                channels="bgr",
            )
        if news_name in self.matcher.names:
            return self.matcher.match_one(
                image,
                news_name,
                threshold=self.news_threshold,
                scales=NEWS_ITEM_SCALES,
                channels="gray",
            )
        return None

    def find_crate_icons(self, source) -> list[CrateIcon]:
        """Crop item art above each lime price tag. Empty crates have no tag."""
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        icons: list[CrateIcon] = []
        for tag in _lime_tag_boxes(image):
            box = _icon_box_above_tag(image, tag)
            if box is None:
                continue
            x, y, w, h = box
            crop = image[y : y + h, x : x + w]
            if crop.size == 0:
                continue
            icons.append(CrateIcon(x, y, w, h, crop.copy()))
        return icons

    def find_close(self, source) -> DetectedObject | None:
        for name in ("shop_close", "newspaper_close", "popup_close"):
            hit = self._match(source, name)
            if hit is not None:
                return match_to_object(hit)
        return None

    def find_home(self, source) -> DetectedObject | None:
        hit = self._match(source, "shop_home")
        return match_to_object(hit) if hit else None

    def find_friends(self, source) -> DetectedObject | None:
        hit = self._match(source, "hud_friends")
        return match_to_object(hit) if hit else None

    def find_header(self, source) -> DetectedObject | None:
        hit = self._match(source, "shop_header")
        return match_to_object(hit) if hit else None

    def _match(self, source, name: str) -> TemplateMatch | None:
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        return self.matcher.match_one(image, name)


def _shop_search(image: np.ndarray) -> tuple[np.ndarray, int, int]:
    h, w = image.shape[:2]
    box = shop_crates_px(w, h)
    if box is None:
        return image, 0, 0
    x, y, bw, bh = box
    return image[y : y + bh, x : x + bw], x, y


def crop_ad_item_icon(ad_bgr: np.ndarray) -> np.ndarray:
    """Tight crop of the item graphic inside a Daily Dirt ad card."""
    h, w = ad_bgr.shape[:2]
    x = int(round(w * 0.36))
    y = int(round(h * 0.54))
    bw = max(24, int(round(w * 0.28)))
    bh = max(24, int(round(h * 0.34)))
    return ad_bgr[y : min(h, y + bh), x : min(w, x + bw)].copy()


def bgr_png_bytes(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("png encode failed")
    return buf.tobytes()


def _nearby_box(hit: TemplateMatch, pad: int) -> tuple[int, int, int, int]:
    return (
        hit.x - pad,
        hit.y - pad,
        hit.width + pad * 2,
        hit.height + pad * 2,
    )


def _lime_tag_boxes(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(LIME_LO), np.array(LIME_HI))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = image.shape[:2]
    box = shop_crates_px(w, h)
    if box is None:
        x_lo, y_lo, x_hi, y_hi = int(w * 0.16), int(h * 0.38), int(w * 0.86), int(h * 0.86)
    else:
        x0, y0, rw, rh = box
        x_lo, y_lo, x_hi, y_hi = x0, y0, x0 + rw, y0 + rh
    min_area = max(400, int(w * h * 0.0002))
    boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        if cv2.contourArea(contour) < min_area:
            continue
        x, y, bw, bh = cv2.boundingRect(contour)
        cx, cy = x + bw // 2, y + bh // 2
        if not (x_lo <= cx <= x_hi and y_lo <= cy <= y_hi):
            continue
        if bw < 24 or bh < 16:
            continue
        boxes.append((x, y, bw, bh))
    boxes.sort(key=lambda b: (b[1], b[0]))
    return boxes


def _icon_box_above_tag(
    image: np.ndarray, tag: tuple[int, int, int, int]
) -> tuple[int, int, int, int] | None:
    tx, ty, tw, th = tag
    h, w = image.shape[:2]
    side = int(max(72, max(tw, th) * 2.4))
    side = min(side, 220)
    cx = tx + tw // 2
    y2 = max(0, ty - 4)
    y1 = max(0, y2 - side)
    x1 = max(0, min(w - 1, cx - side // 2))
    x2 = min(w, x1 + side)
    x1 = max(0, x2 - side)
    if y2 - y1 < 40 or x2 - x1 < 40:
        return None
    return x1, y1, x2 - x1, y2 - y1


def _lime_price_tag(image: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    """Hay Day crate price tags are saturated lime green, not the checkered cloth."""
    bx, by, bw, bh = box
    h, w = image.shape[:2]
    x1, y1 = max(0, bx), max(0, by)
    x2, y2 = min(w, bx + bw), min(h, by + bh)
    if x2 <= x1 or y2 <= y1:
        return False
    roi = image[y1:y2, x1:x2]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(LIME_LO), np.array(LIME_HI))
    return int(np.count_nonzero(mask)) >= MIN_LIME_PX


def _any_inside(hits: list[TemplateMatch], box: tuple[int, int, int, int]) -> bool:
    bx, by, bw, bh = box
    for hit in hits:
        cx = hit.x + hit.width // 2
        cy = hit.y + hit.height // 2
        if bx <= cx <= bx + bw and by <= cy <= by + bh:
            return True
    return False
