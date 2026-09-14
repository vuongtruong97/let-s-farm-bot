"""Newspaper shopping: find column, visit houses, buy wishlist items."""

from __future__ import annotations

import time

from app.actions.farming import Action, ActionResult
from app.config import SCREENSHOT_DIR, AppConfig
from app.controller.camera import CameraManager
from app.controller.device import DeviceController
from app.storage.botdata import (
    active_wishlist,
    clear_column,
    item_news_template_name,
    load_column,
    load_wishlist,
    save_column,
    save_library_png,
)
from app.storage.logger import get_logger
from app.vision.detector import DetectedObject
from app.vision.newspaper import NewspaperDetector, ShopSlot, bgr_png_bytes
from app.vision.overlay import save_overlay
from app.vision.regions import NEWS_OPEN_LEFT_PAGE, NEWS_PAGE_COUNT, hud_tap, news_spread_slots
from app.vision.screen import GameScreen, ScreenDetection, ScreenDetector
from app.vision.template_matcher import TemplateMatcher, as_bgr

log = get_logger("ACTION")


class NewspaperActions:
    def __init__(
        self,
        device: DeviceController,
        config: AppConfig | None = None,
        screens: ScreenDetector | None = None,
        matcher: TemplateMatcher | None = None,
        news: NewspaperDetector | None = None,
        camera: CameraManager | None = None,
        wait_s: float = 0.9,
        visit_wait_s: float = 3.5,
        retries: int = 1,
    ):
        self.device = device
        self.config = config or AppConfig()
        self.matcher = matcher or TemplateMatcher(threshold=self.config.template_threshold)
        self.screens = screens or ScreenDetector(self.matcher)
        self.news = news or NewspaperDetector(
            self.matcher,
            buy_threshold=self.config.buy_threshold,
            news_threshold=self.config.news_threshold,
        )
        self.camera = camera or CameraManager(device)
        self.wait_s = wait_s
        self.visit_wait_s = visit_wait_s
        self.retries = retries
        self.wishlist = active_wishlist()
        self._visited_ads: set[tuple] = set()
        self._news_left_page = NEWS_OPEN_LEFT_PAGE

    def _sync_vision_thresholds(self) -> None:
        self.news.buy_threshold = self.config.buy_threshold
        self.news.news_threshold = self.config.news_threshold

    def shop_from_newspaper(
        self,
        limit: int = 1,
        should_stop=None,
        mode: str = "shop",
        reset_home: bool = True,
    ) -> ActionResult:
        kind = (mode or "shop").strip().lower()
        if kind in {"browse", "xem"}:
            return self.browse_newspaper(
                limit=limit, should_stop=should_stop, reset_home=reset_home
            )
        home = self._reset_home_if_needed(reset_home)
        if home is not None and not home.success:
            return home
        self.matcher.reload()
        self.wishlist = active_wishlist()
        self._sync_vision_thresholds()
        bought = 0
        visits = 0
        max_visits = max(8, max(1, limit) * 6)
        last = ActionResult(False, Action("BUY", "none"), "no purchase")
        while bought < max(1, limit) and visits < max_visits:
            if should_stop and should_stop():
                return ActionResult(False, Action("STOP", "newspaper"), "stopped")
            visits += 1
            # After close_shop the camera sits on that farm's house, same as home.
            found = self.find_column()
            if not found.success:
                return found
            opened = self.open_newspaper()
            if not opened.success:
                return opened
            ad = self._next_ad()
            if ad is None:
                return ActionResult(False, Action("VISIT_SHOP", "none"), "no newspaper ads")
            visit = self.visit_shop(ad)
            if not visit.success:
                self.close_shop()
                last = visit
                continue
            buy = self.buy_wishlist()
            last = buy
            if buy.success:
                bought += 1
            closed = self.close_shop()
            if not closed.success and last.success:
                last = closed
        if last.success or bought:
            parked = self.go_home()
            if not parked.success:
                return parked
        return last

    def browse_newspaper(
        self, limit: int = 1, should_stop=None, reset_home: bool = True
    ) -> ActionResult:
        """Open Daily Dirt, scan ads page by page, never visit a player shop."""
        home = self._reset_home_if_needed(reset_home)
        if home is not None and not home.success:
            return home
        self.matcher.reload()
        self.wishlist = active_wishlist()
        self._sync_vision_thresholds()
        action = Action("BROWSE", "newspaper")
        found = self.find_column()
        if not found.success:
            return found
        opened = self.open_newspaper()
        if not opened.success:
            return opened
        hits: list[str] = []
        while self._news_left_page <= NEWS_PAGE_COUNT:
            if should_stop and should_stop():
                self.close_shop()
                return ActionResult(False, Action("STOP", "browse"), "stopped")
            png = self.device.screenshot()
            ads = self.news.find_ads(png, left_page=self._news_left_page)
            matched = self.news.find_wishlist_ads(
                png, self.wishlist, left_page=self._news_left_page, ads=ads
            )
            for ad, item in matched:
                hits.append(item)
                log.info(
                    f"BROWSE p{self._news_left_page}-{self._news_left_page + 1} "
                    f"{item} ad={ad.x},{ad.y}"
                )
            right = self._news_left_page + 1
            spread = (
                f"p{self._news_left_page}"
                if self._news_left_page >= NEWS_PAGE_COUNT
                else f"p{self._news_left_page}-{right}"
            )
            log.info(f"BROWSE {spread} ads={len(ads)} wishlist={len(matched)}")
            self._debug_shot(png, None, f"browse_page_{self._news_left_page:02d}.png")
            if self._news_left_page >= NEWS_PAGE_COUNT:
                break
            self._turn_newspaper()
        unique = ",".join(dict.fromkeys(hits)) or "none"
        action.target = unique
        closed = self.close_shop()
        if not closed.success:
            return closed
        log.info(f"BROWSE SUCCESS items={unique}")
        return ActionResult(True, action)

    def go_home(self) -> ActionResult:
        """Return to the house-centered farm via friends → visit → close → shop_home."""
        action = Action("GO_HOME", "house")
        fx, fy = self._tap_hud("hud_friends")
        action.x, action.y = fx, fy
        time.sleep(self.wait_s)
        self._tap_hud("friends_first")
        time.sleep(self.visit_wait_s)
        self._tap_hud("shop_close")
        time.sleep(self.wait_s)
        self._tap_hud("shop_home")
        time.sleep(self.visit_wait_s)
        self.camera.clear_history()
        log.info("GO_HOME house")
        return ActionResult(True, action)

    def _tap_hud(self, name: str) -> tuple[int, int]:
        width, height = self.device.resolution()
        point = hud_tap(name, width, height)
        self.device.tap(*point)
        return point

    def _reset_home_if_needed(self, reset_home: bool) -> ActionResult | None:
        if not reset_home:
            return None
        png = self.device.screenshot()
        if self._newspaper_is_open(png) or self.news.find_header(png) is not None:
            return None
        return self.go_home()

    def find_column(self) -> ActionResult:
        """Park the camera on the roadside mailbox and save its tap point.

        If the stand is already visible (second click, or still at the column),
        do not pan again. Otherwise pan once from the house, then match.
        Cached coordinates are never treated as success on their own.
        """
        action = Action("FIND_COLUMN", "newspaper_stand")
        png = self.device.screenshot()
        stand = self.news.find_stand(png)
        if stand is not None:
            log.info("FIND_COLUMN already on screen")
            return self._remember_stand(action, stand)
        self.camera.pan_to_column()
        time.sleep(self.wait_s)
        png = self.device.screenshot()
        stand = self.news.find_stand(png)
        if stand is None:
            log.info("FIND_COLUMN FAIL stand not on screen after pan")
            return ActionResult(False, action, "newspaper stand not found")
        return self._remember_stand(action, stand)

    def open_newspaper(self) -> ActionResult:
        action = Action("OPEN_NEWSPAPER", "newspaper_stand")
        cached = load_column()
        if cached is not None:
            action.x, action.y = cached
            self.device.tap(*cached)
            time.sleep(self.wait_s)
            if self._newspaper_is_open():
                return self._opened_at_first_spread(action)
            log.info("FIND_COLUMN cache miss — searching")
            clear_column()
        png = self.device.screenshot()
        if self._newspaper_is_open(png):
            return self._opened_at_first_spread(action)
        stand = self.news.find_stand(png)
        if stand is None:
            return ActionResult(False, action, "newspaper stand not on screen")
        action.x, action.y = _center(stand)
        save_column(*_center(stand))
        self.device.tap(*_center(stand))
        time.sleep(self.wait_s)
        after = self.device.screenshot()
        self._debug_shot(after, None, "after_open_newspaper.png")
        if self._newspaper_is_open(after):
            return self._opened_at_first_spread(action)
        clear_column()
        return ActionResult(False, action, "newspaper did not open")

    def _opened_at_first_spread(self, action: Action) -> ActionResult:
        self._news_left_page = NEWS_OPEN_LEFT_PAGE
        return ActionResult(True, action)

    def _remember_stand(self, action: Action, stand: DetectedObject) -> ActionResult:
        action.x, action.y = _center(stand)
        save_column(action.x, action.y)
        log.info(f"FIND_COLUMN saved {action.x},{action.y}")
        return ActionResult(True, action)

    def _newspaper_is_open(self, png=None) -> bool:
        source = png if png is not None else self.device.screenshot()
        hit = self.matcher.match_one(as_bgr(source), "newspaper_ad", threshold=0.72)
        return hit is not None

    def visit_shop(self, ad: DetectedObject) -> ActionResult:
        action = Action("VISIT_SHOP", "ad", ad.x + ad.width // 2, ad.y + ad.height // 2)
        self._visited_ads.add(self._ad_cell(ad))
        self.device.tap(action.x, action.y)
        time.sleep(self.visit_wait_s)
        png = self.device.screenshot()
        screen = self.screens.detect(png)
        self._debug_shot(png, screen, "after_visit_shop.png")
        if screen.screen is GameScreen.POPUP:
            self._close_popup(screen)
            return ActionResult(False, action, "diamond/popup — closed, not spent")
        if screen.screen is GameScreen.PLAYER_SHOP:
            return ActionResult(True, action)
        return ActionResult(False, action, f"screen={screen.screen.value}")

    def buy_wishlist(self) -> ActionResult:
        self.matcher.reload()
        self.wishlist = active_wishlist()
        self._sync_vision_thresholds()
        missing = self._missing_item_templates()
        ready = {
            item: spec
            for item, spec in self.wishlist.items()
            if (spec.get("template") or f"item_{item}") not in missing
        }
        if not self.wishlist:
            return ActionResult(False, Action("BUY", "none"), "wishlist empty")
        if not ready:
            return ActionResult(
                False,
                Action("BUY", "none"),
                f"missing template {missing[0]}.png — capture item to data/templates/",
            )
        png = self.device.screenshot()
        screen = self.screens.detect(png)
        if screen.screen is GameScreen.POPUP:
            self._close_popup(screen)
            return ActionResult(False, Action("BUY", "none"), "popup open")
        if screen.screen is not GameScreen.PLAYER_SHOP:
            return ActionResult(
                False, Action("BUY", "none"), f"screen={screen.screen.value}"
            )
        slots = self.news.find_slots(png, ready)
        if not slots:
            log.info(f"BUY no slot matched missing={missing}")
            return ActionResult(False, Action("BUY", "none"), "no wishlist slot")
        last = ActionResult(False, Action("BUY", "none"), "buy failed")
        for slot in slots:
            last = self._buy_one(slot)
            if not last.success:
                return last
        return last

    def buy_open_shop(self, limit: int = 1, should_stop=None) -> ActionResult:
        """Buy wishlist items on the shop already on screen. Does not open newspaper."""
        last = ActionResult(False, Action("BUY", "none"), "no purchase")
        for _ in range(max(1, limit)):
            if should_stop and should_stop():
                return ActionResult(False, Action("STOP", "buy_shop"), "stopped")
            last = self.buy_wishlist()
            if not last.success:
                return last
        return last

    def capture_open_shop(self, should_stop=None) -> ActionResult:
        """Crop crate icons on the open shop into the icon library."""
        self.matcher.reload()
        action = Action("CAPTURE", "shop")
        png = self.device.screenshot()
        screen = self.screens.detect(png)
        self._debug_shot(png, screen, "capture_shop.png")
        if screen.screen is GameScreen.POPUP:
            self._close_popup(screen)
            return ActionResult(False, action, "popup open")
        if screen.screen is not GameScreen.PLAYER_SHOP:
            return ActionResult(False, action, "open the player shop first")
        icons = self.news.find_crate_icons(png)
        if not icons:
            return ActionResult(False, action, "no priced crate on screen")
        saved: list[str] = []
        skipped = 0
        for icon in icons:
            if should_stop and should_stop():
                return ActionResult(False, Action("STOP", "capture"), "stopped")
            name = self._save_capture_icon(icon.image, "shop")
            if name is None:
                skipped += 1
                continue
            saved.append(name)
            log.info(f"CAPTURE library {name} {icon.width}x{icon.height}")
        action.target = ",".join(saved) or "none"
        if not saved:
            return ActionResult(False, action, f"already in library n={skipped}")
        return ActionResult(True, action)

    def capture_newspaper_ads(self, should_stop=None) -> ActionResult:
        """Crop Daily Dirt ad icons into the newspaper library."""
        self.matcher.reload()
        action = Action("CAPTURE", "newspaper")
        png = self.device.screenshot()
        if not self._newspaper_is_open(png):
            return ActionResult(False, action, "open the newspaper first")
        ads = self.news.find_ads(png, left_page=self._news_left_page)
        if not ads:
            return ActionResult(False, action, "no newspaper ads")
        saved: list[str] = []
        skipped = 0
        for ad in ads:
            if should_stop and should_stop():
                return ActionResult(False, Action("STOP", "capture"), "stopped")
            crop = self.news.ad_item_icon(png, ad)
            name = self._save_capture_icon(crop, "news")
            if name is None:
                skipped += 1
                continue
            saved.append(name)
            log.info(f"CAPTURE library {name}")
        action.target = ",".join(saved) or "none"
        if not saved:
            return ActionResult(False, action, f"already in library n={skipped}")
        return ActionResult(True, action)

    def scan_ads(self) -> ActionResult:
        """Match wishlist ads on the current newspaper page. Does not tap."""
        self.matcher.reload()
        self.wishlist = active_wishlist()
        self._sync_vision_thresholds()
        png = self.device.screenshot()
        ads = self.news.find_ads(png, left_page=self._news_left_page)
        missing_news = [
            item
            for item, spec in self.wishlist.items()
            if (spec.get("news_template") or item_news_template_name(item))
            not in self.matcher.names
        ]
        if missing_news:
            log.info(f"SCAN_ADS missing *_news.png {','.join(missing_news)}")
        matched = self.news.find_wishlist_ads(
            png, self.wishlist, left_page=self._news_left_page, ads=ads
        )
        if self.config.debug:
            self._debug_shot(png, None, "scan_ads.png")
        items = ",".join(item for _ad, item in matched) or "none"
        action = Action("SCAN_ADS", items)
        if not ads:
            return ActionResult(False, action, "no newspaper ads")
        log.info(
            f"SCAN_ADS left={self._news_left_page} ads={len(ads)} wishlist={items}"
        )
        return ActionResult(True, action)

    def swipe_newspaper(self) -> ActionResult:
        action = Action("SWIPE", "newspaper")
        if self._news_left_page >= NEWS_PAGE_COUNT:
            action.target = "last page"
            log.info("SWIPE newspaper already on page 10")
            return ActionResult(True, action)
        self._turn_newspaper()
        png = self.device.screenshot()
        ads = self.news.find_ads(png, left_page=self._news_left_page)
        if self.config.debug:
            self._debug_shot(png, None, "after_swipe_newspaper.png")
        action.target = f"left={self._news_left_page} ads={len(ads)}"
        log.info(f"SWIPE newspaper left={self._news_left_page} ads={len(ads)}")
        return ActionResult(True, action)

    def visit_next_shop(self) -> ActionResult:
        """Tap the first unused wishlist ad on the current page."""
        self.matcher.reload()
        self.wishlist = active_wishlist()
        self._sync_vision_thresholds()
        png = self.device.screenshot()
        ad = self._unused_wishlist_ad(png)
        if ad is None:
            return ActionResult(
                False, Action("VISIT_SHOP", "none"), "no unused wishlist ad on this page"
            )
        return self.visit_shop(ad)

    def close_shop(self) -> ActionResult:
        """Tap the stall/newspaper X. Same HUD point every time — no screenshot."""
        action = Action("CLOSE_SHOP", "shop")
        action.x, action.y = self._tap_hud("shop_close")
        time.sleep(self.wait_s)
        self.camera.clear_history()
        log.info(f"CLOSE_SHOP {action.x},{action.y}")
        return ActionResult(True, action)

    def _already_on_wishlist(self, crop, *, scene: str = "shop") -> str | None:
        for item, spec in load_wishlist().items():
            hit = self.news._match_wishlist_item(crop, item, spec, scene=scene)
            if hit is not None:
                return item
        return None

    def _save_capture_icon(self, crop, kind: str) -> str | None:
        scene = "news" if kind == "news" else "shop"
        known = self._already_on_wishlist(crop, scene=scene)
        if known is not None:
            if kind == "shop":
                log.info(f"CAPTURE skip already {known}")
                return None
            news_name = item_news_template_name(known)
            if news_name in self.matcher.names:
                log.info(f"CAPTURE news skip already {known}")
                return None
        row = save_library_png(kind, bgr_png_bytes(crop), skip_similar=True)
        if row.get("duplicate"):
            log.info(f"CAPTURE skip library {row['id']}")
            return None
        return row["id"]

    def _buy_one(self, slot: ShopSlot) -> ActionResult:
        action = Action("BUY", slot.item, *slot.center, crop=slot.item)
        self.device.tap(*slot.center)
        time.sleep(self.wait_s)
        after = self.device.screenshot()
        after_screen = self.screens.detect(after)
        if after_screen.screen is GameScreen.POPUP:
            self._close_popup(after_screen)
            return ActionResult(False, action, "diamond/popup — closed, not spent")
        after_slots = self.news.find_slots(after, self.wishlist)
        still = [
            s for s in after_slots if s.item == slot.item and _overlap(s, slot) > 0.4
        ]
        if still:
            return ActionResult(False, action, "verify failed")
        log.info(f"BUY {slot.item} SUCCESS")
        return ActionResult(True, action)

    def _next_ad(self) -> DetectedObject | None:
        png = self.device.screenshot()
        wanted = self._unused_wishlist_ad(png)
        if wanted is not None:
            return wanted
        while self._news_left_page < NEWS_PAGE_COUNT:
            self._turn_newspaper()
            wanted = self._unused_wishlist_ad(self.device.screenshot())
            if wanted is not None:
                return wanted
        return None

    def _ad_cell(self, ad: DetectedObject) -> tuple:
        """Stable listing id: (page, slot). Same pixels on another spread are a different shop."""
        width, height = self.device.resolution()
        cx = ad.x + max(1, ad.width) // 2
        cy = ad.y + max(1, ad.height) // 2
        for page, slot, x, y, w, h in news_spread_slots(
            width, height, self._news_left_page
        ):
            if x <= cx < x + w and y <= cy < y + h:
                return (page, slot)
        return (self._news_left_page, ad.x, ad.y)

    def _unused_wishlist_ad(self, png) -> DetectedObject | None:
        for ad, item in self.news.find_wishlist_ads(
            png, self.wishlist, left_page=self._news_left_page
        ):
            cell = self._ad_cell(ad)
            if cell in self._visited_ads:
                log.info(f"VISIT_SHOP skip visited {cell}")
                continue
            log.info(f"VISIT_SHOP match {item} ad={ad.x},{ad.y} cell={cell}")
            return ad
        return None

    def _turn_newspaper(self) -> None:
        self._swipe_next_pages()
        if self._news_left_page < NEWS_PAGE_COUNT:
            self._news_left_page = min(NEWS_PAGE_COUNT, self._news_left_page + 2)
        time.sleep(self.wait_s)

    def _swipe_next_pages(self) -> None:
        width, height = self.device.resolution()
        y = height // 2
        self.device.swipe(int(width * 0.72), y, int(width * 0.28), y, 280)

    def _missing_item_templates(self) -> list[str]:
        missing: list[str] = []
        for item, spec in self.wishlist.items():
            name = spec.get("template") or f"item_{item}"
            if name not in self.matcher.names:
                missing.append(name)
        return missing

    def _abort_popup(self, png, action: Action) -> ActionResult | None:
        screen = self.screens.detect(png)
        if screen.screen is GameScreen.POPUP:
            self._close_popup(screen)
            return ActionResult(False, action, "popup open")
        return None

    def _close_popup(self, screen: ScreenDetection) -> None:
        closes = [obj for obj in screen.objects if obj.type == "popup" and obj.state == "close"]
        if not closes:
            self.device.back()
            return
        close = max(closes, key=lambda o: o.confidence)
        self.device.tap(close.x + close.width // 2, close.y + close.height // 2)

    def _debug_shot(self, png, screen: ScreenDetection | None, name: str) -> None:
        if not self.config.debug:
            return
        detection = screen if screen is not None else self.screens.detect(png)
        dest = SCREENSHOT_DIR / "debug" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        extra = []
        stand = self.news.find_stand(png)
        if stand is not None:
            extra.append(stand)
        extra.extend(self.news.find_ads(png, left_page=self._news_left_page))
        combined = ScreenDetection(
            detection.screen, detection.confidence, [*detection.objects, *extra]
        )
        save_overlay(png, combined, dest.with_name(name.replace(".png", "_overlay.png")))


def _center(obj: DetectedObject) -> tuple[int, int]:
    return obj.x + obj.width // 2, obj.y + obj.height // 2


def _overlap(a: ShopSlot, b: ShopSlot) -> float:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.width, b.x + b.width)
    y2 = min(a.y + a.height, b.y + b.height)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union else 0.0
