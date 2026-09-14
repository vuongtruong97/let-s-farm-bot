from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.actions.farming import FarmingActions
from app.config import AppConfig
from app.controller.camera import CameraManager
from app.main import main
from app.state.field_state import FieldState, FieldStatus
from app.vision.fields import FieldDetector
from app.vision.template_matcher import TemplateMatcher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FARM = FIXTURES / "farm.png"


def _png_bytes(image: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return buf.tobytes()


class FakeDevice:
    def __init__(self, frames: list[bytes]):
        self.frames = list(frames)
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple] = []
        self.backs = 0

    def screenshot(self) -> bytes:
        if not self.frames:
            raise AssertionError("no screenshot frames left")
        if len(self.frames) == 1:
            return self.frames[0]
        return self.frames.pop(0)

    def tap(self, x: int, y: int) -> None:
        self.taps.append((int(x), int(y)))

    def swipe(self, x1, y1, x2, y2, duration=None) -> None:
        self.swipes.append((x1, y1, x2, y2, duration))

    def back(self) -> None:
        self.backs += 1

    def resolution(self) -> tuple[int, int]:
        return 1920, 1080


def _farm_bgr() -> np.ndarray:
    image = cv2.imread(str(FARM))
    assert image is not None
    return image


def _paint(image: np.ndarray, fields, hsv) -> np.ndarray:
    out = image.copy()
    color = cv2.cvtColor(np.uint8([[[hsv[0], hsv[1], hsv[2]]]]), cv2.COLOR_HSV2BGR)[0, 0]
    for field in fields:
        out[field.y : field.y + field.height, field.x : field.x + field.width] = color
    return out


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("app.actions.farming.time.sleep", lambda _s: None)


def test_harvest_ready_field_verifies(no_sleep):
    before = _farm_bgr()
    ready = [f for f in FieldDetector().detect_fields(before) if f.state is FieldStatus.READY]
    assert ready
    after = _paint(before, ready[:1], (12, 160, 90))
    device = FakeDevice([_png_bytes(before), _png_bytes(after)])
    actions = FarmingActions(
        device,
        AppConfig(debug=False),
        wait_s=0,
        retries=0,
    )
    result = actions.harvest_ready_fields(limit=1)
    assert result.success
    assert result.action.type == "HARVEST"
    assert device.swipes
    x1, y1, x2, y2, _ms = device.swipes[0]
    target = ready[0]
    assert target.x <= x1 <= target.x + target.width
    assert target.x <= x2 <= target.x + target.width


def test_harvest_verify_fails_when_still_ready(no_sleep):
    frame = _png_bytes(_farm_bgr())
    device = FakeDevice([frame, frame])
    actions = FarmingActions(device, AppConfig(debug=False), wait_s=0, retries=0)
    result = actions.harvest_ready_fields(limit=1)
    assert result.success is False
    assert result.error == "verify failed"


def test_plant_without_seed_template_fails(tmp_path, no_sleep):
    frame = _png_bytes(_farm_bgr())
    device = FakeDevice([frame])
    matcher = TemplateMatcher(templates_dir=tmp_path, threshold=0.80, scales=(1.0,))
    actions = FarmingActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, retries=0
    )
    result = actions.plant_empty_fields(crop="wheat", limit=1)
    assert result.success is False
    assert "missing template" in (result.error or "")


