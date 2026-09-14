from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.main import main
from app.vision.overlay import draw_overlay
from app.vision.regions import UI_REGIONS
from app.vision.screen import GameScreen, ScreenDetector
from app.vision.template_matcher import TemplateMatcher, as_bgr

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FARM = FIXTURES / "farm.png"
POPUP = FIXTURES / "popup.png"
UNKNOWN = FIXTURES / "unknown.png"
NEWSPAPER = FIXTURES / "newspaper.png"
PLAYER_SHOP = FIXTURES / "player_shop.png"


@pytest.fixture(scope="module")
def detector() -> ScreenDetector:
    return ScreenDetector(TemplateMatcher(threshold=0.80))


def test_farm_fixture_is_farm(detector: ScreenDetector):
    result = detector.detect(FARM)
    assert result.screen is GameScreen.FARM
    states = {obj.state for obj in result.objects}
    assert {"settings", "shop", "friends"} <= states
    assert result.confidence >= 0.80


def test_popup_fixture_is_popup_not_farm(detector: ScreenDetector):
    result = detector.detect(POPUP)
    assert result.screen is GameScreen.POPUP
    states = {obj.state for obj in result.objects}
    assert "close" in states
    assert result.confidence >= 0.80


def test_unknown_fixture_is_unknown(detector: ScreenDetector):
    result = detector.detect(UNKNOWN)
    assert result.screen is GameScreen.UNKNOWN
    states = {obj.state for obj in result.objects}
    assert "settings" not in states
    assert "shop" not in states


def test_newspaper_fixture_is_newspaper_not_popup(detector: ScreenDetector):
    result = detector.detect(NEWSPAPER)
    assert result.screen is GameScreen.NEWSPAPER
    assert result.confidence >= 0.80


def test_player_shop_fixture_is_player_shop(detector: ScreenDetector):
    result = detector.detect(PLAYER_SHOP)
    assert result.screen is GameScreen.PLAYER_SHOP
    states = {obj.state for obj in result.objects}
    assert "header" in states
    assert not any("egg" in (s or "") or "trung" in (s or "") for s in states)


def test_player_shop_without_header_is_still_shop(detector: ScreenDetector):
    image = as_bgr(PLAYER_SHOP).copy()
    image[140:250, 650:1280] = (0, 0, 0)
    result = detector.detect(image)
    assert result.screen is GameScreen.PLAYER_SHOP
    states = {obj.state for obj in result.objects}
    assert "close" in states
    assert "header" not in states


def test_detect_ignores_strict_matcher_threshold():
    detector = ScreenDetector(TemplateMatcher(threshold=0.99))
    shop = detector.detect(PLAYER_SHOP)
    assert shop.screen is GameScreen.PLAYER_SHOP
    popup = detector.detect(POPUP)
    assert popup.screen is GameScreen.POPUP
    farm = detector.detect(FARM)
    assert farm.screen is GameScreen.FARM


def test_single_hud_button_is_unknown(detector: ScreenDetector):
    image = as_bgr(FARM)
    corner = image[0:180, 0:180]
    result = detector.detect(corner)
    assert result.screen is GameScreen.UNKNOWN


def test_template_match_synthetic(tmp_path: Path):
    template = np.zeros((32, 32, 3), dtype=np.uint8)
    template[:, :] = (20, 20, 20)
    cv2.rectangle(template, (4, 4), (27, 27), (0, 200, 255), 3)
    cv2.circle(template, (16, 16), 6, (255, 80, 0), -1)
    tpl_dir = tmp_path / "tpl"
    tpl_dir.mkdir()
    cv2.imwrite(str(tpl_dir / "mark.png"), template)
    canvas = np.zeros((120, 160, 3), dtype=np.uint8)
    canvas[40:72, 70:102] = template
    matcher = TemplateMatcher(templates_dir=tpl_dir, threshold=0.90, scales=(1.0,))
    hit = matcher.match_one(canvas, "mark")
    assert hit is not None
    assert hit.x == 70
    assert hit.y == 40
    assert hit.confidence >= 0.90


def test_ui_regions_scale_with_resolution():
    x, y, w, h = UI_REGIONS["hud_shop"].to_pixels(1920, 1080)
    assert x < 80
    assert y > 800
    x2, y2, w2, h2 = UI_REGIONS["hud_shop"].to_pixels(1280, 720)
    assert abs(x2 / 1280 - x / 1920) < 0.02
    assert abs(y2 / 720 - y / 1080) < 0.02
    assert w2 < w
    assert h2 < h


def test_hud_taps_land_on_fixture_buttons():
    from app.vision.regions import hud_tap

    matcher = TemplateMatcher(scales=(1.0,))
    friends = matcher.match_one(as_bgr(FARM), "hud_friends")
    assert friends is not None
    fx, fy = hud_tap("hud_friends", 1920, 1080)
    assert friends.x <= fx <= friends.x + friends.width
    assert friends.y <= fy <= friends.y + friends.height
    shop = as_bgr(PLAYER_SHOP)
    close = matcher.match_one(shop, "shop_close")
    home = matcher.match_one(shop, "shop_home")
    assert close is not None and home is not None
    cx, cy = hud_tap("shop_close", 1920, 1080)
    hx, hy = hud_tap("shop_home", 1920, 1080)
    assert close.x <= cx <= close.x + close.width
    assert close.y <= cy <= close.y + close.height
    assert home.x <= hx <= home.x + home.width
    assert home.y <= hy <= home.y + home.height


def test_overlay_draws_screen_label(detector: ScreenDetector):
    result = detector.detect(FARM)
    overlay = draw_overlay(FARM, result)
    assert overlay.shape == as_bgr(FARM).shape
    assert overlay.sum() != as_bgr(FARM).sum()


def test_cli_detect_farm(capsys):
    assert main(["detect", str(FARM)]) == 0
    out = capsys.readouterr().out
    assert "screen\tFARM" in out
