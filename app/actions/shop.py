"""Newspaper shopping: find column, visit houses, buy wishlist items."""

from __future__ import annotations

import time
from datetime import datetime
from typing import NamedTuple

from app.actions.farming import Action, ActionResult
from app.actions.timing import LoopTiming
from app.config import SCREENSHOT_DIR, AppConfig
from app.controller.camera import CameraManager
from app.controller.device import DeviceController, frame_png_bytes
from app.storage.botdata import (
    active_wishlist,
    clear_column,
    item_news_template_name,
    load_column,
    load_wishlist,
    record_purchase,
    save_column,
    save_library_png,
)
from app.storage.logger import get_logger
from app.vision.detector import DetectedObject, box_iou
from app.vision.newspaper import (
    SAME_VIEW_RESIDUAL,
    NewspaperDetector,
    ShopSlot,
    ad_cell_is_dim,
    bgr_png_bytes,
    crate_proof_crop,
    crate_view_shift,
    newspaper_fingerprint,
    newspaper_fingerprints_differ,
    stall_end_cloth,
    stall_end_in_view,
    stall_view_residual,
)
from app.vision.overlay import save_overlay
from app.vision.regions import (
    NEWS_OPEN_LEFT_PAGE,
    NEWS_PAGE_COUNT,
    NEWS_PROMO_SLOTS,
    STALL_PAN_MAX,
    hud_tap,
    news_spread_slots,
    stall_swipe_px,
)
from app.vision.screen import (
    GameScreen,
    ScreenDetection,
    ScreenDetector,
    popup_close_point,
)
from app.vision.template_matcher import TemplateMatcher, as_bgr

log = get_logger("ACTION")

# Two settled frames this close apart show the same view: the table is against
# an edge, or the game swallowed the swipe.
STALL_STILL_PX = 8.0
# An edge has a little give, so a swipe into one still shifts the table 25-30px
# before it springs back. Below this the table crept without carrying a new
# window into view.
STALL_CREEP_PX = 40.0
FAILURE_FRAMES_KEPT = 10
# popup_close matched the paper's X at 0.88-0.95 on every page and the cover.
PAPER_X_THRESHOLD = 0.8
# A friend's farm has taken 13-36s to load (the game shows "Connecting.."
# when the network is slow); the home farm ~5s. Polls return as soon as the
# screen is there, so this is only a ceiling.
GO_HOME_LOAD_S = 60.0
# The cart (hud_shop) reads 0.96 on the home farm and does not match at all on
# a friend's farm, which shows a house button in that corner instead.
HOME_CART_THRESHOLD = 0.8
# Measured centre on every page: exactly (1689, 126). The stall's X: (1768, 95).
PAPER_X_TOLERANCE_PX = 25


class StallPan(NamedTuple):
    moved: bool
    dx: float
    png: object