def test_plant_taps_seed_template_and_verifies(tmp_path, no_sleep):
    before = _farm_bgr()
    empty = [f for f in FieldDetector().detect_fields(before) if f.state is FieldStatus.EMPTY]
    empty.sort(key=lambda f: (f.width * f.height, -f.confidence))
    assert empty
    seed = np.zeros((40, 40, 3), dtype=np.uint8)
    cv2.rectangle(seed, (4, 4), (35, 35), (0, 220, 255), 3)
    cv2.circle(seed, (20, 20), 8, (0, 180, 40), -1)
    tpl = tmp_path / "tpl"
    tpl.mkdir()
    cv2.imwrite(str(tpl / "seed_wheat.png"), seed)
    menu = before.copy()
    menu[300:340, 900:940] = seed
    after = _paint(before, empty[:1], (55, 180, 120))
    device = FakeDevice([_png_bytes(before), _png_bytes(menu), _png_bytes(after)])
    matcher = TemplateMatcher(templates_dir=tpl, threshold=0.80, scales=(1.0,))
    actions = FarmingActions(
        device,
        AppConfig(debug=False),
        matcher=matcher,
        wait_s=0,
        retries=0,
    )
    result = actions.plant_empty_fields(crop="wheat", limit=1)
    assert result.success
    assert len(device.taps) == 1
    assert device.swipes
    sx, sy, dx, dy, ms = device.swipes[0]
    assert 900 <= sx <= 940
    assert 300 <= sy <= 340
    expected = empty[0].plant_drag_from(sx, sy)
    assert (sx, sy, dx, dy) == expected
    assert dy != sy
    assert ms == 700


def test_plant_drag_follows_isometric_diagonal():
    field = FieldState("field:0", 800, 500, 240, 160, FieldStatus.EMPTY, 0.9)
    x1, y1, x2, y2 = field.plant_drag_from(700, 560)
    pad_x = max(8, field.width // 6)
    pad_y = max(8, field.height // 6)
    assert (x1, y1) == (700, 560)
    assert y2 == field.y + field.height - pad_y
    assert x2 in (field.x + pad_x, field.x + field.width - pad_x)
    assert x2 != x1
    assert y2 != y1


def test_harvest_aborts_popup_without_diamond_click(no_sleep):
    popup = cv2.imread(str(FIXTURES / "popup.png"))
    device = FakeDevice([_png_bytes(popup)])
    actions = FarmingActions(device, AppConfig(debug=False), wait_s=0, retries=0)
    result = actions.harvest_ready_fields(limit=1)
    assert result.success is False
    assert result.error == "popup open"
    assert device.taps  # close X, not a field


def test_camera_pan_uses_relative_swipe():
    device = FakeDevice([])
    camera = CameraManager(device, pan_fraction=0.25, swipe_ms=200)
    camera.pan_right()
    assert len(device.swipes) == 1
    x1, y1, x2, y2, ms = device.swipes[0]
    assert x1 > x2
    assert y1 == y2 == 540
    assert ms == 200
    camera.reset_camera()
    assert len(device.swipes) == 2
    rx1, ry1, rx2, ry2, _ = device.swipes[1]
    assert (rx1, ry1, rx2, ry2) == (x2, y2, x1, y1)


def test_camera_pan_diagonal():
    device = FakeDevice([])
    camera = CameraManager(device, pan_fraction=0.25, swipe_ms=200)
    camera.pan_down_left()
    x1, y1, x2, y2, ms = device.swipes[0]
    assert x1 != x2
    assert y1 != y2
    assert ms == 200


def test_camera_pan_to_column_follows_house_diagonal():
    from app.vision.regions import COLUMN_PAN_END, COLUMN_PAN_START, hud_tap

    device = FakeDevice([])
    camera = CameraManager(device)
    camera.pan_to_column()
    x1, y1, x2, y2, ms = device.swipes[0]
    assert (x1, y1) == COLUMN_PAN_START.to_pixels(1920, 1080)
    assert (x2, y2) == COLUMN_PAN_END.to_pixels(1920, 1080)
    shop_x, _shop_y = hud_tap("shop_home", 1920, 1080)
    assert x1 > shop_x + 80
    assert x1 < x2
    assert y1 > y2
    assert x2 < 1920 - 120
    assert y2 > 80
    assert ms >= 500


def test_cli_fields_farm(capsys):
    assert main(["fields", str(FARM)]) == 0
    out = capsys.readouterr().out
    assert "READY" in out
    assert "EMPTY" in out
