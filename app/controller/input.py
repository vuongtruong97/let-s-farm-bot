from __future__ import annotations

from app.controller.adb import AdbClient


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
