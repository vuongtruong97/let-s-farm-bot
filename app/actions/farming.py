"""Harvest / plant with precondition, verify, retry. No diamond clicks."""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.config import SCREENSHOT_DIR, AppConfig
from app.controller.device import DeviceController
from app.state.field_state import FieldState, FieldStatus
from app.storage.botdata import load_crops
from app.storage.logger import get_logger
from app.vision.fields import FieldDetector
from app.vision.overlay import save_overlay
from app.vision.screen import GameScreen, ScreenDetection, ScreenDetector
from app.vision.template_matcher import TemplateMatcher, as_bgr

log = get_logger("ACTION")


@dataclass
class Action:
    type: str
    target: str
    x: int = 0
    y: int = 0
    x2: int = 0
    y2: int = 0
    crop: str | None = None


@dataclass
class ActionResult:
    success: bool
    action: Action
    error: str | None = None
    duration: float = 0.0


class FarmingActions:
    def __init__(
        self,
        device: DeviceController,
        config: AppConfig | None = None,
        fields: FieldDetector | None = None,
        screens: ScreenDetector | None = None,
        matcher: TemplateMatcher | None = None,
        wait_s: float = 0.9,
        retries: int = 1,
    ):
        self.device = device
        self.config = config or AppConfig()
        self.fields = fields or FieldDetector()
        self.screens = screens or ScreenDetector()
        self.matcher = matcher or TemplateMatcher(threshold=self.config.template_threshold)
        self.wait_s = wait_s
        self.retries = retries
        self.crops = load_crops()

    def detect_fields(self, source=None) -> list[FieldState]:
        png = source if source is not None else self.device.screenshot()
        return self.fields.detect_fields(png)

    def harvest_ready_fields(self, limit: int = 1, should_stop=None) -> ActionResult:
        png, screen, early = self._observe("HARVEST")
        if early is not None:
            return early
        ready = [f for f in self.fields.detect_fields(png) if f.state is FieldStatus.READY]
        if not ready:
            return ActionResult(False, Action("HARVEST", "none"), "no READY field")
        result: ActionResult | None = None
        for field in ready[: max(1, limit)]:
            if should_stop and should_stop():
                return ActionResult(False, Action("STOP", "harvest"), "stopped")
            result = self._harvest_one(field, png)
            png = None
            if not result.success:
                return result
        assert result is not None
        return result

    def plant_empty_fields(self, crop: str = "wheat", limit: int = 1, should_stop=None) -> ActionResult:
        png, screen, early = self._observe("PLANT")
        if early is not None:
            return early
        empty = [
            f for f in self.fields.detect_fields(png) if f.state is FieldStatus.EMPTY
        ]
        empty.sort(key=lambda f: (f.width * f.height, -f.confidence))
        if not empty:
            return ActionResult(False, Action("PLANT", "none", crop=crop), "no EMPTY field")
        result: ActionResult | None = None
        for field in empty[: max(1, limit)]:
            if should_stop and should_stop():
                return ActionResult(False, Action("STOP", "plant"), "stopped")
            result = self._plant_one(field, crop, png)
            png = None
            if not result.success:
                return result
        assert result is not None
        return result

    def _observe(self, action_type: str) -> tuple[object, ScreenDetection | None, ActionResult | None]:
        png = self.device.screenshot()
        screen = self.screens.detect(png)
        if screen.screen is GameScreen.POPUP:
            self._close_popup(screen)
            return png, screen, ActionResult(False, Action(action_type, "none"), "popup open")
        if screen.screen is not GameScreen.FARM:
            return png, screen, ActionResult(
                False, Action(action_type, "none"), f"screen={screen.screen.value}"
            )
        return png, screen, None

    def _harvest_one(self, field: FieldState, png=None) -> ActionResult:
        x1, y1, x2, y2 = field.harvest_swipe()
        action = Action("HARVEST", field.id, x1, y1, x2, y2)
        return self._run(action, field, lambda: self.device.swipe(x1, y1, x2, y2), png)

    def _plant_one(self, field: FieldState, crop: str, png=None) -> ActionResult:
        cx, cy = field.center
        action = Action("PLANT", field.id, cx, cy, crop=crop)
        seed_name = self.crops.get(crop, {}).get("seed_template")
        if not seed_name:
            return ActionResult(False, action, f"crop '{crop}' not in crops.json")
        if seed_name not in self.matcher.names:
            return ActionResult(
                False,
                action,
                f"missing template {seed_name}.png — capture seed tray to data/templates/",
            )

        def execute() -> None:
            self.device.tap(cx, cy)
            time.sleep(self.wait_s)
            shot = self.device.screenshot()
            if self._abort_if_popup(shot, action):
                raise _PopupAbort()
            hit = self.matcher.match_one(as_bgr(shot), seed_name)
            if hit is None:
                raise _SeedMissing(f"seed template {seed_name} not on screen")
            sx = hit.x + hit.width // 2
            sy = hit.y + hit.height // 2
            x1, y1, x2, y2 = field.plant_drag_from(sx, sy)
            action.x, action.y, action.x2, action.y2 = x1, y1, x2, y2
            self.device.swipe(x1, y1, x2, y2, 700)

        return self._run(action, field, execute, png)

    def _run(self, action: Action, field: FieldState, execute, png=None) -> ActionResult:
        started = time.monotonic()
        last_error = "failed"
        for attempt in range(self.retries + 1):
            if png is None:
                png = self.device.screenshot()
            screen = self.screens.detect(png)
            if screen.screen is GameScreen.POPUP:
                self._close_popup(screen)
                last_error = "popup open"
                png = None
                continue
            if screen.screen is not GameScreen.FARM:
                last_error = f"screen={screen.screen.value}"
                png = None
                continue
            if not self._precondition(action, png, field):
                last_error = "precondition failed"
                png = None
                continue
            self._debug_shot(png, screen, "before_action.png")
            try:
                execute()
            except _PopupAbort:
                return ActionResult(False, action, "diamond/popup — closed, not spent", time.monotonic() - started)
            except _SeedMissing as exc:
                last_error = str(exc)
                png = None
                continue
            time.sleep(self.wait_s)
            after = self.device.screenshot()
            after_screen = self.screens.detect(after)
            self._debug_shot(after, after_screen, "after_action.png")
            if after_screen.screen is GameScreen.POPUP:
                self._close_popup(after_screen)
                return ActionResult(False, action, "popup after action", time.monotonic() - started)
            if self._verified(action, after, field):
                log.info(f"{action.type} {action.target} SUCCESS")
                return ActionResult(True, action, None, time.monotonic() - started)
            last_error = "verify failed"
            log.info(f"{action.type} {action.target} retry={attempt} verify failed")
            png = None
        log.info(f"{action.type} {action.target} FAIL {last_error}")
        return ActionResult(False, action, last_error, time.monotonic() - started)

    def _precondition(self, action: Action, png, field: FieldState) -> bool:
        current = self._matching_field(png, field)
        if current is None:
            return False
        if action.type == "HARVEST":
            return current.state is FieldStatus.READY
        if action.type == "PLANT":
            return current.state is FieldStatus.EMPTY
        return False

    def _verified(self, action: Action, png, field: FieldState) -> bool:
        current = self._matching_field(png, field)
        if action.type == "HARVEST":
            return current is None or current.state is not FieldStatus.READY
        if action.type == "PLANT":
            return current is None or current.state is not FieldStatus.EMPTY
        return False

    def _matching_field(self, png, field: FieldState) -> FieldState | None:
        best: FieldState | None = None
        best_iou = 0.0
        for candidate in self.fields.detect_fields(png):
            iou = _iou(field, candidate)
            if iou > best_iou:
                best_iou = iou
                best = candidate
        if best_iou < 0.15:
            return None
        return best

    def _abort_if_popup(self, png, action: Action) -> bool:
        screen = self.screens.detect(png)
        if screen.screen is GameScreen.POPUP:
            self._close_popup(screen)
            log.info(f"{action.type} aborted popup")
            return True
        return False

    def _close_popup(self, screen: ScreenDetection) -> None:
        if self.config.allow_diamond_spending:
            log.info("diamond spending is off — closing popup anyway")
        closes = [obj for obj in screen.objects if obj.type == "popup" and obj.state == "close"]
        if not closes:
            self.device.back()
            return
        close = max(closes, key=lambda o: o.confidence)
        self.device.tap(close.x + close.width // 2, close.y + close.height // 2)

    def _debug_shot(self, png, screen: ScreenDetection, name: str) -> None:
        if not self.config.debug:
            return
        dest = SCREENSHOT_DIR / "debug" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(png, (bytes, bytearray)):
            dest.write_bytes(png)
        extra = self.fields.detect(png)
        combined = ScreenDetection(
            screen.screen,
            screen.confidence,
            [*screen.objects, *extra],
        )
        save_overlay(
            png,
            combined,
            SCREENSHOT_DIR / "debug" / name.replace(".png", "_overlay.png"),
        )


def _iou(a: FieldState, b: FieldState) -> float:
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.width, b.x + b.width)
    y2 = min(a.y + a.height, b.y + b.height)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = a.width * a.height + b.width * b.height - inter
    return inter / union if union else 0.0


class _PopupAbort(Exception):
    pass


class _SeedMissing(Exception):
    pass
