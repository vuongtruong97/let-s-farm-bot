from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.actions.shop import STALL_CREEP_PX, NewspaperActions
from app.config import AppConfig
from app.main import main
from app.storage.botdata import load_column, save_column
from app.vision.detector import DetectedObject
from app.vision.newspaper import NewspaperDetector, crate_view_shift
from app.vision.regions import NEWS_PAGE_COUNT, NEWS_PROMO_SLOTS, news_spread_slots
from app.vision.screen import GameScreen, ScreenDetector
from app.vision.template_matcher import TemplateMatcher
from tests.test_farming import FakeDevice, _png_bytes

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FARM = FIXTURES / "farm.png"
FARM_NO_COLUMN = FIXTURES / "farm_no_column.png"
NEWSPAPER = FIXTURES / "newspaper.png"
NEWSPAPER_VISITED = FIXTURES / "newspaper_visited.png"
PLAYER_SHOP = FIXTURES / "player_shop.png"
# Live stalls: one whose table fits the window, one opened mid-table.
STALL_FITS = FIXTURES / "stall_fits.jpg"
STALL_CLIPPED = FIXTURES / "stall_clipped.jpg"
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


STAND_AT = (500, 400)


def _newspaper_right_page_only() -> np.ndarray:
    """Opening spread whose left leaf is still farm grass — page 2 not drawn."""
    news = _bgr(NEWSPAPER)
    farm = _bgr(FARM_NO_COLUMN)
    h, w = news.shape[:2]
    for page, slot, x, y, bw, bh in news_spread_slots(w, h, 2):
        if page != 2 or (page, slot) in NEWS_PROMO_SLOTS:
            continue
        news[y : y + bh, x : x + bw] = farm[y : y + bh, x : x + bw]
    return news


def _farm_with_stand() -> np.ndarray:
    """Farm frame with the 1:1 mailbox crop pasted in. The live farm fixture
    is a distant view; FIND_COLUMN now only looks at native size."""
    farm = _bgr(FARM_NO_COLUMN)
    crop = cv2.imread(str(TEMPLATES / "newspaper_stand.png"))
    assert crop is not None
    y, x = STAND_AT[1], STAND_AT[0]
    farm[y : y + crop.shape[0], x : x + crop.shape[1]] = crop
    return farm


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
    monkeypatch.setattr("app.storage.botdata.purchases_path", lambda: tmp_path / "purchases.json")


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("app.actions.shop.time.sleep", lambda _s: None)
    monkeypatch.setattr("app.actions.timing.time.sleep", lambda _s: None)


def test_farm_fixture_finds_newspaper_stand():
    farm = _farm_with_stand()
    stand = NewspaperDetector().find_stand(farm)
    assert stand is not None
    assert stand.state == "stand"
    assert stand.confidence >= 0.90
    assert abs(stand.x - STAND_AT[0]) < 4
    assert abs(stand.y - STAND_AT[1]) < 4


def test_farm_no_column_has_no_stand():
    assert NewspaperDetector().find_stand(FARM_NO_COLUMN) is None