class NewspaperActions:
    def __init__(
        self,
        device: DeviceController,
        config: AppConfig | None = None,
        screens: ScreenDetector | None = None,
        matcher: TemplateMatcher | None = None,
        news: NewspaperDetector | None = None,
        camera: CameraManager | None = None,
        wait_s: float | None = None,
        buy_wait_s: float | None = None,
        visit_wait_s: float | None = None,
        poll_interval_s: float | None = None,
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
        self.wait_s = (
            float(self.config.action_wait_s) if wait_s is None else wait_s
        )
        self.buy_wait_s = (
            float(self.config.buy_wait_s) if buy_wait_s is None else buy_wait_s
        )
        self.visit_wait_s = (
            float(self.config.visit_wait_s) if visit_wait_s is None else visit_wait_s
        )
        self.poll_interval_s = (
            float(getattr(self.config, "poll_interval_s", 0.0))
            if poll_interval_s is None
            else poll_interval_s
        )
        self.stall_swipe_ms = int(getattr(self.config, "stall_swipe_ms", 280))
        self.retries = retries
        self.wishlist = active_wishlist()
        self.timing = LoopTiming()
        self._visited_ads: set[tuple] = set()
        self._planned_ads: list[tuple] = []
        self._news_fp = None
        self._news_left_page = NEWS_OPEN_LEFT_PAGE
        # Verdict of the last frame a poll classified, so the caller can read
        # it back instead of paying for detect twice on the same frame.
        self._last_screen = None
        # Screenshot handed from one step to the next so the same frame is not
        # captured twice (a screencap costs far more than a sleep).
        self._pending_png = None
        self._pending_screen: ScreenDetection | None = None
        self._pending_buys: list[tuple] = []
        self._templates_loaded = False
        self._screen_size: tuple[int, int] | None = None
        try:
            self.device.timing = self.timing
        except AttributeError:
            pass

    def _sync_vision_thresholds(self) -> None:
        self.news.buy_threshold = self.config.buy_threshold
        self.news.news_threshold = self.config.news_threshold

    def _publish_waits(self) -> None:
        self.timing.set_waits(
            action_wait_s=self.wait_s,
            buy_wait_s=self.buy_wait_s,
            visit_wait_s=self.visit_wait_s,
            poll_interval_s=self.poll_interval_s,
        )

    # --- measured device I/O -------------------------------------------------

    def _shot(self):
        """Every screenshot in the loop goes through here so it is counted."""
        self._drop_pending()
        with self.timing.adb(shot=True):
            return self.device.screenshot()

    def _tap(self, x: int, y: int) -> None:
        self._drop_pending()
        with self.timing.adb():
            self.device.tap(x, y)

    def _swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int | None = None) -> None:
        self._drop_pending()
        with self.timing.adb():
            self.device.swipe(x1, y1, x2, y2, ms)

    def _resolution(self) -> tuple[int, int]:
        """Cached: `wm size` is an ADB round trip and _ad_cell asks per ad."""
        if self._screen_size is None:
            with self.timing.adb():
                self._screen_size = self.device.resolution()
        return self._screen_size

    def _sleep(self, seconds: float) -> None:
        self.timing.sleep(seconds)

    def _stash_png(self, png, screen: ScreenDetection | None = None) -> None:
        """Hand the frame just captured to the next step. Any tap, swipe or
        screenshot invalidates it, so a stale frame can never be acted on."""
        self._pending_png = png
        self._pending_screen = screen

    def _drop_pending(self) -> None:
        self._pending_png = None
        self._pending_screen = None

    def _take_pending(self):
        png, screen = self._pending_png, self._pending_screen
        self._pending_png = None
        self._pending_screen = None
        return png, screen

    def _poll_until(self, check, timeout_s: float, step: str, first_png=None):
        """Screenshot until check(png) is truthy or the deadline passes.

        Returns (last_png, value). timeout_s is a ceiling, not a sleep: a shop
        that draws in 0.4s costs 0.4s even when the ceiling is 2.5s.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        png = first_png
        while True:
            if png is None:
                png = self._shot()
            value = check(png)
            if value:
                return png, value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.timing.deadline_hit(step)
                return png, value
            if self.poll_interval_s > 0:
                self._sleep(min(self.poll_interval_s, remaining))
            png = None

    def shop_from_newspaper(
        self,
        limit: int = 1,
        should_stop=None,
        mode: str = "follow",
        reset_home: bool = True,
        until_done: bool = False,
    ) -> ActionResult:
        kind = _shop_mode(mode)
        if kind == "browse":
            return self.browse_newspaper(
                limit=limit, should_stop=should_stop, reset_home=reset_home
            )
        self.timing.reset()
        self._publish_waits()
        try:
            return self._shop_loop(kind, limit, should_stop, reset_home, until_done)
        finally:
            # Baseline line to compare against after every wait change.
            log.info(self.timing.summary_line())

    def _shop_loop(
        self,
        kind: str,
        limit: int,
        should_stop,
        reset_home: bool,
        until_done: bool,
    ) -> ActionResult:
        home = self._reset_home_if_needed(reset_home)
        if home is not None and not home.success:
            return home
        self.matcher.reload_if_changed()
        self.wishlist = active_wishlist()
        self._sync_vision_thresholds()
        self._templates_loaded = True
        bought = 0
        visits = 0
        max_visits = (
            max(80, NEWS_PAGE_COUNT * 12)
            if until_done or kind == "sweep"
            else max(8, max(1, limit) * 6)
        )
        last = ActionResult(False, Action("BUY", "none"), "no purchase")
        exhausted = False
        while visits < max_visits:
            if not until_done and bought >= max(1, limit):
                break
            if should_stop and should_stop():
                return ActionResult(False, Action("STOP", "newspaper"), "stopped")
            visits += 1
            with self.timing.step("shop") as shop_step:
                # After close_shop the camera sits on that farm's house, same as home.
                found = self.find_column()
                if not found.success:
                    return found
                opened = self.open_newspaper()
                if not opened.success:
                    return opened
                ad = self._next_listing(kind)
                if ad is None:
                    shop_step.discard = True
                    exhausted = True
                    # "Same paper, nothing left" returns with the paper open.
                    self.close_newspaper()
                    break
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
            log.info(self.timing.step_line("shop"))
        self._templates_loaded = False
        if last.success or bought or exhausted:
            parked = self.go_home()
            if not parked.success:
                return parked
        if until_done:
            self.reset_loop_state()
        if until_done and exhausted:
            return ActionResult(True, Action("VISIT_SHOP", "done"), "all shops done")
        if exhausted and not last.success and not bought:
            return ActionResult(
                False,
                Action("VISIT_SHOP", "none"),
                "no newspaper ads",
            )
        return last

    def reset_loop_state(self) -> None:
        """Drop visited/plan memory so the next cycle scans the newspaper fresh."""
        self._visited_ads.clear()
        self._planned_ads = []
        self._news_fp = None
        self._news_left_page = NEWS_OPEN_LEFT_PAGE
        if self.camera is not None:
            self.camera.clear_history()
        log.info("NEWS reset loop state")

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
                self.close_newspaper()
                return ActionResult(False, Action("STOP", "browse"), "stopped")
            png = self._shot()
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
        closed = self.close_newspaper()
        if not closed.success:
            return closed
        log.info(f"BROWSE SUCCESS items={unique}")
        return ActionResult(True, action)

    def go_home(self) -> ActionResult:
        """Return to the house-centered farm via friends → visit → close → shop_home.

        Every step waits for what the game shows rather than for a fixed time:
        a friend's farm has taken 15s+ to load, and it opens its stall on
        arrival, so taps sent on a timer landed on the loading screen and left
        the bot parked in that stall. Succeeds only on the home farm (the cart
        button bottom-left; a friend's farm shows a house button there).
        """
        action = Action("GO_HOME", "house")
        with self.timing.step("go_home", deadline_s=GO_HOME_LOAD_S):
            cleared = self._clear_overlay()
            if not cleared.success:
                log.info(f"GO_HOME FAIL {cleared.error}")
                return ActionResult(False, action, cleared.error)
            png, _ = self._take_pending()
            png = png if png is not None else self._shot()
            where = self._where(png)
            if where != "friend":
                # Already home (or unsure): the house button of a visited farm
                # is what re-centres the camera on the house, so visit a friend
                # first. From anyone else's farm that button is one tap away.
                fx, fy = self._tap_hud("hud_friends")
                action.x, action.y = fx, fy
                self._sleep(self.wait_s)
                self._tap_hud("friends_first")
                # Home is not an answer here: with the friends list still open
                # over the home farm, the cart shows and the visit has not
                # happened yet.
                png, where = self._poll_where(("friend", "stall"), GO_HOME_LOAD_S)
                if where is None:
                    where = self._where(png)
            # stall → friend's farm → home, and a stall may open on either farm.
            for _ in range(4):
                if where == "stall":
                    # The stall keeps drawing while it closes: wait for it to go,
                    # not for the next frame that shows anything at all.
                    self._tap_hud("shop_close")
                    png, where = self._poll_where(("home", "friend"), self.visit_wait_s)
                elif where == "friend":
                    self._tap_hud("shop_home")
                    png, where = self._poll_where(("home", "stall"), GO_HOME_LOAD_S)
                else:
                    break
            self._stash_png(png)
            self.camera.clear_history()
            if where != "home":
                log.info(f"GO_HOME FAIL farm not drawn ({where or 'loading'})")
                return ActionResult(False, action, "farm not drawn")
            log.info("GO_HOME house")
            return ActionResult(True, action)

    def _poll_where(self, wanted: tuple[str, ...], timeout_s: float):
        """Poll until _where reads one of `wanted`; (frame, where or None)."""
        png, where = self._poll_until(
            lambda frame: (lambda w: w if w in wanted else None)(self._where(frame)),
            timeout_s,
            step="go_home",
        )
        return png, where

    def _where(self, png) -> str | None:
        """'home', 'friend' (someone else's farm), 'stall', or None (loading
        or anything else)."""
        screen = self.screens.detect(png)
        self._last_screen = screen
        if screen.screen is GameScreen.PLAYER_SHOP:
            return "stall"
        if screen.screen is not GameScreen.FARM:
            return None
        cart = self.matcher.match_one(as_bgr(png), "hud_shop", threshold=HOME_CART_THRESHOLD)
        if cart is None:
            return "friend"
        # The friends list covers the friends button; the farm under it is not
        # a place to act on yet.
        if not any(obj.state == "friends" for obj in screen.objects):
            return None
        return "home"

    def _clear_overlay(self) -> ActionResult:
        """Close a paper or stall left on top of the farm."""
        png, screen = self._take_pending()
        png = png if png is not None else self._shot()
        screen = screen or self.screens.detect(png)
        # The stall first: its X sits close enough to the paper's that a
        # stall must never be taken for a paper (tapping the paper's spot on a
        # stall only hits the awning).
        if screen.screen is GameScreen.PLAYER_SHOP:
            self._tap_hud("shop_close")
            _png, where = self._poll_where(("home", "friend"), self.visit_wait_s)
            if where is None:
                return ActionResult(False, Action("CLOSE_SHOP", "shop"), "stall still open")
            return ActionResult(True, Action("CLOSE_SHOP", "shop"))
        if screen.screen is GameScreen.NEWSPAPER or self._paper_x_visible(png):
            return self.close_newspaper(png)
        return ActionResult(True, Action("CLEAR", screen.screen.value))

    def close_newspaper(self, png=None) -> ActionResult:
        """Tap the paper's own X and wait for the farm behind it.

        The X sits at one spot on every page, not where the stall's X is, and
        the Back key opens the game's exit dialog, so neither close_shop nor
        Back can close the paper. It is only tapped when seen: blind, that spot
        on the farm is next to the coin and diamond counters.
        """
        action = Action("CLOSE_NEWSPAPER", "newspaper")
        with self.timing.step("close_newspaper", deadline_s=self.visit_wait_s):
            for _ in range(2):
                if png is None:
                    png = self._shot()
                if not self._paper_x_visible(png):
                    if self._farm_drawn(png):
                        self._stash_png(png)
                        return ActionResult(True, action)
                    return ActionResult(False, action, "newspaper X not on screen")
                action.x, action.y = self._tap_hud("newspaper_close")
                png, home = self._poll_until(
                    self._farm_drawn, self.visit_wait_s, step="close_newspaper"
                )
                if home:
                    self._stash_png(png)
                    self.camera.clear_history()
                    log.info(f"CLOSE_NEWSPAPER {action.x},{action.y}")
                    return ActionResult(True, action)
            log.info("CLOSE_NEWSPAPER FAIL still open")
            return ActionResult(False, action, "newspaper still open")

    def _paper_x_visible(self, png) -> bool:
        """The paper's red X at its fixed spot.

        The stall's X is the same art ~85px away, so the match has to land on
        the paper's spot, not just near it.
        """
        image = as_bgr(png)
        h, w = image.shape[:2]
        cx, cy = hud_tap("newspaper_close", w, h)
        pad = int(round(0.06 * w))
        x0, y0 = max(0, cx - pad), max(0, cy - pad)
        roi = image[y0 : cy + pad, x0 : cx + pad]
        hit = self.matcher.match_one(
            roi, "popup_close", threshold=PAPER_X_THRESHOLD, scales=(1.0,)
        )
        if hit is None:
            return False
        hx = x0 + hit.x + hit.width // 2
        hy = y0 + hit.y + hit.height // 2
        return abs(hx - cx) <= PAPER_X_TOLERANCE_PX and abs(hy - cy) <= PAPER_X_TOLERANCE_PX

    def _farm_drawn(self, png) -> bool:
        """Own/visited farm is up: HUD showing and no stall or newspaper on top."""
        screen = self.screens.detect(png)
        return screen.screen is GameScreen.FARM

    def _tap_hud(self, name: str) -> tuple[int, int]:
        width, height = self._resolution()
        point = hud_tap(name, width, height)
        self._tap(*point)
        return point

    def _stall_rewind(self, png):
        """Pan left until the table stops moving, and hand back that frame.

        Swipes that come one after another carry the table a different distance
        than a deliberate nudge does, so counting swipes never lands on the
        left edge. Measuring each one does.
        """
        for _ in range(STALL_PAN_MAX + 2):
            if self._stall_ends_at(png, "left"):
                return png
            # Across ~25 live rewinds a still first swipe never moved on a
            # second one, so the rewind trusts it and spares ~1.3s a stall.
            pan = self._stall_pan(png, "left", step="stall_rewind", confirm=False)
            png = pan.png
            if not pan.moved:
                log.info("STALL rewound to the left edge")
                return png
        self.timing.deadline_hit("stall_rewind")
        log.info("STALL rewind gave up at the pan cap")
        return png

    def _stall_ends_at(self, png, side: str) -> bool:
        """True when the table visibly runs out on that side.

        A crate that could still be panned into view always sits within one
        crate gap of the post, so a wider run of bare cloth proves that end is
        in view without a swipe. A wide stall shows no such run even at its
        ends; there only _stall_pan finding the table still can tell.
        """
        if not stall_end_in_view(png, side):
            log.debug(f"STALL {side} end not proven, cloth={stall_end_cloth(png, side)}px")
            return False
        log.info(f"STALL {side} end in view, cloth={stall_end_cloth(png, side)}px")
        return True

    def _stall_pan(
        self,
        png,
        direction: str,
        *,
        step: str = "stall_pan",
        confirm: bool = True,
    ) -> StallPan:
        """Swipe once and report whether the table carried a new window in.

        confirm=True spends a second swipe before calling a still table an
        edge. The sweep asks it of its first swipe and of the first after a
        tap: the game was seen dropping the swipe that followed the rewind's
        bounce. Anywhere else a still table is the edge.
        """
        with self.timing.step(step):
            self._swipe_stall(direction)
            after = self._stall_settled(step)
            travel, moved = self._stall_moved(png, after)
            if not moved and confirm:
                first = travel
                self._swipe_stall(direction)
                after = self._stall_settled(step)
                travel, moved = self._stall_moved(png, after)
                log.debug(
                    f"STALL {direction} confirm first_dx={first:.0f} "
                    f"second_dx={travel:.0f} moved={moved}"
                )
            if not moved:
                log.info(f"STALL edge {direction} dx={travel:.0f}")
                return StallPan(False, travel, after)
            log.info(f"STALL pan {direction} dx={travel:.0f}")
            return StallPan(True, travel, after)

    def _stall_moved(self, before, after) -> tuple[float, bool]:
        """(|shift|, moved). A shift under STALL_CREEP_PX is only trusted when
        undoing it leaves the same picture: on a row of look-alike crates a pan
        of whole columns reads as a few px."""
        dx = crate_view_shift(before, after)
        travel = abs(dx)
        if travel >= STALL_CREEP_PX:
            return travel, True
        if stall_view_residual(before, after, dx) >= SAME_VIEW_RESIDUAL:
            log.info(f"STALL shift {dx:.0f} is a whole-column pan, not a creep")
            return travel, True
        return travel, False

    def _stall_settled(self, step: str = "stall_pan"):
        """Wait out the glide: the table keeps sliding after the finger lifts,
        and a frame caught mid-glide measures a distance never travelled."""
        deadline = time.monotonic() + max(0.0, self.wait_s)
        png = self._shot()
        while time.monotonic() < deadline:
            nxt = self._shot()
            dx = crate_view_shift(png, nxt)
            # A glide of whole columns between two shots also reads as a
            # small shift; the residual tells it from a table at rest.
            if abs(dx) < STALL_STILL_PX and (
                stall_view_residual(png, nxt, dx) < SAME_VIEW_RESIDUAL
            ):
                return nxt
            png = nxt
        self.timing.deadline_hit(step)
        return png

    def _swipe_stall(self, direction: str) -> None:
        width, height = self._resolution()
        x1, y1, x2, y2 = stall_swipe_px(width, height, direction)
        self._swipe(x1, y1, x2, y2, self.stall_swipe_ms)

    def _reset_home_if_needed(self, reset_home: bool) -> ActionResult | None:
        if not reset_home:
            return None
        png = self._shot()
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
        with self.timing.step("find_column"):
            # close_shop / go_home already captured the farm — reuse that frame.
            png, _ = self._take_pending()
            png = png if png is not None else self._shot()
            stand = self.news.find_stand(png)
            if stand is not None:
                log.info("FIND_COLUMN already on screen")
                return self._remember_stand(action, stand)
            self.camera.pan_to_column()
            self._sleep(self.wait_s)
            png = self._shot()
            stand = self.news.find_stand(png)
            if stand is None:
                log.info("FIND_COLUMN FAIL stand not on screen after pan")
                return ActionResult(False, action, "newspaper stand not found")
            return self._remember_stand(action, stand)

    def open_newspaper(self) -> ActionResult:
        action = Action("OPEN_NEWSPAPER", "newspaper_stand")
        # Opening the paper is a screen transition, so it gets the screen
        # ceiling: one screencap can outlast action_wait_s on its own, which
        # would leave the poll with a single look taken before the paper drew.
        deadline = self.visit_wait_s
        with self.timing.step("open_newspaper", deadline_s=deadline):
            cached = load_column()
            if cached is not None:
                action.x, action.y = cached
                self._tap(*cached)
                _, opened = self._poll_until(
                    self._newspaper_is_open, deadline, step="open_newspaper"
                )
                if opened:
                    return self._opened_at_first_spread(action)
                log.info("FIND_COLUMN cache miss — searching")
                clear_column()
            # close_shop already captured the farm behind the stall.
            png, _ = self._take_pending()
            png = png if png is not None else self._shot()
            if self._newspaper_is_open(png):
                return self._opened_at_first_spread(action)
            stand = self.news.find_stand(png)
            if stand is None:
                return ActionResult(False, action, "newspaper stand not on screen")
            action.x, action.y = _center(stand)
            save_column(*_center(stand))
            self._tap(*_center(stand))
            after, opened = self._poll_until(
                self._newspaper_is_open, deadline, step="open_newspaper"
            )
            self._debug_shot(after, None, "after_open_newspaper.png")
            if opened:
                return self._opened_at_first_spread(action)
            clear_column()
            self._keep_failure_frame(after, "open_fail")
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
        source = png if png is not None else self._shot()
        image = as_bgr(source)
        hit = self.matcher.match_one(image, "newspaper_ad", threshold=0.72)
        if hit is None:
            return False
        # The left leaf (page 2) finishes turning after the right one. Scanning
        # at that moment plans only page 3 and never comes back.
        return self._left_page_has_listing(image, NEWS_OPEN_LEFT_PAGE)

    def _left_page_has_listing(self, image, left: int) -> bool:
        h, w = image.shape[:2]
        for page, slot, x, y, bw, bh in news_spread_slots(w, h, left):
            if page != left or (page, slot) in NEWS_PROMO_SLOTS:
                continue
            if self.news._slot_coin(image[y : y + bh, x : x + bw]) is not False:
                return True
        return False

    def _shop_or_popup(self, png) -> ScreenDetection | None:
        """Terminal states after tapping an ad: the stall, or a popup to close."""
        screen = self.screens.detect(png)
        self._last_screen = screen
        if screen.screen in (GameScreen.PLAYER_SHOP, GameScreen.POPUP):
            return screen
        return None

    def visit_shop(self, ad: DetectedObject) -> ActionResult:
        action = Action("VISIT_SHOP", "ad", ad.x + ad.width // 2, ad.y + ad.height // 2)
        self._visited_ads.add(self._ad_cell(ad))
        with self.timing.step("visit_shop", deadline_s=self.visit_wait_s):
            self._tap(action.x, action.y)
            self._last_screen = None
            png, screen = self._poll_until(
                self._shop_or_popup, self.visit_wait_s, step="visit_shop"
            )
            if screen is None:
                # The poll already classified this frame; re-running detect on
                # it only burns another second to reach the same verdict.
                screen = self._last_screen or self.screens.detect(png)
            if screen.screen is GameScreen.UNKNOWN:
                # Still on the loading screen: the farm is on its way (13-36s
                # on a slow network). Giving up now leaves the bot tapping
                # into a farm that opens its stall a few seconds later.
                log.info("VISIT_SHOP still loading — waiting longer")
                png, found = self._poll_until(
                    self._shop_or_popup,
                    max(0.0, GO_HOME_LOAD_S - self.visit_wait_s),
                    step="visit_shop",
                )
                screen = found or self._last_screen or self.screens.detect(png)
            self._debug_shot(png, screen, "after_visit_shop.png")
            if screen.screen is GameScreen.POPUP:
                self._close_popup(screen)
                self.timing.mark("visit_fail")
                return ActionResult(False, action, "diamond/popup — closed, not spent")
            if screen.screen is GameScreen.PLAYER_SHOP:
                self._stash_png(png, screen)
                return ActionResult(True, action)
            self.timing.mark("visit_fail")
            log.info(f"VISIT_SHOP FAIL screen={screen.screen.value}")
            return ActionResult(False, action, f"screen={screen.screen.value}")

    def buy_wishlist(self, should_stop=None, max_buys: int | None = None) -> ActionResult:
        with self.timing.step("buy_wishlist"):
            try:
                return self._buy_loop(should_stop, max_buys)
            finally:
                # Proof PNGs and purchases.json are written once, outside the
                # tap-verify-tap window.
                self._flush_buys()

    def _flush_buys(self) -> None:
        pending, self._pending_buys = self._pending_buys, []
        for item, kind, image, qty in pending:
            record_purchase(item, kind=kind, image=image, qty=qty)

    def _buy_loop(self, should_stop=None, max_buys: int | None = None) -> ActionResult:
        if not self._templates_loaded:
            self.matcher.reload_if_changed()
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
        # visit_shop already captured and classified this frame.
        png, screen = self._take_pending()
        if png is None:
            png = self._shot()
        if screen is None:
            screen = self.screens.detect(png)
        if screen.screen not in (GameScreen.PLAYER_SHOP, GameScreen.POPUP):
            return ActionResult(
                False, Action("BUY", "none"), f"screen={screen.screen.value}"
            )
        last = ActionResult(False, Action("BUY", "none"), "no wishlist slot")
        bought = 0
        matched: set[str] = set()
        skipped: list[ShopSlot] = []

        def sweep(png) -> tuple[object, str]:
            """Buy out each window from the left edge to the right one.

            Returns the last frame and why the leg ended: edge, stop, limit or
            cap.
            """
            nonlocal last, bought
            at_end = False
            # The first swipe of the leg, and the first after a crate was
            # tapped, are the ones a still stall may have dropped.
            fresh = True
            for _ in range(STALL_PAN_MAX + 1):
                if should_stop and should_stop():
                    return png, "stop"
                with self.timing.step("find_slots"):
                    slots = [
                        slot
                        for slot in self.news.find_slots(png, ready)
                        if not _slot_skipped(slot, skipped)
                    ]
                if slots:
                    for slot in slots:
                        if slot.item not in matched:
                            matched.add(slot.item)
                            self._pending_buys.append((slot.item, "match", None, None))
                            log.info(f"WISH match {slot.item} in stall")
                        proof = _slot_proof_png(png, slot)
                        qty = self.news.read_crate_qty(png, slot)
                        result = self._buy_one(slot)
                        if not result.success:
                            skipped.append(slot)
                            if not bought:
                                last = result
                            self.timing.mark("buy_skip")
                            log.info(f"BUY skip {slot.item} {result.error or 'fail'}")
                            png = self._verify_frame()
                            continue
                        self._pending_buys.append((slot.item, "buy", proof, qty))
                        last = result
                        bought += 1
                        if max_buys is not None and bought >= max_buys:
                            return png, "limit"
                    # _buy_one already looked at the stall after the tap.
                    png = self._verify_frame()
                    fresh = True
                    continue
                if at_end or self._stall_ends_at(png, "right"):
                    return png, "edge"
                pan = self._stall_pan(png, "right", confirm=fresh)
                fresh = False
                png = pan.png
                if not pan.moved:
                    if pan.dx < STALL_STILL_PX:
                        return png, "edge"
                    # The table crept against its edge without carrying a whole
                    # window in. That sliver is still a frame nobody has looked
                    # at, so inspect it before calling the leg done.
                    at_end = True
            return png, "cap"

        # A stall opens wherever the player left it, so start by finding the
        # left edge. The sweep covers those same windows on its way back out,
        # which is why this leg does not look at them.
        png = self._stall_rewind(png)
        _png, reason = sweep(png)
        if reason == "stop":
            return ActionResult(False, Action("STOP", "buy"), "stopped")
        if not bought and last.error == "no wishlist slot":
            log.info(f"BUY no slot matched missing={missing}")
        return last

    def buy_open_shop(self, limit: int = 1, should_stop=None) -> ActionResult:
        """Buy wishlist items on the shop already on screen. Does not open newspaper."""
        if should_stop and should_stop():
            return ActionResult(False, Action("STOP", "buy_shop"), "stopped")
        return self.buy_wishlist(should_stop=should_stop, max_buys=max(1, limit))

    def capture_open_shop(self, should_stop=None) -> ActionResult:
        """Crop crate icons on the open shop into the icon library."""
        self.matcher.reload()
        action = Action("CAPTURE", "shop")
        png = self._shot()
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
        png = self._shot()
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
        png = self._shot()
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
        png = self._shot()
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
        png = self._shot()
        ad = self._unused_wishlist_ad(png)
        if ad is None:
            return ActionResult(
                False, Action("VISIT_SHOP", "none"), "no unused wishlist ad on this page"
            )
        return self.visit_shop(ad)

    def close_shop(self) -> ActionResult:
        """Tap the stall/newspaper X, then wait for the farm behind it to draw.

        The frame that proves the farm is back is handed to find_column, so
        polling here costs no extra screenshot per shop.
        """
        action = Action("CLOSE_SHOP", "shop")
        with self.timing.step("close_shop"):
            action.x, action.y = self._tap_hud("shop_close")
            # Closing a stall drops straight back onto that player's farm, so
            # there is nothing to wait for. The one frame is for the next step
            # to hunt the stand in; the screencap also paces the next tap.
            self._stash_png(self._shot())
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

    def _verify_frame(self):
        """Frame _buy_one left behind, or a fresh one if it was invalidated."""
        png, _ = self._take_pending()
        return png if png is not None else self._shot()

    def _buy_one(self, slot: ShopSlot) -> ActionResult:
        action = Action("BUY", slot.item, *slot.center, crop=slot.item)
        # Only the item just tapped matters — rescanning the whole wishlist per
        # poll would be wasted matching.
        lookup = {slot.item: self.wishlist.get(slot.item) or {}}
        with self.timing.step("buy_one", deadline_s=self.buy_wait_s):
            self._tap(*slot.center)
            after, verdict = self._poll_until(
                lambda png: self._slot_taken(png, slot, lookup),
                self.buy_wait_s,
                step="buy_one",
            )
            self._stash_png(after)
            if verdict:
                self.timing.mark("buys")
                log.info(f"BUY {slot.item} SUCCESS {verdict}")
                return ActionResult(True, action)
            # Waited the whole ceiling and the crate is still on sale: the tap
            # did not register.
            self.timing.mark("verify_fail")
            return ActionResult(False, action, "verify failed")

    def _slot_taken(self, png, slot: ShopSlot, lookup: dict) -> str | None:
        """'sold' when the crate greyed out, 'gone' when it left the table."""
        if self.news.slot_sold(png, slot, had_coin=slot.has_coin):
            return "sold"
        still = [
            s
            for s in self.news.find_slots(png, lookup)
            if s.item == slot.item and box_iou(s, slot) > 0.4
        ]
        return None if still else "gone"

    def _next_listing(self, mode: str) -> DetectedObject | None:
        with self.timing.step("next_listing"):
            return self._next_listing_inner(mode)

    def _next_listing_inner(self, mode: str) -> DetectedObject | None:
        remaining = self._planned_remaining()
        if remaining:
            log.info(f"NEWS reuse plan remaining={len(remaining)} next={remaining[0]}")
            return self._seek_next_planned()
        png = self._shot()
        if self._plan_needs_refresh(png, mode):
            self._rescan_plan(mode, first_png=png)
            self.close_newspaper()
            if not self._planned_remaining():
                return None
            opened = self.open_newspaper()
            if not opened.success:
                return None
        return self._seek_next_planned()

    def _plan_needs_refresh(self, png, mode: str) -> bool:
        if not self._planned_ads:
            return True
        if self._visited_on_spread() and not self._visited_still_dim(png):
            log.info("NEWS visited listing looks fresh")
            return True
        if mode == "sweep" and self._unvisited_bright_ads(png):
            log.info("NEWS unvisited bright listing still on the spread")
            return True
        if not self._visited_on_spread():
            if self._news_fp is None:
                return True
            if newspaper_fingerprints_differ(png, self._news_fp):
                log.info("NEWS first spread listings changed")
                return True
        log.info("NEWS same paper, nothing left to visit")
        return False

    def _unvisited_bright_ads(self, png) -> bool:
        for ad in self.news.find_ads(png, left_page=self._news_left_page):
            if self._ad_cell(ad) not in self._visited_ads:
                return True
        return False

    def _visited_on_spread(self) -> bool:
        left = self._news_left_page
        right = None if left >= NEWS_PAGE_COUNT else left + 1
        for cell in self._visited_ads:
            page = cell[0]
            if page == left or (right is not None and page == right):
                return True
        return False

    def _visited_still_dim(self, png) -> bool:
        """True when every shop we tapped on this spread is still greyed out."""
        image = as_bgr(png)
        h, w = image.shape[:2]
        checked = 0
        for page, slot, x, y, bw, bh in news_spread_slots(w, h, self._news_left_page):
            if (page, slot) not in self._visited_ads:
                continue
            checked += 1
            roi = image[y : y + bh, x : x + bw]
            if not ad_cell_is_dim(roi):
                log.info(f"NEWS cell {page},{slot} is bright again")
                return False
        return checked > 0

    def _planned_remaining(self) -> list[tuple]:
        return [cell for cell in self._planned_ads if cell not in self._visited_ads]

    def _rescan_plan(self, mode: str, first_png) -> None:
        self._visited_ads.clear()
        self._planned_ads = []
        self._news_fp = newspaper_fingerprint(first_png)
        wishlist_cells: list[tuple] = []
        coin_cells: list[tuple] = []
        png = first_png
        while True:
            self._collect_spread(png, wishlist_cells, coin_cells)
            if self._news_left_page >= NEWS_PAGE_COUNT:
                break
            self._turn_newspaper()
            png = self._shot()
        if mode == "sweep":
            seen = set(wishlist_cells)
            self._planned_ads = wishlist_cells + [c for c in coin_cells if c not in seen]
        else:
            self._planned_ads = list(wishlist_cells)
        log.info(f"NEWS plan {mode} n={len(self._planned_ads)} {self._planned_ads}")

    def _collect_spread(
        self, png, wishlist_cells: list[tuple], coin_cells: list[tuple]
    ) -> None:
        ads = self.news.find_ads(png, left_page=self._news_left_page)
        matched = self.news.find_wishlist_ads(
            png, self.wishlist, left_page=self._news_left_page, ads=ads
        )
        seen_wish = set(wishlist_cells)
        for ad, item in matched:
            cell = self._ad_cell(ad)
            if cell in seen_wish:
                continue
            seen_wish.add(cell)
            wishlist_cells.append(cell)
            log.info(f"NEWS plan wishlist {item} {cell}")
        seen_coin = set(coin_cells)
        for ad in ads:
            cell = self._ad_cell(ad)
            if cell in seen_wish or cell in seen_coin:
                continue
            seen_coin.add(cell)
            coin_cells.append(cell)

    def _seek_next_planned(self) -> DetectedObject | None:
        for cell in self._planned_remaining():
            self._turn_to(_spread_left(cell[0]))
            ad = self._ad_from_cell(cell)
            if ad is None:
                log.info(f"VISIT_SHOP missing cell {cell} on spread {self._news_left_page}")
                continue
            log.info(f"VISIT_SHOP planned {cell}")
            return ad
        return None

    def _ad_from_cell(self, cell: tuple) -> DetectedObject | None:
        page, slot = cell[0], cell[1]
        width, height = self._resolution()
        for p, s, x, y, w, h in news_spread_slots(
            width, height, self._news_left_page
        ):
            if p == page and s == slot:
                return DetectedObject("newspaper", x, y, w, h, 1.0, "ad")
        return None

    def _ad_cell(self, ad: DetectedObject) -> tuple:
        """Stable listing id: (page, slot). Same pixels on another spread are a different shop."""
        width, height = self._resolution()
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

    def _turn_newspaper(self, settle: bool = True) -> None:
        self._swipe_next_pages()
        if self._news_left_page < NEWS_PAGE_COUNT:
            self._news_left_page = min(NEWS_PAGE_COUNT, self._news_left_page + 2)
        if settle:
            self._sleep(self.wait_s)

    def _turn_to(self, target_left: int) -> None:
        """Swipe back to back and wait once, for the last page only.

        Measured on the emulator, the paper takes every swipe of a quick run
        (each `input swipe` already costs ~0.6s); only the final page needs
        time to finish turning before an ad on it can be tapped.
        """
        turned = False
        while self._news_left_page < target_left:
            self._turn_newspaper(settle=False)
            turned = True
        if turned:
            self._sleep(self.wait_s)

    def _swipe_next_pages(self) -> None:
        width, height = self._resolution()
        y = height // 2
        self._swipe(int(width * 0.72), y, int(width * 0.28), y, 280)

    def _missing_item_templates(self) -> list[str]:
        missing: list[str] = []
        for item, spec in self.wishlist.items():
            name = spec.get("template") or f"item_{item}"
            if name not in self.matcher.names:
                missing.append(name)
        return missing

    def _close_popup(self, screen: ScreenDetection) -> None:
        point = popup_close_point(screen)
        if point is None:
            # Back would open the game's "exit?" dialog, not close this.
            log.info("POPUP without a visible X — left alone")
            return
        self._tap(*point)

    def _keep_failure_frame(self, png, prefix: str) -> None:
        """Save the frame a step gave up on, even with debug off.

        Rare by nature, so the newest FAILURE_FRAMES_KEPT are enough to see
        what the game showed instead of the expected screen.
        """
        folder = SCREENSHOT_DIR / "debug"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = folder / f"{prefix}_{stamp}.png"
            target.write_bytes(frame_png_bytes(png))
            log.info(f"{prefix.upper()} frame saved {target.name}")
            old = sorted(folder.glob(f"{prefix}_*.png"))[:-FAILURE_FRAMES_KEPT]
            for path in old:
                path.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            log.info(f"{prefix.upper()} frame not saved: {exc}")

    def _debug_shot(self, png, screen: ScreenDetection | None, name: str) -> None:
        if not self.config.debug:
            return
        detection = screen if screen is not None else self.screens.detect(png, full=True)
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


def _shop_mode(mode: str | None) -> str:
    kind = (mode or "follow").strip().lower()
    if kind in {"browse", "xem"}:
        return "browse"
    if kind == "sweep":
        return "sweep"
    return "follow"


def _spread_left(page: int) -> int:
    if page >= NEWS_PAGE_COUNT:
        return NEWS_PAGE_COUNT
    return page if page % 2 == 0 else page - 1


def _center(obj: DetectedObject) -> tuple[int, int]:
    return obj.x + obj.width // 2, obj.y + obj.height // 2


def _slot_skipped(slot: ShopSlot, skipped: list[ShopSlot]) -> bool:
    return any(box_iou(slot, seen) > 0.4 for seen in skipped)


def _slot_proof_png(png, slot: ShopSlot) -> bytes | None:
    crop = crate_proof_crop(png, slot)
    if crop.size == 0:
        return None
    return bgr_png_bytes(crop)
