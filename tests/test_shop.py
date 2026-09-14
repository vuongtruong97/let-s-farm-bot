from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.actions.shop import NewspaperActions
from app.config import AppConfig
from app.main import main
from app.storage.botdata import save_column
from app.vision.detector import DetectedObject
from app.vision.newspaper import NewspaperDetector
from app.vision.regions import news_spread_slots
from app.vision.screen import GameScreen, ScreenDetector
from app.vision.template_matcher import TemplateMatcher
from tests.test_farming import FakeDevice, _png_bytes

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FARM = FIXTURES / "farm.png"
FARM_NO_COLUMN = FIXTURES / "farm_no_column.png"
NEWSPAPER = FIXTURES / "newspaper.png"
PLAYER_SHOP = FIXTURES / "player_shop.png"
ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "data" / "templates"


def _bgr(path: Path) -> np.ndarray:
    image = cv2.imread(str(path))
    assert image is not None
    return image


def _item_icon() -> np.ndarray:
    icon = np.zeros((44, 44, 3), dtype=np.uint8)
    cv2.rectangle(icon, (3, 3), (40, 40), (0, 220, 255), 3)
    cv2.circle(icon, (22, 22), 10, (0, 180, 40), -1)
    return icon


def _tpl_dir(tmp_path: Path, extras: dict[str, np.ndarray] | None = None) -> Path:
    dest = tmp_path / "tpl"
    dest.mkdir()
    for path in TEMPLATES.glob("*.png"):
        data = path.read_bytes()
        (dest / path.name).write_bytes(data)
    for name, image in (extras or {}).items():
        cv2.imwrite(str(dest / f"{name}.png"), image)
    return dest


@pytest.fixture(autouse=True)
def isolated_column(tmp_path, monkeypatch):
    path = tmp_path / "column.json"
    monkeypatch.setattr("app.storage.botdata.column_path", lambda: path)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("app.actions.shop.time.sleep", lambda _s: None)


def test_farm_fixture_finds_newspaper_stand():
    stand = NewspaperDetector().find_stand(FARM)
    assert stand is not None
    assert stand.state == "stand"
    assert stand.confidence >= 0.90


def test_farm_no_column_has_no_stand():
    assert NewspaperDetector().find_stand(FARM_NO_COLUMN) is None


def test_find_stand_matches_close_zoom_crop():
    canvas = _bgr(FARM_NO_COLUMN)
    crop = cv2.imread(str(TEMPLATES / "newspaper_stand_zoom.png"))
    assert crop is not None
    y, x = 680, 10
    canvas[y : y + crop.shape[0], x : x + crop.shape[1]] = crop
    stand = NewspaperDetector().find_stand(canvas)
    assert stand is not None
    assert stand.state == "stand"
    assert abs(stand.x - x) < 8
    assert abs(stand.y - y) < 8


def test_newspaper_fixture_finds_ads():
    ads = NewspaperDetector().find_ads(NEWSPAPER)
    assert len(ads) == 11
    assert all(a.state == "ad" for a in ads)
    assert all((a.x, a.y) != (230, 173) for a in ads)


def test_news_grid_skips_cover_and_page_ten_is_left_only():
    from app.vision.regions import news_spread_slots

    open_spread = news_spread_slots(1920, 1080, 2)
    assert len(open_spread) == 12
    assert {(p, s) for p, s, *_ in open_spread} >= {(2, 0), (2, 5), (3, 0), (3, 5)}
    last = news_spread_slots(1920, 1080, 10)
    assert len(last) == 6
    assert all(page == 10 for page, *_ in last)
    assert max(x for _p, _s, x, _y, _w, _h in last) < 900


def test_crop_ad_item_icon_from_fixture():
    news = NewspaperDetector()
    ads = news.find_ads(NEWSPAPER)
    icon = news.ad_item_icon(NEWSPAPER, ads[0])
    assert icon.shape[0] >= 24
    assert icon.shape[1] >= 24
    assert icon.shape[2] == 3