def test_find_stand_ignores_a_scaled_copy():
    """The mailbox is captured at native size, so a zoomed-out crop must not
    count as the stand — that was the two-template miss."""
    farm = _bgr(FARM_NO_COLUMN)
    crop = cv2.imread(str(TEMPLATES / "newspaper_stand.png"))
    assert crop is not None
    small = cv2.resize(crop, (crop.shape[1] // 2, crop.shape[0] // 2))
    y, x = 680, 10
    farm[y : y + small.shape[0], x : x + small.shape[1]] = small
    assert NewspaperDetector().find_stand(farm) is None


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
    device = FakeDevice([_png_bytes(_farm_with_stand())])
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
        [_png_bytes(_bgr(FARM_NO_COLUMN)), _png_bytes(_farm_with_stand())]
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
        [_png_bytes(_bgr(FARM_NO_COLUMN)), _png_bytes(_farm_with_stand())]
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


def test_newspaper_waits_until_page_2_listings_draw(no_sleep):
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    assert actions._newspaper_is_open(_bgr(NEWSPAPER)) is True
    assert actions._newspaper_is_open(_newspaper_right_page_only()) is False


def test_open_newspaper_waits_out_slow_draw(no_sleep):
    save_column(140, 820)
    device = FakeDevice([_png_bytes(_bgr(FARM)), _png_bytes(_bgr(NEWSPAPER))])
    actions = NewspaperActions(
        device, AppConfig(debug=False), wait_s=0, visit_wait_s=2.5
    )
    result = actions.open_newspaper()
    assert result.success
    assert device.taps == [(140, 820)]
    assert load_column() == (140, 820)


def test_open_newspaper_waits_for_left_leaf(no_sleep):
    save_column(140, 820)
    device = FakeDevice(
        [_png_bytes(_newspaper_right_page_only()), _png_bytes(_bgr(NEWSPAPER))]
    )
    actions = NewspaperActions(
        device, AppConfig(debug=False), wait_s=0, visit_wait_s=2.5
    )
    result = actions.open_newspaper()
    assert result.success
    assert device.taps == [(140, 820)]


def test_open_newspaper_taps_stand(no_sleep):
    farm = _farm_with_stand()
    device = FakeDevice([_png_bytes(farm), _png_bytes(_bgr(NEWSPAPER))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.open_newspaper()
    assert result.success
    assert device.taps
    stand = NewspaperDetector().find_stand(farm)
    assert stand is not None
    tx, ty = device.taps[0]
    assert stand.x <= tx <= stand.x + stand.width
    assert stand.y <= ty <= stand.y + stand.height


def test_shop_egg_slot_is_buyable():
    slots = NewspaperDetector().find_slots(
        FIXTURES / "shop_egg.png", {"trung": {"template": "item_trung"}}
    )
    assert slots
    assert slots[0].item == "trung"
    assert slots[0].buyable
    assert slots[0].confidence >= 0.72


def test_read_crate_qty_egg_is_8():
    from app.vision.newspaper import crate_proof_crop

    news = NewspaperDetector()
    egg = _bgr(FIXTURES / "shop_egg.png")
    slot = news.find_slots(egg, {"trung": {"template": "item_trung"}})[0]
    assert news.read_crate_qty(egg, slot) == 8
    crop = crate_proof_crop(egg, slot)
    assert crop.shape[0] > slot.height
    assert crop.shape[1] > slot.width
    hit = news.matcher.match_one(crop, "qty_x", threshold=0.70)
    assert hit is not None


def test_read_crate_qty_popcorn_is_7():
    news = NewspaperDetector()
    shop = _bgr(PLAYER_SHOP)
    slot = news.find_slots(shop, {"pop": {"template": "item_bong_ngo_cay"}})[0]
    assert news.read_crate_qty(shop, slot) == 7


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


def test_find_slots_only_looks_at_native_size(tmp_path):
    """A crate draws at the template's own size, so the scan stays at 1.0.

    The matcher here is happy to try 1.25, but find_slots must not: every extra
    scale multiplies the cost of the one call the buy loop makes per window.
    """
    color = np.zeros((48, 48, 3), dtype=np.uint8)
    cv2.rectangle(color, (4, 4), (43, 43), (0, 60, 255), -1)
    cv2.circle(color, (24, 24), 12, (40, 220, 40), -1)
    bigger = cv2.resize(color, (60, 60), interpolation=cv2.INTER_LINEAR)
    shop = _bgr(PLAYER_SHOP)
    shop[420:468, 640:688] = color
    shop[520:580, 900:960] = bigger
    dest = tmp_path / "tpl"
    dest.mkdir()
    cv2.imwrite(str(dest / "item_demo.png"), color)
    matcher = TemplateMatcher(templates_dir=dest, threshold=0.72, scales=(1.0, 1.25))
    slots = NewspaperDetector(matcher).find_slots(
        shop, {"demo": {"template": "item_demo"}}
    )
    assert [(slot.width, slot.height) for slot in slots] == [(48, 48)]
    assert abs(slots[0].x - 640) < 6


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


def test_visit_shop_takes_one_shot_when_stall_is_already_up(no_sleep):
    ads = NewspaperDetector().find_ads(NEWSPAPER)
    device = FakeDevice([_png_bytes(_bgr(PLAYER_SHOP))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.visit_shop(ads[0])
    assert result.success
    snap = actions.timing.snapshot()
    assert snap["shot"]["n"] == 1
    assert snap["steps"]["visit_shop"]["n"] == 1
    assert snap["steps"]["visit_shop"]["deadline_hits"] == 0


def test_visit_shop_polls_until_stall_draws(no_sleep):
    ads = NewspaperDetector().find_ads(NEWSPAPER)
    device = FakeDevice([_png_bytes(_bgr(FARM)), _png_bytes(_bgr(PLAYER_SHOP))])
    actions = NewspaperActions(
        device, AppConfig(debug=False), wait_s=0, visit_wait_s=5
    )
    result = actions.visit_shop(ads[0])
    assert result.success
    snap = actions.timing.snapshot()
    # Second frame answered; the 5s ceiling was never waited out.
    assert snap["shot"]["n"] == 2
    assert snap["steps"]["visit_shop"]["deadline_hits"] == 0
    assert snap["steps"]["visit_shop"]["last_s"] < 5


def test_visit_shop_counts_deadline_hit_when_stall_never_draws(no_sleep):
    ads = NewspaperDetector().find_ads(NEWSPAPER)
    device = FakeDevice([_png_bytes(_bgr(FARM))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.visit_shop(ads[0])
    assert result.success is False
    snap = actions.timing.snapshot()
    assert snap["steps"]["visit_shop"]["deadline_hits"] == 1
    assert snap["safety"]["visit_fail"] == 1


def test_visit_shop_hands_stall_frame_to_buy_wishlist(no_sleep, monkeypatch):
    monkeypatch.setattr("app.actions.shop.active_wishlist", lambda: {})
    ads = NewspaperDetector().find_ads(NEWSPAPER)
    device = FakeDevice([_png_bytes(_bgr(PLAYER_SHOP))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    assert actions.visit_shop(ads[0]).success
    assert actions._pending_png is not None


def test_find_column_keeps_visited_ads(no_sleep):
    device = FakeDevice([_png_bytes(_farm_with_stand())])
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
    _skip_rewind(actions, monkeypatch)
    result = actions.buy_wishlist()
    assert result.success
    assert result.action.target == "wheat"
    assert device.taps
    from app.storage.botdata import list_purchases

    rows = list_purchases()
    assert [row["kind"] for row in rows] == ["buy", "match"]
    assert rows[0]["item"] == "wheat"
    assert "T" in rows[0]["at"]
    assert rows[0]["image"] == "buy_01"
    from app.storage.botdata import buy_proof_png

    assert buy_proof_png("buy_01").is_file()
    assert "qty" not in rows[0]


def test_stall_match_records_wishlist_when_buy_verify_fails(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    icon = _item_icon()
    coin = cv2.imread(str(TEMPLATES / "price_coin.png"))
    assert coin is not None
    stall = _bgr(PLAYER_SHOP)
    stall[360:404, 640:684] = icon
    stall[360 : 360 + coin.shape[0], 690 : 690 + coin.shape[1]] = coin
    matcher = TemplateMatcher(templates_dir=_tpl_dir(tmp_path, {"item_wheat": icon}), scales=(1.0,))
    device = FakeDevice([_png_bytes(stall), _png_bytes(stall), _png_bytes(stall)])
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.buy_wishlist()
    assert not result.success
    assert result.error == "verify failed"
    from app.storage.botdata import list_purchases

    rows = list_purchases()
    assert [row["kind"] for row in rows] == ["match"]
    assert rows[0]["item"] == "wheat"
    assert "image" not in rows[0]
    assert "qty" not in rows[0]
    assert list((tmp_path / "buy_proofs").glob("*.png")) == []


def test_buy_wishlist_succeeds_when_icon_turns_gray(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    icon = _item_icon()
    gray = cv2.cvtColor(cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    before = _bgr(PLAYER_SHOP)
    before[360:404, 640:684] = icon
    after = _bgr(PLAYER_SHOP)
    after[360:404, 640:684] = gray
    matcher = TemplateMatcher(templates_dir=_tpl_dir(tmp_path, {"item_wheat": icon}), scales=(1.0,))
    device = FakeDevice([_png_bytes(before), _png_bytes(before), _png_bytes(after)])
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    _skip_rewind(actions, monkeypatch)
    result = actions.buy_wishlist()
    assert result.success
    assert result.action.target == "wheat"


def test_buy_wishlist_allows_popup_screen(tmp_path, no_sleep, monkeypatch):
    from app.vision.screen import GameScreen, ScreenDetection

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
    monkeypatch.setattr(
        actions.screens,
        "detect",
        lambda _png: ScreenDetection(GameScreen.POPUP, 0.99, []),
    )
    _skip_rewind(actions, monkeypatch)
    result = actions.buy_wishlist()
    assert result.success
    assert result.action.target == "wheat"


def test_buy_wishlist_continues_after_verify_fail(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {
            "so_do": {"template": "item_so_do", "enabled": True},
            "wheat": {"template": "item_wheat", "enabled": True},
        },
    )
    fail_icon = np.zeros((44, 44, 3), dtype=np.uint8)
    fail_icon[:] = (0, 0, 255)
    cv2.rectangle(fail_icon, (4, 4), (39, 39), (255, 255, 0), 3)
    ok_icon = _item_icon()
    both = _bgr(PLAYER_SHOP)
    both[360:404, 500:544] = fail_icon
    both[360:404, 640:684] = ok_icon
    after_ok = _bgr(PLAYER_SHOP)
    after_ok[360:404, 500:544] = fail_icon
    matcher = TemplateMatcher(
        templates_dir=_tpl_dir(
            tmp_path, {"item_so_do": fail_icon, "item_wheat": ok_icon}
        ),
        scales=(1.0,),
    )
    device = FakeDevice(
        [
            _png_bytes(both),
            _png_bytes(both),
            _png_bytes(both),
            _png_bytes(both),
            _png_bytes(after_ok),
            _png_bytes(after_ok),
        ]
    )
    actions = NewspaperActions(
        device, AppConfig(debug=False), matcher=matcher, wait_s=0, visit_wait_s=0
    )
    result = actions.buy_wishlist()
    assert result.success
    assert result.action.target == "wheat"
    assert len(device.taps) == 2
    assert device.taps[0][0] < device.taps[1][0]


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
    _skip_rewind(actions, monkeypatch)
    result = actions.buy_wishlist()
    assert result.success
    assert result.action.target == "wheat"
    assert device.taps


def _wheat_shop(tmp_path, monkeypatch):
    """Stall with one wishlist crate, plus the frame after it is gone."""
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    icon = _item_icon()
    before = _bgr(PLAYER_SHOP)
    before[360:404, 640:684] = icon
    after = _bgr(PLAYER_SHOP)
    matcher = TemplateMatcher(
        templates_dir=_tpl_dir(tmp_path, {"item_wheat": icon}), scales=(1.0,)
    )
    return before, after, matcher


def test_buy_one_uses_single_shot_when_crate_leaves(tmp_path, no_sleep, monkeypatch):
    before, after, matcher = _wheat_shop(tmp_path, monkeypatch)
    device = FakeDevice([_png_bytes(before), _png_bytes(after)])
    actions = NewspaperActions(
        device,
        AppConfig(debug=False),
        matcher=matcher,
        wait_s=0,
        buy_wait_s=0,
        visit_wait_s=0,
    )
    _skip_rewind(actions, monkeypatch)
    result = actions.buy_wishlist()
    assert result.success
    snap = actions.timing.snapshot()
    # Tap, one verify shot, and that frame is reused to look for more crates.
    assert snap["steps"]["buy_one"]["shots_avg"] == 1
    assert snap["safety"]["buys"] == 1
    assert snap["safety"]["verify_fail"] == 0


def test_buy_one_marks_verify_fail_when_crate_stays(tmp_path, no_sleep, monkeypatch):
    before, _after, matcher = _wheat_shop(tmp_path, monkeypatch)
    device = FakeDevice([_png_bytes(before)])
    actions = NewspaperActions(
        device,
        AppConfig(debug=False),
        matcher=matcher,
        wait_s=0,
        buy_wait_s=0,
        visit_wait_s=0,
    )
    _skip_rewind(actions, monkeypatch)
    result = actions.buy_wishlist()
    assert result.success is False
    snap = actions.timing.snapshot()
    assert snap["safety"]["verify_fail"] == 1
    assert snap["safety"]["buy_skip"] == 1
    assert snap["safety"]["buys"] == 0
    assert snap["steps"]["buy_one"]["deadline_hits"] == 1


def _pan(direction: str) -> tuple:
    from app.vision.regions import stall_swipe_px

    return stall_swipe_px(1920, 1080, direction)


def _swipes(device) -> list[tuple]:
    return [(s[0], s[1], s[2], s[3]) for s in device.swipes]


def _stall_window(offset: int, *, icon=None) -> "np.ndarray":
    """One scroll position of a stall. The crate table is rolled sideways so
    the pan detector sees a real horizontal shift, the way the game scrolls."""
    view = _bgr(PLAYER_SHOP)
    table = view[324:842, 307:1613]
    view[324:842, 307:1613] = np.roll(table, offset, axis=1)
    if icon is not None:
        view[420:464, 640:684] = icon
    return view


def _one_item_shop(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    icon = _item_icon()
    matcher = TemplateMatcher(
        templates_dir=_tpl_dir(tmp_path, {"item_wheat": icon}), scales=(1.0,)
    )
    return icon, matcher


def _skip_rewind(actions, monkeypatch) -> None:
    """For tests about buying rather than about finding the stall's left edge."""
    monkeypatch.setattr(actions, "_stall_rewind", lambda png: png)


def _stall_actions(tmp_path, monkeypatch, frames, **kwargs):
    _icon, matcher = _one_item_shop(tmp_path, monkeypatch)
    device = FakeDevice([_png_bytes(frame) for frame in frames])
    opts = {"wait_s": 0, "buy_wait_s": 0, "visit_wait_s": 0} | kwargs
    actions = NewspaperActions(device, AppConfig(debug=False), matcher=matcher, **opts)
    # _stall_window rolls the table, which wraps the cloth around to the far
    # side. Reading an edge of a rolled fixture says nothing about a real
    # stall, so these tests exercise the measured pans instead.
    monkeypatch.setattr(actions, "_stall_ends_at", lambda png, side: False)
    return device, actions


def test_stall_pan_measures_the_frame_that_stopped_moving(
    tmp_path, no_sleep, monkeypatch
):
    """The table glides on after the finger lifts. Measuring a mid-glide frame
    reads a distance the table had not travelled yet."""
    start = _stall_window(0)
    device, actions = _stall_actions(
        tmp_path,
        monkeypatch,
        [_stall_window(150), _stall_window(300), _stall_window(300)],
        wait_s=0.5,
    )
    pan = actions._stall_pan(start, "right")
    assert pan.moved is True
    # The mid-glide 150 was thrown away for the 300 the table came to rest at.
    assert crate_view_shift(start, pan.png) == pytest.approx(300, abs=2)
    assert actions.timing.snapshot()["steps"]["stall_pan"]["shots_avg"] == 3


def test_swallowed_swipe_is_not_an_edge(tmp_path, no_sleep, monkeypatch):
    """Measured on a still frame, a swipe the game dropped looks exactly like
    an edge. Asking twice is what tells them apart."""
    start = _stall_window(0)
    device, actions = _stall_actions(
        tmp_path, monkeypatch, [start, _stall_window(300)]
    )
    pan = actions._stall_pan(start, "right")
    assert pan.moved is True
    assert _swipes(device) == [_pan("right"), _pan("right")]


def test_edge_is_only_called_after_a_second_swipe(tmp_path, no_sleep, monkeypatch):
    start = _stall_window(0)
    device, actions = _stall_actions(tmp_path, monkeypatch, [start])
    pan = actions._stall_pan(start, "right")
    assert pan.moved is False
    assert _swipes(device) == [_pan("right"), _pan("right")]


def test_mid_leg_still_swipe_is_the_edge_at_once(tmp_path, no_sleep, monkeypatch):
    """Past a leg's first swipe the game takes swipes, so a table that stays
    put is against its stop and a second push would only cost time."""
    start = _stall_window(0)
    device, actions = _stall_actions(tmp_path, monkeypatch, [start])
    pan = actions._stall_pan(start, "right", confirm=False)
    assert pan.moved is False
    assert _swipes(device) == [_pan("right")]


def test_whole_column_pan_read_as_a_small_shift_still_counts(
    tmp_path, no_sleep, monkeypatch
):
    """Phase correlation on look-alike crates reads a pan of whole columns as a
    few px. Other crates in view is what gives it away."""
    start = _stall_window(0)
    device, actions = _stall_actions(tmp_path, monkeypatch, [_stall_window(534)])
    monkeypatch.setattr("app.actions.shop.crate_view_shift", lambda a, b: 3.0)
    pan = actions._stall_pan(start, "right", confirm=False)
    assert pan.moved is True


def test_view_residual_ignores_sparkle_but_not_a_pan():
    from app.vision.newspaper import SAME_VIEW_RESIDUAL, stall_view_residual

    still = _stall_window(0)
    speckled = still.copy()
    rng = np.random.default_rng(7)
    speckled[rng.integers(330, 840, 40000), rng.integers(374, 1554, 40000)] = 255
    assert stall_view_residual(still, speckled, 0.0) < SAME_VIEW_RESIDUAL
    assert stall_view_residual(still, _stall_window(25), 25.0) < SAME_VIEW_RESIDUAL
    assert stall_view_residual(still, _stall_window(534), 0.0) >= SAME_VIEW_RESIDUAL


def test_creeping_against_the_edge_is_not_a_new_window(
    tmp_path, no_sleep, monkeypatch
):
    """An edge has give: a swipe into one shifts the table ~25px before it
    springs back. Counting that as a pan is what kept the bot swiping."""
    start = _stall_window(0)
    device, actions = _stall_actions(
        tmp_path, monkeypatch, [_stall_window(25), _stall_window(25)]
    )
    pan = actions._stall_pan(start, "right")
    assert pan.moved is False
    assert pan.dx == pytest.approx(25, abs=3)


def test_cloth_at_the_edge_tells_a_stall_that_cannot_pan():
    """Wider bare cloth than one crate gap next to a post means nothing is
    hidden there. A gap's worth of cloth proves nothing: mid-table shows the
    same whenever a gap lines up with the post."""
    from app.vision.newspaper import stall_end_cloth, stall_end_in_view

    fits = _bgr(STALL_FITS)
    assert stall_end_in_view(fits, "left")
    assert stall_end_in_view(fits, "right")
    # This one opened mid-table: the left column is cut off by the frame, and
    # the right shows a crate gap of cloth, not a proven end.
    clipped = _bgr(STALL_CLIPPED)
    assert not stall_end_in_view(clipped, "left")
    assert 0 < stall_end_cloth(clipped, "right") < 60
    assert not stall_end_in_view(clipped, "right")
    wide = _bgr(PLAYER_SHOP)
    assert not stall_end_in_view(wide, "left")
    assert not stall_end_in_view(wide, "right")


def test_a_stall_that_fits_the_window_costs_no_swipe(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    matcher = TemplateMatcher(
        templates_dir=_tpl_dir(tmp_path, {"item_wheat": _item_icon()}), scales=(1.0,)
    )
    device = FakeDevice([_png_bytes(_bgr(STALL_FITS))])
    actions = NewspaperActions(
        device,
        AppConfig(debug=False),
        matcher=matcher,
        wait_s=0,
        buy_wait_s=0,
        visit_wait_s=0,
    )
    actions.buy_wishlist()
    # Cloth shows at both ends, so neither the rewind nor the sweep has any
    # reason to push against a stop.
    assert device.swipes == []


def test_a_clipped_column_is_still_panned_to(tmp_path, no_sleep, monkeypatch):
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {"wheat": {"template": "item_wheat", "enabled": True}},
    )
    matcher = TemplateMatcher(
        templates_dir=_tpl_dir(tmp_path, {"item_wheat": _item_icon()}), scales=(1.0,)
    )
    device = FakeDevice([_png_bytes(_bgr(STALL_CLIPPED))])
    actions = NewspaperActions(
        device,
        AppConfig(debug=False),
        matcher=matcher,
        wait_s=0,
        buy_wait_s=0,
        visit_wait_s=0,
    )
    actions.buy_wishlist()
    # A crate covers the cloth on the left, so that column gets rewound to.
    # The right shows no more cloth than a crate gap, so it is probed too.
    assert _pan("left") in _swipes(device)
    assert _pan("right") in _swipes(device)


def test_rewind_measures_its_way_to_the_left_edge(tmp_path, no_sleep, monkeypatch):
    """Swipes fired one after another carry a different distance than a
    deliberate nudge, so the rewind is measured rather than counted."""
    device, actions = _stall_actions(
        tmp_path,
        monkeypatch,
        [_stall_window(120), _stall_window(0), _stall_window(0)],
    )
    edge = actions._stall_rewind(_stall_window(300))
    # Two swipes carried it back 180px then 120px. Mid-leg, one still swipe is
    # the edge: only a leg's first swipe gets asked twice.
    assert _swipes(device) == [_pan("left")] * 3
    assert crate_view_shift(_stall_window(0), edge) == pytest.approx(0, abs=3)
    assert actions.timing.snapshot()["steps"]["stall_rewind"]["n"] == 3


def test_stall_is_rewound_before_the_first_look(tmp_path, no_sleep, monkeypatch):
    """A stall opens wherever the player left it, so no window is worth looking
    at until the table is against its left edge."""
    device, actions = _stall_actions(
        tmp_path,
        monkeypatch,
        [_stall_window(300), _stall_window(0), _stall_window(0)],
    )
    swipes_before_first_look = None
    real_find_slots = actions.news.find_slots

    def spy(png, ready):
        nonlocal swipes_before_first_look
        if swipes_before_first_look is None:
            swipes_before_first_look = list(_swipes(device))
        return real_find_slots(png, ready)

    monkeypatch.setattr(actions.news, "find_slots", spy)
    actions.buy_wishlist()
    # One pan carried it home, then one still swipe was the edge.
    assert swipes_before_first_look == [_pan("left")] * 2


def test_crate_left_of_the_opening_window_is_bought(tmp_path, no_sleep, monkeypatch):
    """The stall opened mid-table with the crate behind it. Buying it is the
    whole point of rewinding first."""
    icon, matcher = _one_item_shop(tmp_path, monkeypatch)
    device = FakeDevice(
        [
            _png_bytes(_stall_window(300)),
            _png_bytes(_stall_window(0, icon=icon)),
            _png_bytes(_stall_window(0, icon=icon)),
            _png_bytes(_stall_window(0, icon=icon)),
            _png_bytes(_stall_window(0)),
        ]
    )
    actions = NewspaperActions(
        device,
        AppConfig(debug=False),
        matcher=matcher,
        wait_s=0,
        buy_wait_s=0,
        visit_wait_s=0,
    )
    result = actions.buy_wishlist()
    assert result.success
    assert result.action.target == "wheat"


def test_sweep_leaves_the_stall_where_it_ended(tmp_path, no_sleep, monkeypatch):
    """Nothing depends on where the stall is parked, so nothing pans it back."""
    device, actions = _stall_actions(
        tmp_path,
        monkeypatch,
        [_stall_window(0), _stall_window(0), _stall_window(300), _stall_window(300)],
    )
    actions.buy_wishlist()
    rights = _swipes(device).index(_pan("right"))
    # Every left swipe belongs to the rewind, none to a way back.
    assert _pan("left") not in _swipes(device)[rights:]


def test_interrupted_sweep_stops_without_panning(tmp_path, no_sleep, monkeypatch):
    device, actions = _stall_actions(
        tmp_path, monkeypatch, [_stall_window(0), _stall_window(0)]
    )
    monkeypatch.setattr(actions, "_stall_rewind", lambda png: png)
    result = actions.buy_wishlist(should_stop=lambda: True)
    assert result.success is False
    assert result.error == "stopped"
    assert device.swipes == []


def test_pan_shift_survives_a_busy_frame(tmp_path, no_sleep, monkeypatch):
    """Counting changed pixels calls an idle animation movement and a slow
    redraw an edge. The measured shift is what decides."""
    from app.vision.newspaper import crate_view_shift

    still = _stall_window(0)
    speckled = still.copy()
    rng = np.random.default_rng(7)
    ys = rng.integers(330, 840, 40000)
    xs = rng.integers(310, 1610, 40000)
    speckled[ys, xs] = 255
    assert abs(crate_view_shift(still, speckled)) < STALL_CREEP_PX
    assert abs(crate_view_shift(still, _stall_window(300))) == pytest.approx(300, abs=2)


def test_buy_wishlist_defers_purchase_writes(tmp_path, no_sleep, monkeypatch):
    before, after, matcher = _wheat_shop(tmp_path, monkeypatch)
    writes: list[tuple] = []
    monkeypatch.setattr(
        "app.actions.shop.record_purchase",
        lambda item, **kw: writes.append((item, kw.get("kind"))),
    )
    device = FakeDevice([_png_bytes(before), _png_bytes(after)])
    actions = NewspaperActions(
        device,
        AppConfig(debug=False),
        matcher=matcher,
        wait_s=0,
        buy_wait_s=0,
        visit_wait_s=0,
    )
    _skip_rewind(actions, monkeypatch)

    real_buy_one = actions._buy_one

    def spy(slot):
        # Nothing on disk yet while crates are still being tapped.
        assert writes == []
        return real_buy_one(slot)

    monkeypatch.setattr(actions, "_buy_one", spy)
    assert actions.buy_wishlist().success
    assert writes == [("wheat", "match"), ("wheat", "buy")]


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


def test_crate_view_shift_measures_the_scroll():
    from app.vision.newspaper import crate_view_shift

    base = _bgr(PLAYER_SHOP)
    assert crate_view_shift(base, base) == pytest.approx(0, abs=1)
    assert crate_view_shift(base, _stall_window(240)) == pytest.approx(240, abs=2)
    assert crate_view_shift(_stall_window(240), base) == pytest.approx(-240, abs=2)


def test_sweep_buys_a_crate_in_every_window(tmp_path, no_sleep, monkeypatch):
    """Two crates two windows apart, both bought on the one leg right."""
    monkeypatch.setattr(
        "app.actions.shop.active_wishlist",
        lambda: {
            "near_item": {"template": "item_near_item", "enabled": True},
            "far_item": {"template": "item_far_item", "enabled": True},
        },
    )
    near_icon = np.zeros((44, 44, 3), dtype=np.uint8)
    near_icon[:] = (0, 0, 255)
    cv2.rectangle(near_icon, (4, 4), (39, 39), (255, 255, 0), 3)
    far_icon = np.zeros((44, 44, 3), dtype=np.uint8)
    far_icon[:] = (255, 0, 0)
    cv2.circle(far_icon, (22, 22), 16, (0, 255, 255), -1)
    near = _stall_window(0, icon=near_icon)
    near_gone = _stall_window(0)
    far = _stall_window(300, icon=far_icon)
    far_gone = _stall_window(300)
    matcher = TemplateMatcher(
        templates_dir=_tpl_dir(
            tmp_path, {"item_near_item": near_icon, "item_far_item": far_icon}
        ),
        scales=(1.0,),
    )
    device = FakeDevice(
        [
            _png_bytes(near),
            _png_bytes(near_gone),
            _png_bytes(near_gone),
            _png_bytes(far),
            _png_bytes(far_gone),
        ]
    )
    actions = NewspaperActions(
        device,
        AppConfig(debug=False),
        matcher=matcher,
        wait_s=0,
        buy_wait_s=0,
        visit_wait_s=0,
    )
    _skip_rewind(actions, monkeypatch)
    assert actions.buy_wishlist().success
    assert actions.timing.snapshot()["safety"]["buys"] == 2
    assert _pan("right") in _swipes(device)


def _friend_farm() -> "np.ndarray":
    """A friend's farm: the same HUD, but no cart bottom-left (a house there)."""
    view = _bgr(FARM)
    h, w = view.shape[:2]
    view[int(h * 0.78) :, : int(w * 0.14)] = (60, 140, 60)
    return view


def test_go_home_follows_the_screens_it_is_shown(no_sleep):
    """Loading, the friend's stall that opens on arrival, the friend's farm,
    then home: each tap waits for the screen it is meant for."""
    from app.vision.regions import hud_tap

    device = FakeDevice(
        [
            _png_bytes(_bgr(FARM)),
            _png_bytes(_bgr(FIXTURES / "unknown.png")),
            _png_bytes(_bgr(PLAYER_SHOP)),
            _png_bytes(_friend_farm()),
            _png_bytes(_bgr(FIXTURES / "unknown.png")),
            _png_bytes(_bgr(FARM)),
        ]
    )
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


def test_a_friends_farm_is_not_home():
    device = FakeDevice([_png_bytes(_bgr(FARM))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    assert actions._where(_bgr(FARM)) == "home"
    assert actions._where(_friend_farm()) == "friend"
    assert actions._where(_bgr(PLAYER_SHOP)) == "stall"


def test_close_shop_taps_fixed_point_and_hands_frame_over(no_sleep):
    from app.vision.regions import hud_tap

    device = FakeDevice([_png_bytes(_farm_with_stand())])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.close_shop()
    assert result.success
    assert result.action.type == "CLOSE_SHOP"
    width, height = device.resolution()
    assert device.taps == [hud_tap("shop_close", width, height)]
    # The frame proving the farm is back is reused by find_column.
    assert actions._pending_png is not None
    shots = actions.timing.snapshot()["shot"]["n"]
    found = actions.find_column()
    assert found.success
    assert actions.timing.snapshot()["shot"]["n"] == shots


def test_close_shop_then_find_column(no_sleep):
    device = FakeDevice(
        [
            _png_bytes(_bgr(FARM_NO_COLUMN)),
            _png_bytes(_farm_with_stand()),
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
    monkeypatch.setattr(actions, "_next_listing", lambda _mode: ad)
    monkeypatch.setattr(actions, "visit_shop", lambda _ad: ActionResult(True, Action("VISIT_SHOP", "ad")))
    monkeypatch.setattr(actions, "buy_wishlist", lambda: ActionResult(True, Action("BUY", "egg")))
    monkeypatch.setattr(actions, "close_shop", lambda: ActionResult(True, Action("CLOSE_SHOP", "shop")))
    result = actions.shop_from_newspaper(limit=2, reset_home=False)
    assert result.success
    assert columns == [1, 1]
    assert homes == [1]


def test_one_shop_stays_inside_screenshot_budget(no_sleep, monkeypatch):
    """Guard rail: a screencap costs far more than a sleep, so a shop visit
    must not grow new screenshots. find_column → open → visit → close = 4."""
    monkeypatch.setattr("app.actions.shop.active_wishlist", lambda: {})
    ad = DetectedObject("newspaper", 600, 453, 340, 260, 1.0, "ad")
    device = FakeDevice(
        [
            _png_bytes(_farm_with_stand()),
            _png_bytes(_bgr(NEWSPAPER)),
            _png_bytes(_bgr(PLAYER_SHOP)),
            _png_bytes(_farm_with_stand()),
            _png_bytes(_bgr(NEWSPAPER)),
            _png_bytes(_farm_with_stand()),
        ]
    )
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    queue = [ad]
    monkeypatch.setattr(
        actions, "_next_listing", lambda _mode: queue.pop(0) if queue else None
    )
    actions.shop_from_newspaper(limit=1, reset_home=False, until_done=True)
    snap = actions.timing.snapshot()
    assert snap["shop_n"] == 1
    assert snap["steps"]["shop"]["shots_avg"] <= 4
    assert snap["steps"]["close_shop"]["shots_avg"] == 1


def test_reset_loop_state_clears_plan_and_visited(no_sleep):
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._planned_ads = [(2, 1), (6, 0)]
    actions._visited_ads.add((2, 1))
    actions._news_fp = object()
    actions._news_left_page = 8
    actions.reset_loop_state()
    assert actions._planned_ads == []
    assert actions._visited_ads == set()
    assert actions._news_fp is None
    assert actions._news_left_page == 2


def test_until_done_visits_all_shops_then_home_and_clears(no_sleep, monkeypatch):
    from app.actions.farming import Action, ActionResult

    wish = DetectedObject("newspaper", 230, 173, 340, 260, 1.0, "ad")
    coin = DetectedObject("newspaper", 995, 173, 340, 260, 1.0, "ad")
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._planned_ads = [(2, 1), (3, 0)]
    actions._visited_ads.add((2, 0))
    actions._news_fp = object()
    visited = _stub_news_loop(actions, monkeypatch)
    homes: list[int] = []
    monkeypatch.setattr(
        actions, "go_home", lambda: homes.append(1) or ActionResult(True, Action("GO_HOME", "house"))
    )
    queue = [wish, coin]
    monkeypatch.setattr(
        actions, "_next_listing", lambda _mode: queue.pop(0) if queue else None
    )
    result = actions.shop_from_newspaper(
        limit=1, mode="sweep", reset_home=False, until_done=True
    )
    assert result.success
    assert result.action.target == "done"
    assert result.error == "all shops done"
    assert [(ad.x, ad.y) for ad in visited] == [(wish.x, wish.y), (coin.x, coin.y)]
    assert homes == [1]
    assert actions._planned_ads == []
    assert actions._visited_ads == set()
    assert actions._news_fp is None


def _stub_news_loop(actions, monkeypatch):
    from app.actions.farming import Action, ActionResult

    visited: list[DetectedObject] = []
    monkeypatch.setattr(
        actions, "go_home", lambda: ActionResult(True, Action("GO_HOME", "house"))
    )
    monkeypatch.setattr(
        actions, "find_column", lambda: ActionResult(True, Action("FIND_COLUMN", "stand"))
    )
    monkeypatch.setattr(
        actions,
        "open_newspaper",
        lambda: ActionResult(True, Action("OPEN_NEWSPAPER", "stand")),
    )
    monkeypatch.setattr(
        actions, "close_shop", lambda: ActionResult(True, Action("CLOSE_SHOP", "shop"))
    )
    monkeypatch.setattr(
        actions,
        "close_newspaper",
        lambda png=None: ActionResult(True, Action("CLOSE_NEWSPAPER", "newspaper")),
    )
    monkeypatch.setattr(
        actions,
        "visit_shop",
        lambda ad: visited.append(ad) or ActionResult(True, Action("VISIT_SHOP", "ad")),
    )
    monkeypatch.setattr(
        actions,
        "buy_wishlist",
        lambda: ActionResult(False, Action("BUY", "none"), "no wishlist slot"),
    )
    return visited


def test_follow_does_not_visit_non_wishlist_coin_ad(no_sleep, monkeypatch):
    coin = DetectedObject("newspaper", 995, 173, 340, 260, 1.0, "ad")
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    visited = _stub_news_loop(actions, monkeypatch)
    monkeypatch.setattr(actions, "_next_listing", lambda mode: coin if mode == "sweep" else None)
    result = actions.shop_from_newspaper(limit=1, mode="follow", reset_home=False)
    assert not result.success
    assert result.error == "no newspaper ads"
    assert visited == []


def test_shop_alias_follows_wishlist_only(no_sleep, monkeypatch):
    coin = DetectedObject("newspaper", 995, 173, 340, 260, 1.0, "ad")
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    visited = _stub_news_loop(actions, monkeypatch)
    monkeypatch.setattr(actions, "_next_listing", lambda mode: coin if mode == "sweep" else None)
    result = actions.shop_from_newspaper(limit=1, mode="shop", reset_home=False)
    assert visited == []
    assert result.error == "no newspaper ads"


def test_sweep_visits_remaining_coin_ad_after_wishlist(no_sleep, monkeypatch):
    wish = DetectedObject("newspaper", 230, 173, 340, 260, 1.0, "ad")
    coin = DetectedObject("newspaper", 995, 173, 340, 260, 1.0, "ad")
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    visited = _stub_news_loop(actions, monkeypatch)
    queue = [wish, coin]
    monkeypatch.setattr(
        actions, "_next_listing", lambda _mode: queue.pop(0) if queue else None
    )
    result = actions.shop_from_newspaper(limit=1, mode="sweep", reset_home=False)
    assert [(ad.x, ad.y) for ad in visited] == [(wish.x, wish.y), (coin.x, coin.y)]
    assert result.error == "no newspaper ads"


def test_follow_plan_skips_coin_only_cells(no_sleep, monkeypatch):
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._news_left_page = NEWS_PAGE_COUNT
    slots = {
        (page, slot): (x, y, w, h)
        for page, slot, x, y, w, h in news_spread_slots(1920, 1080, NEWS_PAGE_COUNT)
    }
    wish = DetectedObject("newspaper", *slots[(10, 1)], 1.0, "ad")
    coin = DetectedObject("newspaper", *slots[(10, 2)], 1.0, "ad")
    monkeypatch.setattr(actions.news, "find_ads", lambda png, left_page=10: [wish, coin])
    monkeypatch.setattr(
        actions.news,
        "find_wishlist_ads",
        lambda png, wishlist, left_page=10, ads=None: [(wish, "egg")],
    )
    blank = np.zeros((1080, 1920, 3), dtype=np.uint8)
    actions._rescan_plan("follow", first_png=blank)
    assert actions._planned_ads == [(10, 1)]


def test_sweep_plan_appends_remaining_coin_cells(no_sleep, monkeypatch):
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._news_left_page = NEWS_PAGE_COUNT
    slots = {
        (page, slot): (x, y, w, h)
        for page, slot, x, y, w, h in news_spread_slots(1920, 1080, NEWS_PAGE_COUNT)
    }
    wish = DetectedObject("newspaper", *slots[(10, 1)], 1.0, "ad")
    coin = DetectedObject("newspaper", *slots[(10, 2)], 1.0, "ad")
    monkeypatch.setattr(actions.news, "find_ads", lambda png, left_page=10: [wish, coin])
    monkeypatch.setattr(
        actions.news,
        "find_wishlist_ads",
        lambda png, wishlist, left_page=10, ads=None: [(wish, "egg")],
    )
    blank = np.zeros((1080, 1920, 3), dtype=np.uint8)
    actions._rescan_plan("sweep", first_png=blank)
    assert actions._planned_ads == [(10, 1), (10, 2)]


def test_unchanged_newspaper_seeks_next_cell_without_rescanning(no_sleep, monkeypatch):
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._planned_ads = [(2, 1), (6, 0)]
    actions._visited_ads.add((2, 1))
    actions._news_left_page = 2
    rescans: list[int] = []
    turns: list[int] = []
    monkeypatch.setattr(
        actions, "_rescan_plan", lambda mode, first_png=None: rescans.append(1)
    )
    monkeypatch.setattr(
        actions,
        "_turn_newspaper",
        lambda settle=True: turns.append(actions._news_left_page)
        or setattr(
            actions,
            "_news_left_page",
            min(NEWS_PAGE_COUNT, actions._news_left_page + 2),
        ),
    )
    ad = actions._next_listing("follow")
    assert rescans == []
    assert turns == [2, 4]
    assert actions._ad_cell(ad) == (6, 0)


def test_remaining_plan_does_not_rescan_when_first_spread_looks_different(
    no_sleep, monkeypatch
):
    from app.actions.farming import Action, ActionResult

    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._planned_ads = [(2, 1), (2, 2)]
    actions._visited_ads.add((2, 1))
    actions._news_left_page = 2
    monkeypatch.setattr(
        actions, "close_shop", lambda: ActionResult(True, Action("CLOSE_SHOP", "shop"))
    )
    rescans: list[int] = []
    monkeypatch.setattr(
        actions, "_rescan_plan", lambda mode, first_png=None: rescans.append(1)
    )
    ad = actions._next_listing("sweep")
    assert rescans == []
    assert actions._ad_cell(ad) == (2, 2)


def test_sweep_rescans_leftover_bright_page_2_ads(no_sleep, monkeypatch):
    """Page 3 greyed out while page 2 is still white: same paper, missed shops."""
    from app.actions.farming import Action, ActionResult

    device = FakeDevice([_png_bytes(_bgr(NEWSPAPER_VISITED))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._planned_ads = [(3, 0), (3, 2)]
    actions._visited_ads = {(3, 0), (3, 2)}
    actions._news_left_page = 2
    monkeypatch.setattr(
        actions, "close_shop", lambda: ActionResult(True, Action("CLOSE_SHOP", "shop"))
    )
    monkeypatch.setattr(
        actions, "open_newspaper", lambda: ActionResult(True, Action("OPEN_NEWSPAPER", "stand"))
    )
    rescans: list[int] = []

    def fake_rescan(mode, first_png=None):
        rescans.append(1)
        actions._planned_ads = [(2, 5)]
        actions._visited_ads.clear()

    monkeypatch.setattr(actions, "_rescan_plan", fake_rescan)
    monkeypatch.setattr(
        actions,
        "_seek_next_planned",
        lambda: DetectedObject("newspaper", 600, 733, 340, 260, 1.0, "ad"),
    )
    ad = actions._next_listing("sweep")
    assert rescans == [1]
    assert ad is not None


def test_exhausted_plan_skips_rescan_when_visited_still_grey(no_sleep, monkeypatch):
    device = FakeDevice([_png_bytes(np.zeros((1080, 1920, 3), dtype=np.uint8))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._planned_ads = [(2, 1)]
    actions._visited_ads.add((2, 1))
    actions._news_left_page = 2
    monkeypatch.setattr(actions, "_visited_still_dim", lambda png: True)
    rescans: list[int] = []
    monkeypatch.setattr(
        actions, "_rescan_plan", lambda mode, first_png=None: rescans.append(1)
    )
    ad = actions._next_listing("sweep")
    assert ad is None
    assert rescans == []


def test_exhausted_plan_rescans_when_visited_listing_is_bright(no_sleep, monkeypatch):
    from app.actions.farming import Action, ActionResult

    device = FakeDevice([_png_bytes(np.zeros((1080, 1920, 3), dtype=np.uint8))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._planned_ads = [(2, 1)]
    actions._visited_ads.add((2, 1))
    actions._news_left_page = 2
    monkeypatch.setattr(actions, "_visited_still_dim", lambda png: False)
    monkeypatch.setattr(
        actions, "close_shop", lambda: ActionResult(True, Action("CLOSE_SHOP", "shop"))
    )
    monkeypatch.setattr(
        actions, "open_newspaper", lambda: ActionResult(True, Action("OPEN_NEWSPAPER", "stand"))
    )
    rescans: list[int] = []

    def fake_rescan(mode, first_png=None):
        rescans.append(1)
        actions._planned_ads = [(2, 2)]
        actions._visited_ads.clear()

    monkeypatch.setattr(actions, "_rescan_plan", fake_rescan)
    monkeypatch.setattr(
        actions,
        "_seek_next_planned",
        lambda: DetectedObject("newspaper", 230, 453, 340, 260, 1.0, "ad"),
    )
    ad = actions._next_listing("follow")
    assert rescans == [1]
    assert ad is not None


def test_first_open_scans_all_pages_once(no_sleep, monkeypatch):
    from app.actions.farming import Action, ActionResult

    blank = _png_bytes(np.zeros((1080, 1920, 3), dtype=np.uint8))
    device = FakeDevice([blank])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    turns: list[int] = []
    monkeypatch.setattr(actions.news, "find_ads", lambda png, left_page=2: [])
    monkeypatch.setattr(
        actions.news,
        "find_wishlist_ads",
        lambda png, wishlist, left_page=2, ads=None: [],
    )
    monkeypatch.setattr(
        actions, "close_shop", lambda: ActionResult(True, Action("CLOSE_SHOP", "shop"))
    )
    monkeypatch.setattr(
        actions,
        "_turn_newspaper",
        lambda settle=True: turns.append(actions._news_left_page)
        or setattr(
            actions,
            "_news_left_page",
            min(NEWS_PAGE_COUNT, actions._news_left_page + 2),
        ),
    )
    ad = actions._next_listing("follow")
    assert ad is None
    assert turns == [2, 4, 6, 8]
    assert actions._planned_ads == []
    assert actions._news_fp is not None


def test_newspaper_fingerprints_differ_on_listing_paint():
    from app.vision.newspaper import newspaper_fingerprints_differ

    base = np.zeros((1080, 1920, 3), dtype=np.uint8)
    other = base.copy()
    other[200:400, 250:500] = (0, 180, 40)
    assert newspaper_fingerprints_differ(base, base) is False
    assert newspaper_fingerprints_differ(base, other) is True


def test_fresh_newspaper_listings_are_not_dim():
    from app.vision.newspaper import ad_cell_is_dim
    from app.vision.regions import NEWS_PROMO_SLOTS, news_spread_slots

    image = _bgr(NEWSPAPER)
    dim = []
    for page, slot, x, y, w, h in news_spread_slots(1920, 1080, 2):
        if (page, slot) in NEWS_PROMO_SLOTS:
            continue
        if ad_cell_is_dim(image[y : y + h, x : x + w]):
            dim.append((page, slot))
    assert dim == []


def test_visited_newspaper_listings_are_dim():
    """A shop already opened on this paper goes grey. Fresh cards stay white."""
    from app.vision.newspaper import ad_cell_is_dim

    image = _bgr(NEWSPAPER_VISITED)
    dim, bright = [], []
    for page, slot, x, y, w, h in news_spread_slots(1920, 1080, 2):
        if (page, slot) in {(2, 0)}:
            continue
        cell = (page, slot)
        if ad_cell_is_dim(image[y : y + h, x : x + w]):
            dim.append(cell)
        else:
            bright.append(cell)
    assert (2, 1) in dim
    assert (3, 0) in dim
    assert (2, 5) in bright
    ads = NewspaperDetector().find_ads(image)
    cells = set()
    for ad in ads:
        cx = ad.x + ad.width // 2
        cy = ad.y + ad.height // 2
        for page, slot, x, y, w, h in news_spread_slots(1920, 1080, 2):
            if x <= cx < x + w and y <= cy < y + h:
                cells.add((page, slot))
    assert (2, 1) not in cells
    assert (2, 5) in cells


def test_visited_still_dim_follows_the_grey_cards(no_sleep):
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._visited_ads = {(2, 1), (3, 0)}
    actions._news_left_page = 2
    assert actions._visited_still_dim(_bgr(NEWSPAPER_VISITED)) is True
    assert actions._visited_still_dim(_bgr(NEWSPAPER)) is False


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
    farm = _farm_with_stand()
    stand = NewspaperDetector().find_stand(farm)
    assert stand is not None
    stand_tap = (stand.x + stand.width // 2, stand.y + stand.height // 2)
    dest = tmp_path / "browse_tpl"
    dest.mkdir()
    names = (
        "newspaper_ad",
        "newspaper_close",
        "newspaper_stand",
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
            _png_bytes(farm),
            _png_bytes(_bgr(NEWSPAPER)),
            _png_bytes(_bgr(NEWSPAPER)),
            # The paper's X was tapped: the farm is back.
            _png_bytes(farm),
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


def test_failure_frame_is_kept_and_pruned(tmp_path, no_sleep, monkeypatch):
    import app.actions.shop as shop_mod

    monkeypatch.setattr(shop_mod, "SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(shop_mod, "FAILURE_FRAMES_KEPT", 2)
    folder = tmp_path / "debug"
    folder.mkdir()
    for name in ("open_fail_20000101_000000.png", "open_fail_20000101_000001.png"):
        (folder / name).write_bytes(b"old")
    actions = NewspaperActions(FakeDevice([]), AppConfig(debug=False), wait_s=0)
    actions._keep_failure_frame(np.zeros((6, 8, 3), dtype=np.uint8), "open_fail")
    kept = sorted(p.name for p in folder.glob("open_fail_*.png"))
    assert len(kept) == 2
    assert kept[0] == "open_fail_20000101_000001.png"
    assert (folder / kept[1]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_seeking_a_far_page_swipes_back_to_back_and_waits_once(no_sleep, monkeypatch):
    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0.9, visit_wait_s=0)
    actions._planned_ads = [(10, 2)]
    actions._news_left_page = 2
    events: list[str] = []
    monkeypatch.setattr(actions, "_swipe_next_pages", lambda: events.append("swipe"))
    monkeypatch.setattr(actions, "_sleep", lambda s: events.append(f"sleep{s:g}"))
    ad = actions._seek_next_planned()
    assert events == ["swipe"] * 4 + ["sleep0.9"]
    assert actions._news_left_page == NEWS_PAGE_COUNT
    assert actions._ad_cell(ad) == (10, 2)


def test_close_newspaper_taps_the_papers_own_x(no_sleep):
    """The paper's X is not where the stall's is; the stall's point misses it."""
    from app.vision.regions import hud_tap

    device = FakeDevice([_png_bytes(_bgr(NEWSPAPER)), _png_bytes(_bgr(FARM))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.close_newspaper()
    assert result.success
    width, height = device.resolution()
    assert device.taps == [hud_tap("newspaper_close", width, height)]
    assert hud_tap("shop_close", width, height) not in device.taps
    assert device.backs == 0


def test_close_newspaper_never_taps_blind_on_the_farm(no_sleep):
    """That spot on the farm sits by the coin and diamond counters."""
    device = FakeDevice([_png_bytes(_bgr(FARM))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    assert actions.close_newspaper().success
    assert device.taps == []


def test_close_newspaper_reports_a_paper_that_stays_open(no_sleep):
    device = FakeDevice([_png_bytes(_bgr(NEWSPAPER))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.close_newspaper()
    assert not result.success
    assert result.error == "newspaper still open"
    assert device.backs == 0


def test_go_home_closes_an_open_paper_first(no_sleep):
    from app.vision.regions import hud_tap

    device = FakeDevice(
        [
            _png_bytes(_bgr(NEWSPAPER)),
            _png_bytes(_bgr(FARM)),
            _png_bytes(_bgr(FARM)),
            _png_bytes(_friend_farm()),
            _png_bytes(_bgr(FARM)),
        ]
    )
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.go_home()
    assert result.success
    width, height = device.resolution()
    assert device.taps[0] == hud_tap("newspaper_close", width, height)
    assert device.taps[1] == hud_tap("hud_friends", width, height)


def test_go_home_fails_when_the_farm_never_draws(no_sleep, monkeypatch):
    monkeypatch.setattr("app.actions.shop.GO_HOME_LOAD_S", 0)
    device = FakeDevice([_png_bytes(_bgr(FARM)), _png_bytes(_bgr(FIXTURES / "unknown.png"))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.go_home()
    assert not result.success
    assert result.error == "farm not drawn"


def test_popup_without_x_is_left_alone_not_backed_out_of(no_sleep):
    """Back opens the game's exit dialog instead of closing anything."""
    from app.vision.screen import GameScreen, ScreenDetection

    device = FakeDevice([_png_bytes(_bgr(FARM))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    actions._close_popup(ScreenDetection(GameScreen.POPUP, 0.9, []))
    assert device.backs == 0
    assert device.taps == []


def test_finished_paper_is_closed_before_going_home(no_sleep, monkeypatch):
    from app.actions.farming import Action, ActionResult

    device = FakeDevice([])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    _stub_news_loop(actions, monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(
        actions,
        "close_newspaper",
        lambda png=None: order.append("close_paper")
        or ActionResult(True, Action("CLOSE_NEWSPAPER", "newspaper")),
    )
    monkeypatch.setattr(
        actions,
        "go_home",
        lambda: order.append("home") or ActionResult(True, Action("GO_HOME", "house")),
    )
    monkeypatch.setattr(actions, "_next_listing", lambda _mode: None)
    result = actions.shop_from_newspaper(
        limit=1, mode="sweep", reset_home=False, until_done=True
    )
    assert result.error == "all shops done"
    assert order == ["close_paper", "home"]


def test_a_stall_is_never_taken_for_the_paper():
    """The stall's X is the same art ~85px from the paper's. Mistaking one for
    the other tapped the awning all night and the bot never got home."""
    device = FakeDevice([_png_bytes(_bgr(FARM))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    for stall in (PLAYER_SHOP, STALL_FITS, STALL_CLIPPED):
        assert not actions._paper_x_visible(_bgr(stall))
    assert actions._paper_x_visible(_bgr(NEWSPAPER))


def test_go_home_closes_an_open_stall_with_the_stalls_x(no_sleep):
    from app.vision.regions import hud_tap

    device = FakeDevice(
        [
            _png_bytes(_bgr(PLAYER_SHOP)),
            _png_bytes(_bgr(FARM)),
            _png_bytes(_bgr(FARM)),
            _png_bytes(_friend_farm()),
            _png_bytes(_bgr(FARM)),
        ]
    )
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.go_home()
    assert result.success
    width, height = device.resolution()
    assert device.taps[0] == hud_tap("shop_close", width, height)
    assert hud_tap("newspaper_close", width, height) not in device.taps


def test_go_home_waits_for_a_closing_stall_to_go(no_sleep):
    """The stall still shows for a frame or two after its X is tapped; reading
    that as "still open" gave up on a stall that was closing fine."""
    from app.vision.regions import hud_tap

    device = FakeDevice(
        [
            _png_bytes(_bgr(FARM)),
            _png_bytes(_bgr(PLAYER_SHOP)),  # friend's stall on arrival
            _png_bytes(_bgr(PLAYER_SHOP)),  # still drawing after the tap
            _png_bytes(_bgr(PLAYER_SHOP)),
            _png_bytes(_friend_farm()),
            _png_bytes(_bgr(FARM)),
        ]
    )
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=5)
    result = actions.go_home()
    assert result.success
    width, height = device.resolution()
    assert device.taps.count(hud_tap("shop_close", width, height)) == 1
    assert device.taps[-1] == hud_tap("shop_home", width, height)


def test_go_home_from_another_farm_skips_the_friend_visit(no_sleep):
    """On someone else's farm the house button is one tap away; visiting a
    friend first only adds a slow load."""
    from app.vision.regions import hud_tap

    device = FakeDevice(
        [_png_bytes(_friend_farm()), _png_bytes(_friend_farm()), _png_bytes(_bgr(FARM))]
    )
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    result = actions.go_home()
    assert result.success
    width, height = device.resolution()
    assert device.taps == [hud_tap("shop_home", width, height)]


def test_visit_keeps_waiting_while_the_farm_loads(no_sleep):
    """A slow network keeps the loading screen up past visit_wait_s; the stall
    that follows is still the visit's, not a failure."""
    loading = _png_bytes(_bgr(FIXTURES / "unknown.png"))
    device = FakeDevice([loading, loading, loading, _png_bytes(_bgr(PLAYER_SHOP))])
    actions = NewspaperActions(device, AppConfig(debug=False), wait_s=0, visit_wait_s=0)
    ad = DetectedObject("newspaper", 230, 173, 340, 260, 1.0, "ad")
    assert actions.visit_shop(ad).success
