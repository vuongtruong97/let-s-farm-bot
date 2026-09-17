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
    NEWS_SLOT_COLS,
    NEWS_SLOT_ROWS,
    NEWS_SLOT_SIZE,
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
SOLD_SAT_MAX = 50
PRICE_NEAR_PAD = (12, 8, 56, 56)
QTY_PAD_LEFT = 0.85
QTY_PAD_UP = 1.05
QTY_PAD_RIGHT = 0.45
QTY_PAD_DOWN = 0.35
PROOF_PAD_RIGHT = 1.15
PROOF_PAD_DOWN = 1.05
QTY_X_THRESHOLD = 0.70
QTY_SCALES = (0.7, 0.85, 1.0, 1.15, 1.3, 1.5)


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
            hits = self._match_wishlist_items(search, item, spec)
            if not hits:
                name = spec.get("template") or f"item_{item}"
                if name not in self.matcher.names:
                    log.info(f"slot skip {item} missing {name}.png")
                else:
                    log.info(f"slot skip {item} no match")
                continue
            for hit in hits:
                x, y = hit.x + ox, hit.y + oy
                crop = _slot_crop(image, x, y, hit.width, hit.height)
                if _is_gray_crate(crop):
                    log.info(f"slot skip {item} sold gray {x},{y}")
                    continue
                has_coin = _price_near(image, self.matcher, x, y, hit.width, hit.height)
                log.info(f"slot {item} conf={hit.confidence:.2f} {x},{y}")
                slots.append(
                    ShopSlot(
                        item=item,
                        x=x,
                        y=y,
                        width=hit.width,
                        height=hit.height,
                        confidence=hit.confidence,
                        has_coin=has_coin,
                        has_diamond=False,
                    )
                )
        return slots

    def slot_sold(self, source, slot: ShopSlot, *, had_coin: bool = False) -> bool:
        """True when the crate looks sold: grey icon and/or the coin tag vanished."""
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        crop = _slot_crop(image, slot.x, slot.y, slot.width, slot.height)
        if _is_gray_crate(crop):
            return True
        if had_coin and not _price_near(
            image, self.matcher, slot.x, slot.y, slot.width, slot.height
        ):
            return True
        return False

    def _match_wishlist_items(
        self, image: np.ndarray, item: str, spec: dict
    ) -> list[TemplateMatch]:
        shop_name = spec.get("template") or f"item_{item}"
        if shop_name not in self.matcher.names:
            return []
        return self.matcher.match_many(
            image,
            shop_name,
            threshold=self.buy_threshold,
            min_dist=48,
            scales=SHOP_ITEM_SCALES,
            channels="bgr",
        )

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

    def read_crate_qty(self, source, slot: ShopSlot) -> int | None:
        """Stack size painted as xN on the crate rim. None if unread."""
        if "qty_x" not in self.matcher.names:
            return None
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        box = qty_roi_box(slot, image.shape)
        if box is None:
            return None
        x, y, w, h = box
        roi = image[y : y + h, x : x + w]
        hit = self.matcher.match_one(
            roi, "qty_x", threshold=QTY_X_THRESHOLD, scales=QTY_SCALES
        )
        if hit is None:
            return None
        sx = hit.x + int(hit.width * 0.72)
        sy1 = max(0, hit.y - 2)
        sy2 = min(roi.shape[0], hit.y + hit.height + 2)
        if sx >= roi.shape[1] or sy2 <= sy1:
            return None
        strip = roi[sy1:sy2, sx:]
        digits = _qty_digits(strip, hit.height, hit.width)
        if not digits:
            return None
        try:
            qty = int("".join(digits))
        except ValueError:
            return None
        if 1 <= qty <= 999:
            return qty
        return None

    def _match(self, source, name: str) -> TemplateMatch | None:
        image = source if isinstance(source, np.ndarray) else as_bgr(source)
        return self.matcher.match_one(image, name)