def test_find_column_skips_pan_when_stand_visible(no_sleep):
    device = FakeDevice([_png_bytes(_bgr(FARM))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.find_column()
    assert result.success
    assert result.action.type == "FIND_COLUMN"
    assert device.swipes == []
    assert result.action.x > 0
    assert result.action.y > 0


def test_find_column_pans_once_when_stand_hidden(no_sleep):
    from app.vision.regions import COLUMN_PAN_END, COLUMN_PAN_START

    device = FakeDevice(
        [_png_bytes(_bgr(FARM_NO_COLUMN)), _png_bytes(_bgr(FARM))]
    )
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.find_column()
    assert result.success
    assert result.action.type == "FIND_COLUMN"
    assert len(device.swipes) == 1
    x1, y1, x2, y2, ms = device.swipes[0]
    assert (x1, y1) == COLUMN_PAN_START.to_pixels(1920, 1080)
    assert (x2, y2) == COLUMN_PAN_END.to_pixels(1920, 1080)
    assert x1 < x2
    assert y1 > y2
    assert ms >= 500
    assert result.action.x > 0
    assert result.action.y > 0


def test_find_column_ignores_stale_cache_until_stand_visible(no_sleep):
    save_column(111, 222)
    device = FakeDevice(
        [_png_bytes(_bgr(FARM_NO_COLUMN)), _png_bytes(_bgr(FARM))]
    )
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.find_column()
    assert result.success
    assert (result.action.x, result.action.y) != (111, 222)
    assert len(device.swipes) == 1


def test_find_column_fails_when_stand_missing(no_sleep):
    save_column(111, 222)
    device = FakeDevice([_png_bytes(_bgr(FARM_NO_COLUMN))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.find_column()
    assert result.success is False
    assert "not found" in (result.error or "")
    assert len(device.swipes) == 1


def test_open_newspaper_taps_cached_point(no_sleep):
    save_column(140, 820)
    device = FakeDevice([_png_bytes(_bgr(NEWSPAPER))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.open_newspaper()
    assert result.success
    assert device.taps == [(140, 820)]


def test_open_newspaper_taps_stand(no_sleep):
    device = FakeDevice([_png_bytes(_bgr(FARM)), _png_bytes(_bgr(NEWSPAPER))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.open_newspaper()
    assert result.success
    assert device.taps
    stand = NewspaperDetector().find_stand(FARM)
    assert stand is not None
    tx, ty = device.taps[0]
    assert stand.x <= tx <= stand.x + stand.width
    assert stand.y <= ty <= stand.y + stand.height


def test_shop_egg_slot_is_buyable():
    slots = NewspaperDetector().find_slots(
        FIXTURES / "shop_egg.png", {"egg": {"template": "item_egg"}}
    )
    assert slots
    assert slots[0].item == "egg"
    assert slots[0].buyable
    assert slots[0].confidence >= 0.72


def test_shop_slot_matches_color_template(tmp_path):
    color = np.zeros((48, 48, 3), dtype=np.uint8)
    cv2.rectangle(color, (4, 4), (43, 43), (0, 60, 255), -1)
    cv2.circle(color, (24, 24), 12, (40, 220, 40), -1)
    shop = _bgr(PLAYER_SHOP)
    shop[420:468, 640:688] = color
    dest = tmp_path / "tpl"
    dest.mkdir()
    cv2.imwrite(str(dest / "item_demo.png"), color)
    matcher = TemplateMatcher(templates_dir=dest, threshold=0.72, scales=(1.0,))
    slots = NewspaperDetector(matcher).find_slots(
        shop, {"demo": {"template": "item_demo"}}
    )
    assert slots
    assert slots[0].item == "demo"
    assert abs(slots[0].x - 640) < 6
    assert abs(slots[0].y - 420) < 6


def test_find_slots_matches_every_visible_copy(tmp_path):
    color = np.zeros((48, 48, 3), dtype=np.uint8)
    cv2.rectangle(color, (4, 4), (43, 43), (0, 60, 255), -1)
    cv2.circle(color, (24, 24), 12, (40, 220, 40), -1)
    shop = _bgr(PLAYER_SHOP)
    shop[420:468, 640:688] = color
    shop[420:468, 900:948] = color
    dest = tmp_path / "tpl"
    dest.mkdir()
    cv2.imwrite(str(dest / "item_demo.png"), color)
    matcher = TemplateMatcher(templates_dir=dest, threshold=0.72, scales=(1.0,))
    slots = NewspaperDetector(matcher).find_slots(
        shop, {"demo": {"template": "item_demo"}}
    )
    assert len(slots) >= 2
    xs = sorted(slot.x for slot in slots)
    assert xs[0] < 700
    assert xs[-1] > 850


def test_shop_slots_ignore_matches_outside_crate_table(tmp_path):
    color = np.zeros((48, 48, 3), dtype=np.uint8)
    cv2.rectangle(color, (4, 4), (43, 43), (0, 60, 255), -1)
    shop = _bgr(PLAYER_SHOP)
    shop[20:68, 40:88] = color
    dest = tmp_path / "tpl"
    dest.mkdir()
    cv2.imwrite(str(dest / "item_demo.png"), color)
    matcher = TemplateMatcher(templates_dir=dest, threshold=0.72, scales=(1.0,))
    slots = NewspaperDetector(matcher).find_slots(
        shop, {"demo": {"template": "item_demo"}}
    )
    assert slots == []


def test_newspaper_matches_print_template_not_shop_color(tmp_path):
    color = np.zeros((48, 48, 3), dtype=np.uint8)
    cv2.rectangle(color, (4, 4), (43, 43), (0, 60, 255), -1)
    cv2.circle(color, (24, 24), 12, (40, 220, 40), -1)
    bw = cv2.cvtColor(cv2.cvtColor(color, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    dest = tmp_path / "tpl"
    dest.mkdir()
    cv2.imwrite(str(dest / "item_demo.png"), color)
    cv2.imwrite(str(dest / "item_demo_news.png"), bw)
    matcher = TemplateMatcher(templates_dir=dest, threshold=0.72, scales=(1.0,))
    news = NewspaperDetector(matcher)
    roi = np.full((90, 90, 3), 180, dtype=np.uint8)
    roi[20:68, 20:68] = bw
    spec = {"demo": {"template": "item_demo", "news_template": "item_demo_news"}}
    assert news._wishlist_item_in(roi, spec) == "demo"
    shop = _bgr(PLAYER_SHOP)
    shop[300:348, 500:548] = bw
    assert news.find_slots(shop, spec) == []


def test_newspaper_ignores_shop_icon_without_news_template(tmp_path):
    color = np.zeros((48, 48, 3), dtype=np.uint8)
    cv2.rectangle(color, (4, 4), (43, 43), (0, 60, 255), -1)
    dest = tmp_path / "tpl"
    dest.mkdir()
    cv2.imwrite(str(dest / "item_demo.png"), color)
    matcher = TemplateMatcher(templates_dir=dest, threshold=0.72, scales=(1.0, 0.5))
    news = NewspaperDetector(matcher)
    roi = np.full((90, 90, 3), 180, dtype=np.uint8)
    roi[20:68, 20:68] = color
    spec = {"demo": {"template": "item_demo"}}
    assert news._wishlist_item_in(roi, spec) is None


def test_actions_copy_buy_and_news_thresholds(no_sleep):
    device = FakeDevice([])
    actions = NewspaperActions(
        device,
        AppConfig(debug=False, buy_threshold=0.61, news_threshold=0.83),
        wait_s=0,
    )
    assert actions.news.buy_threshold == 0.61
    assert actions.news.news_threshold == 0.83


def test_match_wishlist_uses_scene_thresholds():
    news = NewspaperDetector(buy_threshold=0.61, news_threshold=0.83)
    news.matcher._templates["item_egg"] = np.zeros((16, 16, 3), dtype=np.uint8)
    news.matcher._templates["item_egg_news"] = np.zeros((16, 16, 3), dtype=np.uint8)
    captured: dict[str, float | None] = {}

    def fake_match_one(_image, name, threshold=None, **_kw):
        captured[name] = threshold
        return None

    news.matcher.match_one = fake_match_one  # type: ignore[method-assign]
    canvas = np.zeros((40, 40, 3), dtype=np.uint8)
    spec = {"template": "item_egg", "news_template": "item_egg_news"}
    news._match_wishlist_item(canvas, "egg", spec, scene="shop")
    news._match_wishlist_item(canvas, "egg", spec, scene="news")
    assert captured["item_egg"] == 0.61
    assert captured["item_egg_news"] == 0.83


def test_newspaper_fixture_matches_egg_and_screw_news():
    found = NewspaperDetector().find_wishlist_ads(
        NEWSPAPER,
        {
            "egg": {"template": "item_egg", "news_template": "item_egg_news"},
            "screw": {"template": "item_screw", "news_template": "item_screw_news"},
        },
    )
    by_pos = {(ad.x, ad.y): item for ad, item in found}
    assert by_pos[(1365, 173)] == "egg"
    assert by_pos[(230, 453)] == "screw"
    assert "egg" in by_pos.values()
    assert "screw" in by_pos.values()


def test_visit_shop_from_ad(no_sleep):
    ads = NewspaperDetector().find_ads(NEWSPAPER)
    assert ads
    device = FakeDevice([_png_bytes(_bgr(PLAYER_SHOP))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.visit_shop(ads[0])
    assert result.success
    assert result.action.type == "VISIT_SHOP"
    assert device.taps
    assert actions._ad_cell(ads[0]) in actions._visited_ads


def test_find_column_keeps_visited_ads(no_sleep):
    device = FakeDevice([_png_bytes(_bgr(FARM))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._visited_ads.add((2, 1))
    result = actions.find_column()
    assert result.success
    assert (2, 1) in actions._visited_ads


def test_ad_cell_is_page_and_slot_not_pixels():
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0)
    _page, _slot, x, y, w, h = next(
        cell for cell in news_spread_slots(1920, 1080, 2) if cell[0] == 2 and cell[1] == 1
    )
    ad = DetectedObject("newspaper", x, y, w, h, 1.0, "ad")
    actions._news_left_page = 2
    assert actions._ad_cell(ad) == (2, 1)
    actions._news_left_page = 4
    assert actions._ad_cell(ad) == (4, 1)


def test_unused_wishlist_ad_skips_visited_cell(monkeypatch, no_sleep):
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0)
    actions._news_left_page = 2
    cells = {
        (page, slot): (x, y, w, h)
        for page, slot, x, y, w, h in news_spread_slots(1920, 1080, 2)
    }
    first = DetectedObject("newspaper", *cells[(2, 1)], 1.0, "ad")
    second = DetectedObject("newspaper", *cells[(3, 2)], 1.0, "ad")
    actions._visited_ads.add((2, 1))
    monkeypatch.setattr(
        actions.news,
        "find_wishlist_ads",
        lambda png, wishlist, left_page=2: [(first, "egg"), (second, "wheat")],
    )
    picked = actions._unused_wishlist_ad(b"png")
    assert picked is second


def test_buy_coin_slot_and_verify(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    icon = _item_icon()
    coin = cv2.imread(str(TEMPLATES / "price_coin.png"))
    assert coin is not None
    before = _bgr(PLAYER_SHOP)
    before[360:404, 640:684] = icon
    before[360 : 360 + coin.shape[0], 690 : 690 + coin.shape[1]] = coin
    after = _bgr(PLAYER_SHOP)
    matcher = TemplateMatcher(templates_dir=_tpl_dir(tmp_path, {"item_wheat": icon}), scales=(1.0,))
    device = FakeDevice([_png_bytes(before), _png_bytes(before), _png_bytes(after)])
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.buy_wishlist()
    assert result.success
    assert result.action.target == "wheat"
    assert device.taps


def test_buy_wishlist_item_without_price_tag(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    icon = _item_icon()
    before = _bgr(PLAYER_SHOP)
    before[360:404, 640:684] = icon
    after = _bgr(PLAYER_SHOP)
    matcher = TemplateMatcher(templates_dir=_tpl_dir(tmp_path, {"item_wheat": icon}), scales=(1.0,))
    device = FakeDevice([_png_bytes(before), _png_bytes(before), _png_bytes(after)])
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.buy_wishlist()
    assert result.success
    assert result.action.target == "wheat"
    assert device.taps


def test_buy_missing_item_template_fails(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"unicorn": {"template": "item_unicorn", "enabled": True}},
    )
    matcher = TemplateMatcher(templates_dir=_tpl_dir(tmp_path), scales=(1.0,))
    device = FakeDevice([_png_bytes(_bgr(PLAYER_SHOP))])
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.buy_wishlist()
    assert result.success is False
    assert "missing template" in (result.error or "")


def test_stall_swipe_px_left_and_right():
    from app.vision.regions import stall_swipe_px

    lx1, ly1, lx2, ly2 = stall_swipe_px(1920, 1080, "left")
    rx1, ry1, rx2, ry2 = stall_swipe_px(1920, 1080, "right")
    assert ly1 == ly2 == ry1 == ry2
    assert lx1 < lx2
    assert rx1 > rx2
    assert (lx1, ly1, lx2, ly2) == (rx2, ry2, rx1, ry1)


def test_crate_views_differ_on_table_paint():
    from app.vision.newspaper import crate_views_differ

    base = _bgr(PLAYER_SHOP)
    other = base.copy()
    other[380:700, 400:1100] = (0, 0, 255)
    assert crate_views_differ(base, other) is True
    assert crate_views_differ(base, base) is False


def test_buy_wishlist_rewinds_then_pans_right(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {
            "mid_item": {"template": "item_mid_item", "enabled": True},
            "left_item": {"template": "item_left_item", "enabled": True},
        },
    )
    mid_icon = np.zeros((44, 44, 3), dtype=np.uint8)
    mid_icon[:] = (0, 0, 255)
    cv2.rectangle(mid_icon, (4, 4), (39, 39), (255, 255, 0), 3)
    left_icon = np.zeros((44, 44, 3), dtype=np.uint8)
    left_icon[:] = (255, 0, 0)
    cv2.circle(left_icon, (22, 22), 16, (0, 255, 255), -1)
    mid = _bgr(PLAYER_SHOP)
    mid[380:700, 360:700] = (40, 40, 40)
    mid[420:464, 640:684] = mid_icon
    left = _bgr(PLAYER_SHOP)
    left[380:700, 800:1200] = (10, 180, 10)
    left[420:464, 900:944] = left_icon
    empty = _bgr(PLAYER_SHOP)
    matcher = TemplateMatcher(
        templates_dir=_tpl_dir(
            tmp_path, {"item_mid_item": mid_icon, "item_left_item": left_icon}
        ),
        scales=(1.0,),
    )
    device = FakeDevice(
        [
            _png_bytes(mid),
            _png_bytes(left),
            _png_bytes(left),
            _png_bytes(empty),
            _png_bytes(empty),
            _png_bytes(mid),
            _png_bytes(empty),
        ]
    )
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.buy_wishlist()
    assert result.success
    from app.vision.regions import stall_swipe_px

    width, height = device.resolution()
    left_swipe = stall_swipe_px(width, height, "left")
    right_swipe = stall_swipe_px(width, height, "right")
    swipes = [(s[0], s[1], s[2], s[3]) for s in device.swipes]
    assert left_swipe in swipes
    assert right_swipe in swipes
    assert len(device.taps) >= 2


def test_go_home_taps_fixed_hud_points(no_sleep):
    from app.vision.regions import hud_tap

    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.go_home()
    assert result.success
    assert result.action.type == "GO_HOME"
    width, height = device.resolution()
    assert device.taps == [
        hud_tap("hud_friends", width, height),
        hud_tap("friends_first", width, height),
        hud_tap("shop_close", width, height),
        hud_tap("shop_home", width, height),
    ]


def test_close_shop_taps_fixed_point_without_screenshot(no_sleep):
    from app.vision.regions import hud_tap

    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.close_shop()
    assert result.success
    assert result.action.type == "CLOSE_SHOP"
    width, height = device.resolution()
    assert device.taps == [hud_tap("shop_close", width, height)]


def test_close_shop_then_find_column(no_sleep):
    device = FakeDevice(
        [
            _png_bytes(_bgr(FARM_NO_COLUMN)),
            _png_bytes(_bgr(FARM)),
        ]
    )
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    closed = actions.close_shop()
    assert closed.success
    assert device.taps or device.backs
    found = actions.find_column()
    assert found.success
    assert device.swipes
    x1, y1, x2, y2, _ms = device.swipes[0]
    assert x1 != x2
    assert y1 != y2


def test_shop_loop_finds_column_without_going_home_between_visits(no_sleep, monkeypatch):
    from app.actions.farming import Action, ActionResult

    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    homes: list[int] = []
    columns: list[int] = []
    ad = DetectedObject("newspaper", 600, 453, 340, 260, 1.0, "ad")
    monkeypatch.setattr(actions, "go_home", lambda: homes.append(1) or ActionResult(True, Action("GO_HOME", "house")))
    monkeypatch.setattr(actions, "find_column", lambda: columns.append(1) or ActionResult(True, Action("FIND_COLUMN", "stand")))
    monkeypatch.setattr(actions, "open_newspaper", lambda: ActionResult(True, Action("OPEN_NEWSPAPER", "stand")))
    monkeypatch.setattr(actions, "_next_ad", lambda: ad)
    monkeypatch.setattr(actions, "visit_shop", lambda _ad: ActionResult(True, Action("VISIT_SHOP", "ad")))
    monkeypatch.setattr(actions, "buy_wishlist", lambda: ActionResult(True, Action("BUY", "egg")))
    monkeypatch.setattr(actions, "close_shop", lambda: ActionResult(True, Action("CLOSE_SHOP", "shop")))
    result = actions.shop_from_newspaper(limit=2, reset_home=False)
    assert result.success
    assert columns == [1, 1]
    assert homes == [1]


def test_cli_detect_newspaper(capsys):
    assert main(["detect", str(NEWSPAPER)]) == 0
    out = capsys.readouterr().out
    assert "screen\tNEWSPAPER" in out


def test_browse_scans_ads_without_visiting_shop(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    ads = NewspaperDetector().find_ads(NEWSPAPER)
    centers = {(a.x + a.width // 2, a.y + a.height // 2) for a in ads}
    stand = NewspaperDetector().find_stand(FARM)
    assert stand is not None
    stand_tap = (stand.x + stand.width // 2, stand.y + stand.height // 2)
    dest = tmp_path / "browse_tpl"
    dest.mkdir()
    names = (
        "newspaper_ad",
        "newspaper_close",
        "newspaper_stand",
        "newspaper_stand_zoom",
        "popup_close",
        "shop_close",
        "shop_header",
        "hud_shop",
        "hud_friends",
        "hud_settings",
        "item_wheat",
    )
    for name in names:
        src = TEMPLATES / f"{name}.png"
        if src.is_file():
            (dest / f"{name}.png").write_bytes(src.read_bytes())
    matcher = TemplateMatcher(templates_dir=dest, scales=(1.0,))
    device = FakeDevice(
        [
            _png_bytes(_bgr(NEWSPAPER)),
            _png_bytes(_bgr(FARM)),
            _png_bytes(_bgr(NEWSPAPER)),
        ]
    )
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.shop_from_newspaper(limit=3, mode="browse")
    assert result.success
    assert result.action.type == "BROWSE"
    assert len(device.swipes) == 4
    assert stand_tap in device.taps
    assert not (set(device.taps) & centers)


def test_find_crate_icons_on_player_shop():
    icons = NewspaperDetector().find_crate_icons(PLAYER_SHOP)
    assert icons
    icon = icons[0]
    assert icon.width >= 40
    assert icon.height >= 40
    assert icon.image.shape[0] == icon.height


def test_capture_open_shop_saves_library(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("app.vision.template_matcher.TEMPLATES_DIR", tmp_path / "templates")
    (tmp_path / "templates").mkdir()
    for name in ("shop_header", "popup_close", "shop_close"):
        src = TEMPLATES / f"{name}.png"
        if src.is_file():
            (tmp_path / "templates" / src.name).write_bytes(src.read_bytes())
    matcher = TemplateMatcher(templates_dir=tmp_path / "templates", scales=(1.0,))
    device = FakeDevice([_png_bytes(_bgr(PLAYER_SHOP))])
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.capture_open_shop()
    assert result.success
    assert result.action.type == "CAPTURE"
    assert "shop_" in result.action.target
    assert (tmp_path / "library" / "shop_01.png").is_file()
    assert not (tmp_path / "templates" / "item_cap_01.png").is_file()


def test_scan_ads_on_newspaper_page(no_sleep, tmp_path, monkeypatch):
    monkeypatch.setattr("app.actions.shop.active_wishlist", lambda: {})
    dest = tmp_path / "tpl"
    dest.mkdir()
    src = TEMPLATES / "newspaper_ad.png"
    (dest / src.name).write_bytes(src.read_bytes())
    matcher = TemplateMatcher(templates_dir=dest, scales=(1.0,))
    device = FakeDevice([_png_bytes(_bgr(NEWSPAPER))])
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0
    )
    result = actions.scan_ads()
    assert result.success
    assert result.action.type == "SCAN_ADS"


def test_swipe_newspaper_records_swipe(no_sleep, tmp_path):
    dest = tmp_path / "tpl"
    dest.mkdir()
    src = TEMPLATES / "newspaper_ad.png"
    (dest / src.name).write_bytes(src.read_bytes())
    matcher = TemplateMatcher(templates_dir=dest, scales=(1.0,))
    device = FakeDevice([_png_bytes(_bgr(NEWSPAPER))])
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0
    )
    result = actions.swipe_newspaper()
    assert result.success
    assert device.swipes
    x1, y1, x2, y2, _ms = device.swipes[0]
    assert x1 > x2


def test_visit_next_shop_skips_when_no_match(no_sleep, tmp_path, monkeypatch):
    monkeypatch.setattr("app.actions.shop.active_wishlist", lambda: {})
    dest = tmp_path / "tpl"
    dest.mkdir()
    src = TEMPLATES / "newspaper_ad.png"
    (dest / src.name).write_bytes(src.read_bytes())
    matcher = TemplateMatcher(templates_dir=dest, scales=(1.0,))
    device = FakeDevice([_png_bytes(_bgr(NEWSPAPER))])
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.visit_next_shop()
    assert result.success is False
    assert device.taps == []


def test_visit_next_shop_taps_wishlist_ad(no_sleep, tmp_path, monkeypatch):
    ads = NewspaperDetector().find_ads(NEWSPAPER)
    assert ads
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    monkeypatch.setattr(
        NewspaperActions, "_unused_wishlist_ad", lambda self, png: ads[0]
    )
    dest = tmp_path / "tpl"
    dest.mkdir()
    for name in (
        "shop_header",
        "shop_close",
        "popup_close",
        "hud_shop",
        "hud_friends",
        "hud_settings",
    ):
        src = TEMPLATES / f"{name}.png"
        if src.is_file():
            (dest / src.name).write_bytes(src.read_bytes())
    matcher = TemplateMatcher(templates_dir=dest, scales=(1.0,))
    device = FakeDevice(
        [_png_bytes(_bgr(NEWSPAPER)), _png_bytes(_bgr(PLAYER_SHOP))]
    )
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.visit_next_shop()
    assert result.success
    assert result.action.type == "VISIT_SHOP"
    assert device.taps
