"""Relative HUD regions from a 1920x1080 Small-UI farm screenshot.

These are search/fallback windows, not tap targets.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RelRect:
    x: float
    y: float
    w: float
    h: float

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        x = int(round(self.x * width))
        y = int(round(self.y * height))
        w = max(1, int(round(self.w * width)))
        h = max(1, int(round(self.h * height)))
        x = max(0, min(x, max(0, width - 1)))
        y = max(0, min(y, max(0, height - 1)))
        w = min(w, width - x)
        h = min(h, height - y)
        return x, y, w, h


@dataclass(frozen=True)
class RelPoint:
    x: float
    y: float

    def to_pixels(self, width: int, height: int) -> tuple[int, int]:
        px = max(0, min(width - 1, int(round(self.x * width))))
        py = max(0, min(height - 1, int(round(self.y * height))))
        return px, py


# Generous windows around HUD controls observed on the live farm capture.
UI_REGIONS: dict[str, RelRect] = {
    "hud_settings": RelRect(0.00, 0.00, 0.12, 0.18),
    "hud_shop": RelRect(0.00, 0.78, 0.14, 0.22),
    "hud_friends": RelRect(0.86, 0.78, 0.14, 0.22),
    "xp": RelRect(0.32, 0.00, 0.36, 0.12),
    "coins": RelRect(0.62, 0.00, 0.28, 0.10),
    "diamonds": RelRect(0.62, 0.08, 0.28, 0.12),
    "shop_header": RelRect(0.25, 0.08, 0.50, 0.22),
    "shop_home": RelRect(0.00, 0.78, 0.18, 0.22),
    "shop_close": RelRect(0.82, 0.00, 0.18, 0.20),
    "newspaper_close": RelRect(0.82, 0.00, 0.18, 0.20),
    # Include the left road edge — live mailbox sits near x=0.
    "newspaper_stand": RelRect(0.00, 0.12, 0.96, 0.84),
}

# Player shop stall: 2x5 crates on the checkered table (1920x1080 Small-UI).
# Search/capture stay inside this box so HUD / FARMSHOP banner are ignored.
SHOP_CRATES = RelRect(0.16, 0.30, 0.68, 0.48)


def shop_crates_px(width: int, height: int) -> tuple[int, int, int, int] | None:
    """Crate table on a full shop screenshot. None = image is already a crate crop."""
    if width < 640 or height < 400:
        return None
    return SHOP_CRATES.to_pixels(width, height)


# Farm playable area = screen minus HUD. From the same 1920x1080 capture as HUD templates.
PLAY_AREA = RelRect(0.08, 0.14, 0.84, 0.68)

# Fixed HUD taps from a 1920x1080 Small-UI capture. These controls do not move.
HUD_TAPS: dict[str, RelPoint] = {
    "hud_friends": RelPoint(0.961, 0.931),
    "friends_first": RelPoint(0.848, 0.931),
    "shop_close": RelPoint(0.921, 0.088),
    "shop_home": RelPoint(0.047, 0.912),
}

# House-view swipe that reveals the roadside mailbox: bottom-left → upper-right.
# Start sits just right of Shop (never on the cart). End is near the top-right
# landmark, below the diamond HUD and left of the edge buttons.
COLUMN_PAN_START = RelPoint(0.155, 0.880)
COLUMN_PAN_END = RelPoint(0.800, 0.200)

# Daily Dirt is a 10-page book. Page 1 is the cover. Opening lands on 2|3.
# Each listing page is a fixed 2x3 grid. The last spread is page 10 on the left.
NEWS_PAGE_COUNT = 10
NEWS_OPEN_LEFT_PAGE = 2
NEWS_SLOTS_PER_PAGE = 6
NEWS_PROMO_SLOTS = frozenset({(2, 0)})  # page 2 slot 0 = Facebook / banner
_NEWS_REF_W, _NEWS_REF_H = 1920, 1080
NEWS_SLOT_SIZE = RelRect(0.0, 0.0, 340 / _NEWS_REF_W, 260 / _NEWS_REF_H)
NEWS_SLOT_COLS = tuple(x / _NEWS_REF_W for x in (230, 600, 995, 1365))
NEWS_SLOT_ROWS = tuple(y / _NEWS_REF_H for y in (173, 453, 733))


def news_spread_slots(
    width: int, height: int, left_page: int
) -> list[tuple[int, int, int, int, int, int]]:
    """Listing cells on the open spread: (page, slot, x, y, w, h).

    `slot` is 0-5 in reading order on that page. Page 10 has no right page.
    """
    slot_w = max(1, int(round(NEWS_SLOT_SIZE.w * width)))
    slot_h = max(1, int(round(NEWS_SLOT_SIZE.h * height)))
    right_page = left_page + 1 if left_page < NEWS_PAGE_COUNT else None
    cells: list[tuple[int, int, int, int, int, int]] = []
    for row, ry in enumerate(NEWS_SLOT_ROWS):
        y = int(round(ry * height))
        for col_on_page in range(2):
            x = int(round(NEWS_SLOT_COLS[col_on_page] * width))
            slot = row * 2 + col_on_page
            cells.append((left_page, slot, x, y, slot_w, slot_h))
        if right_page is None:
            continue
        for col_on_page in range(2):
            x = int(round(NEWS_SLOT_COLS[col_on_page + 2] * width))
            slot = row * 2 + col_on_page
            cells.append((right_page, slot, x, y, slot_w, slot_h))
    return cells


def ui_regions_px(width: int, height: int) -> dict[str, tuple[int, int, int, int]]:
    return {name: rect.to_pixels(width, height) for name, rect in UI_REGIONS.items()}


def hud_tap(name: str, width: int, height: int) -> tuple[int, int]:
    return HUD_TAPS[name].to_pixels(width, height)
