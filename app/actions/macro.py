"""Replay a recorded tap/swipe macro. No vision verify."""

from __future__ import annotations

import time

from app.actions.farming import Action, ActionResult
from app.controller.device import DeviceController
from app.storage.logger import get_logger
from app.storage.macros import load_macro, normalize_macro

log = get_logger("ACTION")


def play_macro(
    device: DeviceController,
    macro: dict | str,
    should_stop=None,
) -> ActionResult:
    data = load_macro(macro) if isinstance(macro, str) else normalize_macro(macro)
    name = data["name"]
    action = Action("MACRO", name)
    for step in data["steps"]:
        if should_stop and should_stop():
            return ActionResult(False, action, "stopped")
        kind = step["type"]
        if kind == "wait":
            time.sleep(max(0, int(step.get("ms") or 0)) / 1000)
            continue
        if kind == "tap":
            device.tap(int(step["x"]), int(step["y"]))
        elif kind == "swipe":
            device.swipe(
                int(step["x1"]),
                int(step["y1"]),
                int(step["x2"]),
                int(step["y2"]),
                int(step.get("duration_ms") or 320),
            )
        elif kind == "back":
            device.back()
        elif kind == "home":
            device.home()
    log.info(f"MACRO {name} SUCCESS n={len(data['steps'])}")
    return ActionResult(True, action)
