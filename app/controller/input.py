from __future__ import annotations

import re

from app.controller.adb import AdbClient

# "mCurrentFocus=Window{bd25221 u0 letsfarm.com.playday/letsfarm...AndroidLauncher}"
_FOCUS = re.compile(r"mCurrentFocus=Window\{\S+ \S+ ([\w.]+)/")
_FOCUSED_APP = re.compile(r"mFocusedApp=.*?ActivityRecord\{\S+ \S+ ([\w.]+)/")


def parse_foreground_package(dumpsys: str) -> str:
    """Package of the focused window, '' when none can be read."""
    for pattern in (_FOCUS, _FOCUSED_APP):
        match = pattern.search(dumpsys)
        if match:
            return match.group(1)
    return ""


class InputController:
    """ADB input primitives. Coordinates come from vision, not from this module."""

    def __init__(self, adb: AdbClient, serial: str):
        self.adb = adb
        self.serial = serial

    def _shell(self, args: list[str], timeout: float = 8.0) -> bytes:
        return self.adb.run(["-s", self.serial, "shell", *args], timeout=timeout)

    def tap(self, x: int, y: int) -> None:
        self._shell(["input", "tap", str(int(x)), str(int(y))])

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 320
    ) -> None:
        self._shell(
            [
                "input",
                "swipe",
                str(int(x1)),
                str(int(y1)),
                str(int(x2)),
                str(int(y2)),
                str(max(80, int(duration_ms))),
            ]
        )

    def back(self) -> None:
        self._shell(["input", "keyevent", "4"])

    def home(self) -> None:
        self._shell(["input", "keyevent", "3"])

    def force_stop(self, package: str) -> None:
        if not package:
            raise ValueError("Thiếu package name — không đoán app.")
        self._shell(["am", "force-stop", package])

    def foreground_package(self) -> str:
        raw = self._shell(["dumpsys", "window"], timeout=8.0)
        return parse_foreground_package(raw.decode("utf-8", errors="ignore"))

    def launch_app(self, package: str) -> None:
        if not package:
            raise ValueError("Thiếu package name — không đoán app.")
        self._shell(
            [
                "monkey",
                "-p",
                package,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
            ],
            timeout=12,
        )
