"""Restart the game and wait for the farm. No other game logic."""

from __future__ import annotations

import time

from app.actions.farming import Action, ActionResult
from app.storage.logger import get_logger
from app.vision.screen import GameScreen, popup_close_point

log = get_logger("ACTION")

# Force-stop → launch → farm took 18-23s on BlueStacks; generous for a slow boot.
GAME_START_TIMEOUT_S = 120.0
START_POLL_S = 1.0


def restart_game(
    device,
    screens,
    package: str,
    should_stop=None,
    timeout_s: float = GAME_START_TIMEOUT_S,
    sleep=None,
) -> ActionResult:
    """Kill the game, launch it again, and wait until the farm is on screen."""
    sleep = sleep or time.sleep
    action = Action("RESTART_GAME", package)
    log.info(f"RESTART_GAME {package}")
    device.force_stop(package)
    sleep(1.0)
    device.launch_app(package)
    started = time.monotonic()
    while True:
        if should_stop and should_stop():
            return ActionResult(False, Action("STOP", "restart_game"), "stopped")
        screen = screens.detect(device.screenshot())
        elapsed = time.monotonic() - started
        if screen.screen is GameScreen.FARM:
            log.info(f"RESTART_GAME farm after {elapsed:.0f}s")
            return ActionResult(True, action, duration=elapsed)
        if screen.screen is GameScreen.POPUP:
            point = popup_close_point(screen)
            if point is not None:
                device.tap(*point)
        if elapsed >= timeout_s:
            log.info(f"RESTART_GAME FAIL farm not shown after {timeout_s:.0f}s")
            return ActionResult(
                False, action, f"farm not shown after {timeout_s:.0f}s", elapsed
            )
        sleep(START_POLL_S)
