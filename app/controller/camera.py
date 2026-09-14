"""Zone-based camera pan. No world map in M2."""

from __future__ import annotations

from enum import Enum

from app.controller.device import DeviceController
from app.state.field_state import FieldState
from app.storage.logger import get_logger
from app.vision.fields import FieldDetector
from app.vision.regions import COLUMN_PAN_END, COLUMN_PAN_START

log = get_logger("CAMERA")


class CameraZone(str, Enum):
    FIELDS = "ZONE_FIELDS"
    ANIMALS = "ZONE_ANIMALS"
    BUILDINGS = "ZONE_BUILDINGS"
    ORDER = "ZONE_ORDER"


class CameraManager:
    def __init__(
        self,
        device: DeviceController,
        detector: FieldDetector | None = None,
        pan_fraction: float = 0.32,
        swipe_ms: int = 280,
    ):
        self.device = device
        self.detector = detector or FieldDetector()
        self.pan_fraction = pan_fraction
        self.swipe_ms = swipe_ms
        self._history: list[tuple[str, int, int, int, int]] = []
        self._size: tuple[int, int] | None = None

    def scan_current_view(self) -> list[FieldState]:
        png = self.device.screenshot()
        fields = self.detector.detect_fields(png)
        log.info(f"scan_current_view n={len(fields)}")
        return fields

    def pan_left(self) -> None:
        self._pan_xy(-1, 0, "left")

    def pan_right(self) -> None:
        self._pan_xy(1, 0, "right")

    def pan_up(self) -> None:
        self._pan_xy(0, -1, "up")

    def pan_down(self) -> None:
        self._pan_xy(0, 1, "down")

    def pan_down_left(self) -> None:
        self._pan_xy(-1, 1, "down_left")

    def pan_down_right(self) -> None:
        self._pan_xy(1, 1, "down_right")

    def pan_up_left(self) -> None:
        self._pan_xy(-1, -1, "up_left")

    def pan_up_right(self) -> None:
        self._pan_xy(1, -1, "up_right")

    def pan_to_column(self) -> None:
        """Drag the house view diagonally toward the upper-right corner.

        Finger path: just right of Shop → near the top-right landmark.
        That pull moves the camera toward the lower-left road / mailbox.
        """
        width, height = self._resolution()
        x1, y1 = COLUMN_PAN_START.to_pixels(width, height)
        x2, y2 = COLUMN_PAN_END.to_pixels(width, height)
        duration = max(self.swipe_ms, 500)
        log.info("pan column")
        self.device.swipe(x1, y1, x2, y2, duration)
        self._history.append(("column", x1, y1, x2, y2))

    def reset_camera(self) -> None:
        while self._history:
            _name, x1, y1, x2, y2 = self._history.pop()
            self.device.swipe(x2, y2, x1, y1, self.swipe_ms)
        log.info("reset_camera")

    def clear_history(self) -> None:
        self._history.clear()

    def _resolution(self) -> tuple[int, int]:
        if self._size is None:
            self._size = self.device.resolution()
        return self._size

    def _pan_xy(self, hx: int, vy: int, name: str, fraction: float | None = None) -> None:
        width, height = self._resolution()
        cx, cy = width // 2, height // 2
        delta = int(min(width, height) * (self.pan_fraction if fraction is None else fraction))
        x1, x2 = cx + hx * delta, cx - hx * delta
        y1, y2 = cy + vy * delta, cy - vy * delta
        log.info(f"pan {name}")
        self.device.swipe(x1, y1, x2, y2, self.swipe_ms)
        self._history.append((name, x1, y1, x2, y2))