def _slot_crop(image: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    ih, iw = image.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(iw, x + w), min(ih, y + h)
    if x2 <= x1 or y2 <= y1:
        return image[0:0, 0:0]
    return image[y1:y2, x1:x2]


def _expand_slot_box(
    slot: ShopSlot,
    shape: tuple[int, ...],
    *,
    left: float,
    up: float,
    right: float,
    down: float,
) -> tuple[int, int, int, int] | None:
    ih, iw = shape[:2]
    x1 = max(0, int(slot.x - left * slot.width))
    y1 = max(0, int(slot.y - up * slot.height))
    x2 = min(iw, int(slot.x + right * slot.width))
    y2 = min(ih, int(slot.y + down * slot.height))
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    return x1, y1, x2 - x1, y2 - y1


def qty_roi_box(slot: ShopSlot, shape: tuple[int, ...]) -> tuple[int, int, int, int] | None:
    return _expand_slot_box(
        slot, shape, left=QTY_PAD_LEFT, up=QTY_PAD_UP, right=QTY_PAD_RIGHT, down=QTY_PAD_DOWN
    )


def crate_proof_box(slot: ShopSlot, shape: tuple[int, ...]) -> tuple[int, int, int, int] | None:
    return _expand_slot_box(
        slot,
        shape,
        left=QTY_PAD_LEFT,
        up=QTY_PAD_UP,
        right=PROOF_PAD_RIGHT,
        down=PROOF_PAD_DOWN,
    )


def crate_proof_crop(source, slot: ShopSlot) -> np.ndarray:
    image = source if isinstance(source, np.ndarray) else as_bgr(source)
    box = crate_proof_box(slot, image.shape)
    if box is None:
        return _slot_crop(image, slot.x, slot.y, slot.width, slot.height)
    x, y, w, h = box
    return _slot_crop(image, x, y, w, h)


def _qty_white_mask(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    white = cv2.inRange(hsv, (0, 0, 160), (180, 130, 255))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    return cv2.morphologyEx(white, cv2.MORPH_CLOSE, kernel)


def _qty_holes(bin_img: np.ndarray) -> int:
    pad = cv2.copyMakeBorder(bin_img, 3, 3, 3, 3, cv2.BORDER_CONSTANT, value=0)
    _contours, hier = cv2.findContours(pad, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return 0
    return int(sum(1 for row in hier[0] if row[3] >= 0))


def _qty_hole_cy(bin_img: np.ndarray) -> float:
    pad = cv2.copyMakeBorder(bin_img, 3, 3, 3, 3, cv2.BORDER_CONSTANT, value=0)
    inv = cv2.bitwise_not(pad)
    filled = inv.copy()
    cv2.floodFill(filled, None, (0, 0), 0)
    ys = np.where(filled > 0)[0]
    if len(ys) == 0:
        return 0.5
    return float(ys.mean() / pad.shape[0])


def _classify_qty_digit(bin_img: np.ndarray) -> str | None:
    h, w = bin_img.shape[:2]
    ink = (bin_img > 0).astype(np.float32)
    if ink.mean() < 0.05:
        return None
    holes = _qty_holes(bin_img)
    aspect = h / max(w, 1)
    top, mid, bot = (
        float(ink[: h // 3].mean()),
        float(ink[h // 3 : 2 * h // 3].mean()),
        float(ink[2 * h // 3 :].mean()),
    )
    fill = float(ink.mean())
    right = float(ink[:, w // 2 :].mean())
    left = float(ink[:, : w // 2].mean())
    if holes >= 2:
        return "8"
    if holes == 1:
        if fill >= 0.58:
            return "8"
        cy = _qty_hole_cy(bin_img)
        if cy < 0.40:
            return "9"
        if cy > 0.60:
            return "6"
        if aspect > 1.35:
            return "4"
        return "0"
    if aspect > 2.05:
        return "1"
    if top > bot * 1.08 and top >= mid:
        return "7"
    if bot > top and right > left:
        return "2"
    if right > left * 1.08:
        return "3"
    return "5"


def _qty_digits(strip: np.ndarray, x_h: int, x_w: int) -> list[str]:
    if strip.size == 0:
        return []
    mask = _qty_white_mask(strip)
    _n, _labels, stats, _cents = cv2.connectedComponentsWithStats(mask, 8)
    min_h = max(10, int(x_h * 0.35))
    glyphs: list[tuple[int, np.ndarray]] = []
    for i in range(1, stats.shape[0]):
        gx, gy, gw, gh, area = (int(v) for v in stats[i])
        if gh < min_h or area < 40 or gw > int(x_w * 1.6):
            continue
        glyphs.append((gx, mask[gy : gy + gh, gx : gx + gw]))
    glyphs.sort(key=lambda item: item[0])
    digits: list[str] = []
    for _gx, crop in glyphs:
        digit = _classify_qty_digit(crop)
        if digit is None:
            return []
        digits.append(digit)
    return digits


def _mean_saturation(crop: np.ndarray) -> float:
    if crop.size == 0:
        return 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    lit = hsv[:, :, 2] > 40
    if not np.any(lit):
        return 0.0
    return float(np.mean(hsv[:, :, 1][lit]))


def _is_gray_crate(crop: np.ndarray) -> bool:
    return crop.size > 0 and _mean_saturation(crop) <= SOLD_SAT_MAX


def _price_near(
    image: np.ndarray, matcher: TemplateMatcher, x: int, y: int, w: int, h: int
) -> bool:
    ih, iw = image.shape[:2]
    pl, pu, pr, pd = PRICE_NEAR_PAD
    x1, y1 = max(0, x - pl), max(0, y - pu)
    x2, y2 = min(iw, x + w + pr), min(ih, y + h + pd)
    if x2 <= x1 or y2 <= y1:
        return False
    roi = image[y1:y2, x1:x2]
    if "price_coin" in matcher.names:
        hit = matcher.match_one(
            roi,
            "price_coin",
            threshold=COIN_THRESHOLD,
            scales=(1.0, 0.85, 0.9, 1.1),
        )
        if hit is not None:
            return True
    for tx, ty, tw, th in _lime_tag_boxes(image):
        cx, cy = tx + tw // 2, ty + th // 2
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            return True
    return False


def _shop_search(image: np.ndarray) -> tuple[np.ndarray, int, int]:
    h, w = image.shape[:2]
    box = shop_crates_px(w, h)
    if box is None:
        return image, 0, 0
    x, y, bw, bh = box
    return image[y : y + bh, x : x + bw], x, y


def crate_table_bgr(source) -> np.ndarray:
    image = source if isinstance(source, np.ndarray) else as_bgr(source)
    search, _ox, _oy = _shop_search(image)
    return search


NEWS_FP_SIZE = (96, 54)


def newspaper_listing_bgr(source) -> np.ndarray:
    """Crop of Daily Dirt listing cards on the open spread (not the cover chrome)."""
    image = source if isinstance(source, np.ndarray) else as_bgr(source)
    h, w = image.shape[:2]
    x0 = int(round(NEWS_SLOT_COLS[0] * w))
    y0 = int(round(NEWS_SLOT_ROWS[0] * h))
    x1 = int(round((NEWS_SLOT_COLS[-1] + NEWS_SLOT_SIZE.w) * w))
    y1 = int(round((NEWS_SLOT_ROWS[-1] + NEWS_SLOT_SIZE.h) * h))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((1, 1, 3), dtype=np.uint8)
    return image[y0:y1, x0:x1]


def newspaper_fingerprint(source) -> np.ndarray:
    crop = newspaper_listing_bgr(source)
    if crop.size == 0:
        return np.zeros((NEWS_FP_SIZE[1], NEWS_FP_SIZE[0]), dtype=np.uint8)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return cv2.resize(gray, NEWS_FP_SIZE, interpolation=cv2.INTER_AREA)


def newspaper_fingerprints_differ(a, b, min_changed: float = 0.04) -> bool:
    """True when the open listing spread is not the same newspaper."""
    left = a if _is_news_fp(a) else newspaper_fingerprint(a)
    right = b if _is_news_fp(b) else newspaper_fingerprint(b)
    if left.shape != right.shape:
        return True
    delta = np.abs(left.astype(np.int16) - right.astype(np.int16))
    changed = float(np.mean(delta > 18))
    return changed >= min_changed


def _is_news_fp(value) -> bool:
    return (
        isinstance(value, np.ndarray)
        and value.ndim == 2
        and value.shape == (NEWS_FP_SIZE[1], NEWS_FP_SIZE[0])
    )


def crate_views_differ(a, b, min_changed: float = 0.03) -> bool:
    """True when the visible stall table moved (not yet at that edge)."""
    left = crate_table_bgr(a)
    right = crate_table_bgr(b)
    if left.size == 0 or right.size == 0:
        return True
    if left.shape != right.shape:
        right = cv2.resize(
            right, (left.shape[1], left.shape[0]), interpolation=cv2.INTER_AREA
        )
    delta = np.max(np.abs(left.astype(np.int16) - right.astype(np.int16)), axis=2)
    changed = float(np.mean(delta > 18))
    return changed >= min_changed


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
