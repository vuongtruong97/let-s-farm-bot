from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.config import AppConfig, save_config
from app.main import main
from app.storage import botdata
from app.web.server import BotWebHandler


def _png_b64(size: tuple[int, int] = (32, 32), color=(40, 180, 70, 255)) -> str:
    image = Image.new("RGBA", size, color)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _png_bytes(size: tuple[int, int] = (24, 20)) -> bytes:
    image = Image.new("RGBA", size, (40, 180, 70, 255))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("app.config.CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "templates").mkdir()
    save_config(AppConfig(debug=True, adb_port=5555), tmp_path / "config.json")
    botdata.save_wishlist({"wheat": {"template": "item_wheat"}})
    botdata.save_crops({"wheat": {"storage": "silo", "seed_template": "seed_wheat"}})
    return tmp_path


def test_upsert_item_writes_png_and_wishlist(data_home: Path):
    row = botdata.upsert_item("wheat", image_b64=_png_b64((24, 20)), enabled=True)
    assert row["has_image"] is True
    assert row["width"] == 24
    assert (data_home / "templates" / "item_wheat.png").is_file()
    loaded = botdata.load_wishlist()
    assert loaded["wheat"]["enabled"] is True
    assert "wheat" in botdata.active_wishlist()
    botdata.upsert_item("wheat", enabled=False)
    assert "wheat" not in botdata.active_wishlist()
    assert (data_home / "library" / "shop_01.png").is_file()


def test_record_purchase_newest_first(data_home: Path):
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=7))
    botdata.upsert_item("egg", enabled=True)
    botdata.upsert_item("screw", enabled=True)
    first = botdata.record_purchase("egg", when=datetime(2026, 9, 14, 10, 0, 0, tzinfo=tz))
    second = botdata.record_purchase("screw", when=datetime(2026, 9, 14, 11, 30, 5, tzinfo=tz))
    assert first["at"].startswith("2026-09-14T10:00:00")
    rows = botdata.list_purchases()
    assert [row["item"] for row in rows] == ["screw", "egg"]
    assert rows[0]["at"] == second["at"]
    assert rows[0]["kind"] == "match"
    status = {row["id"]: row for row in botdata.wishlist_buy_status()}
    assert status["egg"]["match_count"] == 1
    assert status["egg"]["buy_count"] == 0
    assert status["egg"]["last_match_at"] == first["at"]
    assert status["egg"]["last_buy_at"] is None
    assert status["screw"]["last_match_at"] == second["at"]
    assert status["wheat"]["match_count"] == 0
    assert status["wheat"]["buy_count"] == 0
    botdata.record_purchase("egg", when=datetime(2026, 9, 14, 12, 0, 0, tzinfo=tz), kind="buy")
    status = {row["id"]: row for row in botdata.wishlist_buy_status()}
    assert status["egg"]["match_count"] == 1
    assert status["egg"]["buy_count"] == 1
    assert status["egg"]["buys"][0]["at"].startswith("2026-09-14T12:00:00")
    botdata.clear_purchases()
    assert botdata.list_purchases() == []
    cleared = {row["id"]: row for row in botdata.wishlist_buy_status()}
    assert cleared["egg"]["match_count"] == 0
    assert cleared["egg"]["buy_count"] == 0
    assert cleared["egg"]["last_buy_at"] is None


def test_record_purchase_buy_saves_proof_png(data_home: Path):
    png = _png_bytes()
    row = botdata.record_purchase("wheat", kind="buy", image=png, qty=8)
    assert row["kind"] == "buy"
    assert row["image"] == "buy_01"
    assert row["qty"] == 8
    path = data_home / "buy_proofs" / "buy_01.png"
    assert path.is_file()
    status = {entry["id"]: entry for entry in botdata.wishlist_buy_status()}
    assert status["wheat"]["buys"][0]["image"] == "buy_01"
    assert status["wheat"]["buys"][0]["qty"] == 8
    assert status["wheat"]["qty_sum"] == 8
    botdata.record_purchase("wheat", kind="match")
    fail_buy = botdata.record_purchase("wheat", kind="buy")
    assert "image" not in fail_buy
    botdata.clear_purchases()
    assert not path.is_file()
    assert not (data_home / "buy_proofs").exists()


def test_legacy_purchase_without_kind_counts_as_match(data_home: Path):
    botdata.upsert_item("wheat", enabled=True)
    botdata.save_json(
        botdata.purchases_path(),
        {"purchases": [{"item": "wheat", "at": "2026-09-14T10:00:00+07:00"}]},
    )
    status = {row["id"]: row for row in botdata.wishlist_buy_status()}
    assert status["wheat"]["match_count"] == 1
    assert status["wheat"]["buy_count"] == 0
    assert status["wheat"]["matches"][0]["at"].startswith("2026-09-14T10:00:00")


def test_delete_purchases_api(httpd: str, data_home: Path):
    botdata.record_purchase("wheat", kind="buy", image=_png_bytes())
    assert (data_home / "buy_proofs" / "buy_01.png").is_file()
    code, body = _request(f"{httpd}/api/purchases", "DELETE")
    assert code == 200
    assert body["purchases"] == []
    wheat = next(row for row in body["wishlist_buys"] if row["id"] == "wheat")
    assert wheat["match_count"] == 0
    assert wheat["buy_count"] == 0
    assert wheat["last_buy_at"] is None
    assert not (data_home / "buy_proofs" / "buy_01.png").is_file()


def test_get_buy_proof_png(httpd: str, data_home: Path):
    botdata.record_purchase("wheat", kind="buy", image=_png_bytes())
    code, png = _request(f"{httpd}/api/buy-proofs/buy_01.png")
    assert code == 200
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    code, err = _request(f"{httpd}/api/buy-proofs/not_valid.png")
    assert code == 400


def test_update_item_renames_id_and_png(data_home: Path):
    botdata.upsert_item("wheat", image_b64=_png_b64((24, 20)), enabled=False)
    botdata.update_item("wheat", news_image_b64=_png_b64((16, 16), (90, 90, 90, 255)))
    row = botdata.update_item("wheat", new_id="bread")
    assert row["id"] == "bread"
    assert row["enabled"] is False
    assert (data_home / "templates" / "item_bread.png").is_file()
    assert (data_home / "templates" / "item_bread_news.png").is_file()
    assert not (data_home / "templates" / "item_wheat.png").is_file()
    assert not (data_home / "templates" / "item_wheat_news.png").is_file()
    wishlist = botdata.load_wishlist()
    assert "wheat" not in wishlist
    assert wishlist["bread"]["template"] == "item_bread"
    assert wishlist["bread"]["news_template"] == "item_bread_news"
    assert row["has_news_image"] is True


def test_update_item_rejects_duplicate_id(data_home: Path):
    botdata.upsert_item("wheat", image_b64=_png_b64())
    botdata.upsert_item("bread", image_b64=_png_b64())
    with pytest.raises(ValueError, match="already exists"):
        botdata.update_item("wheat", new_id="bread")
    assert "wheat" in botdata.load_wishlist()
    assert (data_home / "templates" / "item_wheat.png").is_file()


def test_save_item_png_skips_existing(data_home: Path):
    first = botdata.save_item_png("milk", _png_bytes())
    path = data_home / "templates" / "item_milk.png"
    original = path.read_bytes()
    botdata.save_item_png("milk", _png_bytes((16, 16)), overwrite=False)
    assert path.read_bytes() == original
    assert first["id"] == "milk"
    assert "milk" in botdata.active_wishlist()


def test_save_library_png_skips_similar(data_home: Path):
    first = botdata.save_library_png("shop", _png_bytes((24, 20)))
    assert first["id"] == "shop_01"
    assert first["duplicate"] is False
    again = botdata.save_library_png("shop", _png_bytes((24, 20)))
    assert again["id"] == "shop_01"
    assert again["duplicate"] is True
    news = botdata.save_library_png("news", _png_bytes((16, 16)))
    assert news["id"] == "news_01"
    assert [row["id"] for row in botdata.list_library()] == ["news_01", "shop_01"]


def test_cannot_overwrite_system_template(data_home: Path):
    hud = data_home / "templates" / "hud_shop.png"
    hud.write_bytes(b"x")
    with pytest.raises(ValueError, match="system template"):
        botdata.write_template("hud_shop", b"not-png")


def test_cli_web_starts_server(monkeypatch, data_home: Path):
    called = {}

    def fake_serve(host, port):
        called["host"] = host
        called["port"] = port

    monkeypatch.setattr("app.web.server.serve", fake_serve)
    assert main(["web", "--port", "48722"]) == 0
    assert called == {"host": "0.0.0.0", "port": 48722}
    assert main(["web", "--host", "127.0.0.1", "--port", "48722"]) == 0
    assert called == {"host": "127.0.0.1", "port": 48722}


def test_listen_urls_all_interfaces_includes_localhost():
    from app.web.server import listen_urls

    urls = listen_urls("0.0.0.0", 48721)
    assert urls[0] == "http://127.0.0.1:48721"
    assert listen_urls("127.0.0.1", 48721) == ["http://127.0.0.1:48721"]


@pytest.fixture
def httpd(data_home: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), BotWebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_share_api_local_only_when_bound_to_loopback(httpd: str):
    code, info = _request(f"{httpd}/api/share")
    assert code == 200
    assert info["local_only"] is True
    assert info["urls"] == []


def test_share_api_lists_lan_urls_when_bound_to_all(monkeypatch, data_home: Path):
    monkeypatch.setattr("app.web.server._lan_ipv4", lambda: ["192.168.1.20"])
    server = ThreadingHTTPServer(("0.0.0.0", 0), BotWebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        code, info = _request(f"http://127.0.0.1:{port}/api/share")
    finally:
        server.shutdown()
        server.server_close()
    assert code == 200
    assert info["local_only"] is False
    assert info["urls"] == [f"http://192.168.1.20:{port}"]


def test_theme_stylesheet_and_selector_are_served(httpd: str):
    code, css = _request(f"{httpd}/static/themes.css")
    assert code == 200
    assert b"data-theme='pastel'" in css
    code, page = _request(f"{httpd}/")
    assert code == 200
    assert b'id="theme-select"' in page
    assert b"/static/themes.css" in page


def test_screen_toggle_and_log_controls_are_on_the_page(httpd: str):
    code, page = _request(f"{httpd}/")
    assert code == 200
    for marker in (b'id="btn-screen-toggle"', b'id="btn-log-expand"', b'id="log-details"'):
        assert marker in page
    # The shot button sits in its own row below the picture, not inside it.
    assert page.index(b'id="viewport"') < page.index(b'class="viewport-actions"') < page.index(b'id="btn-shot"')


def test_control_page_folds_options_and_one_off_actions(httpd: str):
    code, page = _request(f"{httpd}/")
    assert code == 200
    for marker in (b'id="prefs-details"', b'id="once-details"', b'id="prefs-summary"'):
        assert marker in page
    # Everyday controls stay outside the folds; one-off actions live inside.
    once = page.index(b'id="once-details"')
    assert page.index(b'id="btn-start"') < page.index(b'id="prefs-details"') < once
    assert page.index(b'id="btn-stop"') < page.index(b'id="prefs-details"')
    assert page.index(b'data-run="restart_game"') > once
    assert b'data-confirm="' in page[once:]


def test_buy_history_is_a_foldable_section(httpd: str):
    code, page = _request(f"{httpd}/")
    assert code == 200
    fold = page.index(b'id="buy-details"')
    # The one-line summary sits in the <summary>, so it shows while folded.
    assert fold < page.index(b'id="buy-summary"') < page.index(b"</summary>", fold)
    assert page.index(b"</summary>", fold) < page.index(b'id="btn-buy-reset"') < page.index(b'id="buy-log"')


def _request(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict | bytes]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            body = res.read()
            ctype = res.headers.get_content_type()
            if ctype == "application/json":
                return res.status, json.loads(body.decode("utf-8"))
            return res.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return exc.code, body


def test_web_state_and_item_upload(httpd: str, data_home: Path):
    code, state = _request(f"{httpd}/api/state")
    assert code == 200
    assert state["config"]["allow_diamond_spending"] is False
    assert state["wishlist"][0]["id"] == "wheat"
    assert state["wishlist"][0]["has_image"] is False
    assert state["purchases"] == []
    assert state["wishlist_buys"][0]["id"] == "wheat"
    assert state["wishlist_buys"][0]["match_count"] == 0
    assert state["wishlist_buys"][0]["buy_count"] == 0
    assert state["wishlist_buys"][0]["qty_sum"] == 0
    assert state["wishlist_buys"][0]["last_buy_at"] is None

    code, page = _request(f"{httpd}/")
    assert code == 200
    assert b"Wishlist" in page
    assert b"item-modal" in page
    assert b"library-picker" in page
    assert b'data-nav="develop"' in page
    assert b'data-nav="library"' in page

    code, wishlist_page = _request(f"{httpd}/wishlist")
    assert code == 200
    assert b"Wishlist" in wishlist_page
    code, library_page = _request(f"{httpd}/library")
    assert code == 200
    assert b'data-page="library"' in library_page
    code, dev_page = _request(f"{httpd}/dev")
    assert code == 200
    assert b'data-page="develop"' in dev_page
    code, missing = _request(f"{httpd}/not-a-page")
    assert code == 404
    assert b"find_column" in page
    assert b"scan_ads" in page
    assert b"go_home" in page
    assert b"buy-log" in page
    assert "Lịch sử mua".encode("utf-8") in page
    assert b"btn-buy-reset" in page
    assert b"buy-history-modal" in page
    assert b"action_wait_s" in page
    assert b"buy_wait_s" in page
    assert b"visit_wait_s" in page
    assert b"poll_interval_s" in page
    assert b"stall_swipe_ms" in page
    assert b"timing-log" in page
    assert b"timing-safety" in page
    assert "Đo lường".encode("utf-8") in page
    assert b"viewport-fit=cover" in page
    code, css = _request(f"{httpd}/static/style.css")
    assert code == 200
    assert b"@media (max-width: 900px)" in css
    assert b"@media (max-width: 600px)" in css
    code, js = _request(f"{httpd}/static/app.js")
    assert code == 200
    assert b"/api/buy-proofs/" in js
    assert b"buy-proof" in js
    assert b"qty_sum" in js
    assert "Tổng".encode("utf-8") in js
    assert b"renderTiming" in js
    assert b"deadline_hits" in js
    assert b"timing-split" in css

    code, saved = _request(
        f"{httpd}/api/items",
        "POST",
        {"id": "wheat", "image": _png_b64(), "enabled": True},
    )
    assert code == 200
    assert saved["item"]["has_image"] is True
    assert (data_home / "templates" / "item_wheat.png").is_file()
    assert saved["library"][0]["id"] == "shop_01"

    code, _ = _request(f"{httpd}/api/items/wheat", "PATCH", {"enabled": False})
    assert code == 200
    assert "wheat" not in botdata.active_wishlist()

    code, renamed = _request(
        f"{httpd}/api/items/wheat",
        "PATCH",
        {"id": "bread", "image": _png_b64((16, 16))},
    )
    assert code == 200
    assert renamed["item"]["id"] == "bread"
    assert renamed["item"]["width"] == 16
    assert renamed["item"]["enabled"] is False
    assert (data_home / "templates" / "item_bread.png").is_file()
    assert not (data_home / "templates" / "item_wheat.png").is_file()
    assert "wheat" not in botdata.load_wishlist()

    code, cfg = _request(
        f"{httpd}/api/config",
        "PUT",
        {
            "adb_host": "127.0.0.1",
            "adb_port": 5625,
            "debug": False,
            "allow_diamond_spending": True,
            "swipe_duration_ms": 300,
            "package": "com.supercell.hayday",
            "template_threshold": 0.81,
            "buy_threshold": 0.65,
            "news_threshold": 0.78,
            "loop_rest_min": 5,
            "action_wait_s": 1.2,
            "buy_wait_s": 2.5,
            "visit_wait_s": 1.9,
            "poll_interval_s": 0.25,
            "stall_swipe_ms": 240,
        },
    )
    assert code == 200
    assert cfg["config"]["allow_diamond_spending"] is False
    assert cfg["config"]["adb_port"] == 5625
    assert cfg["config"]["buy_threshold"] == 0.65
    assert cfg["config"]["news_threshold"] == 0.78
    assert cfg["config"]["loop_rest_min"] == 5.0
    assert cfg["config"]["action_wait_s"] == 1.2
    assert cfg["config"]["buy_wait_s"] == 2.5
    assert cfg["config"]["visit_wait_s"] == 1.9
    assert cfg["config"]["poll_interval_s"] == 0.25
    assert cfg["config"]["stall_swipe_ms"] == 240

    code, err = _request(
        f"{httpd}/api/items",
        "POST",
        {"id": "../hack", "image": _png_b64()},
    )
    assert code == 400
    assert "id" in err["error"]


def test_library_upload_and_assign(httpd: str, data_home: Path):
    code, uploaded = _request(
        f"{httpd}/api/library",
        "POST",
        {"kind": "shop", "image": _png_b64((20, 18), (200, 40, 40, 255))},
    )
    assert code == 200
    lib_id = uploaded["saved"]["id"]
    assert lib_id == "shop_01"
    assert uploaded["saved"]["duplicate"] is False
    code, png = _request(f"{httpd}/api/library/{lib_id}.png")
    assert code == 200
    assert png[:8] == b"\x89PNG\r\n\x1a\n"

    code, news = _request(
        f"{httpd}/api/library",
        "POST",
        {"kind": "news", "image": _png_b64((16, 16), (90, 90, 90, 255))},
    )
    assert code == 200
    news_id = news["saved"]["id"]
    assert news_id == "news_01"

    code, saved = _request(
        f"{httpd}/api/items",
        "POST",
        {"id": "apple", "shop_library": lib_id, "enabled": True},
    )
    assert code == 200
    assert saved["item"]["has_image"] is True
    assert saved["item"]["has_news_image"] is False
    assert (data_home / "templates" / "item_apple.png").is_file()

    code, patched = _request(
        f"{httpd}/api/items/apple",
        "PATCH",
        {"news_library": news_id},
    )
    assert code == 200
    assert patched["item"]["has_news_image"] is True
    assert (data_home / "templates" / "item_apple_news.png").is_file()

    code, _ = _request(f"{httpd}/api/library/{lib_id}", "DELETE")
    assert code == 200
    assert not (data_home / "library" / "shop_01.png").is_file()
    assert (data_home / "templates" / "item_apple.png").is_file()

    code, missing = _request(
        f"{httpd}/api/items",
        "POST",
        {"id": "pear", "shop_library": "shop_01"},
    )
    assert code == 400


def _png_bytes(size: tuple[int, int] = (32, 32)) -> bytes:
    image = Image.new("RGB", size, (20, 180, 90))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _wait_idle(runtime, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = runtime.snapshot()
        if snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError(runtime.snapshot())


class _FakeCtl:
    def __init__(self, cfg=None):
        self.serial = ""
        self.backs = 0
        self.homes = 0
        self.taps: list = []
        self.swipes: list = []

    def connect(self, host=None, port=None):
        self.serial = "127.0.0.1:5555"
        return self.serial

    def screenshot(self) -> bytes:
        return _png_bytes()

    def tap(self, x: int, y: int) -> None:
        self.taps.append((int(x), int(y)))

    def back(self) -> None:
        self.backs += 1

    def home(self) -> None:
        self.homes += 1

    def swipe(self, *args, **kwargs) -> None:
        self.swipes.append(args)

    def resolution(self) -> tuple[int, int]:
        return 1920, 1080


class _StubFarming:
    def __init__(self, device, config=None, **kwargs):
        self.device = device

    def harvest_ready_fields(self, limit=1, should_stop=None):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("HARVEST", "ok"))

    def plant_empty_fields(self, crop="wheat", limit=1, should_stop=None):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("PLANT", crop))


class _StubNews:
    def __init__(self, device, config=None, **kwargs):
        pass

    def shop_from_newspaper(
        self, limit=1, should_stop=None, mode="shop", reset_home=True, until_done=False
    ):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("BUY", "wheat"))

    def reset_loop_state(self):
        return None

    def buy_open_shop(self, limit=1, should_stop=None):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("BUY", "test"))

    def capture_open_shop(self, should_stop=None):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("CAPTURE", "cap_01"))

    def capture_newspaper_ads(self, should_stop=None):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("CAPTURE", "newspaper"))

    def find_column(self):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("FIND_COLUMN", "newspaper_stand"))

    def open_newspaper(self):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("OPEN_NEWSPAPER", "newspaper_stand"))

    def scan_ads(self):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("SCAN_ADS", "wheat"))

    def swipe_newspaper(self):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("SWIPE", "newspaper"))

    def visit_next_shop(self):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("VISIT_SHOP", "ad"))

    def buy_wishlist(self):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("BUY", "wheat"))

    def close_shop(self):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("CLOSE_SHOP", "shop"))

    def go_home(self):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("GO_HOME", "house"))


def _runtime():
    from app.web.runtime import BotRuntime

    return BotRuntime(
        device_factory=lambda cfg: _FakeCtl(),
        farming_cls=_StubFarming,
        newspaper_cls=_StubNews,
        loop_pause_s=0.05,
    )


def test_web_run_harvest_and_busy(httpd: str, data_home: Path, monkeypatch: pytest.MonkeyPatch):
    from app.actions.farming import Action, ActionResult
    from app.web.runtime import BotRuntime

    started = threading.Event()

    class SlowFarming(_StubFarming):
        def harvest_ready_fields(self, limit=1, should_stop=None):
            started.set()
            while not (should_stop and should_stop()):
                time.sleep(0.02)
            return ActionResult(False, Action("STOP", "harvest"), "stopped")

    rt = BotRuntime(
        device_factory=lambda cfg: _FakeCtl(),
        farming_cls=SlowFarming,
        newspaper_cls=_StubNews,
        loop_pause_s=0.05,
    )
    monkeypatch.setattr("app.web.server.RUNTIME", rt)
    code, body = _request(f"{httpd}/api/run", "POST", {"action": "harvest"})
    assert code == 200
    assert body["run"]["status"] == "running"
    started.wait(timeout=1)
    code, busy = _request(f"{httpd}/api/run", "POST", {"action": "harvest"})
    assert code == 409
    code, _ = _request(f"{httpd}/api/stop", "POST", {})
    assert code == 200
    snap = _wait_idle(rt)
    assert snap["status"] == "idle"


def test_web_run_screenshot_and_unknown(httpd: str, data_home: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _runtime()
    monkeypatch.setattr("app.web.server.RUNTIME", rt)
    code, body = _request(f"{httpd}/api/run", "POST", {"action": "screenshot"})
    assert code == 200
    snap = _wait_idle(rt)
    assert snap["has_frame"] is True
    code, png = _request(f"{httpd}/api/frame.png")
    assert code == 200
    assert isinstance(png, bytes) and png[:8] == b"\x89PNG\r\n\x1a\n"
    # The live panel shows a small JPEG of the same frame.
    code, jpg = _request(f"{httpd}/api/frame.jpg")
    assert code == 200
    assert isinstance(jpg, bytes) and jpg[:3] == b"\xff\xd8\xff"
    code, err = _request(f"{httpd}/api/run", "POST", {"action": "explode"})
    assert code == 400
    code, err = _request(
        f"{httpd}/api/run",
        "POST",
        {"action": "loop", "harvest": False, "plant": False, "newspaper": False},
    )
    assert code == 400


def test_snapshot_exposes_loop_timing():
    from app.actions.timing import LoopTiming

    rt = _runtime()
    # No shop actor built yet, and stubs without a collector stay silent.
    assert rt.snapshot()["timing"] is None

    timing = LoopTiming()
    timing.set_waits(buy_wait_s=1.2, visit_wait_s=2.5)
    with timing.step("shop"):
        with timing.step("visit_shop", deadline_s=2.5):
            timing.mark("visit_fail")
            timing.deadline_hit("visit_shop")

    class _Actor:
        pass

    actor = _Actor()
    actor.timing = timing
    rt._news = actor
    snap = rt.snapshot()["timing"]
    assert snap["shop_n"] == 1
    assert snap["steps"]["visit_shop"]["n"] == 1
    assert snap["steps"]["visit_shop"]["deadline_s"] == 2.5
    assert snap["steps"]["visit_shop"]["deadline_hits"] == 1
    assert snap["safety"]["visit_fail"] == 1
    assert snap["waits"]["buy_wait_s"] == 1.2


def test_web_run_newspaper_step(httpd: str, data_home: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _runtime()
    monkeypatch.setattr("app.web.server.RUNTIME", rt)
    code, body = _request(f"{httpd}/api/run", "POST", {"action": "scan_ads"})
    assert code == 200
    snap = _wait_idle(rt)
    assert snap["last_result"]["action"] == "SCAN_ADS"
    assert snap["last_result"]["ok"] is True


def test_news_mode_follow_sweep_and_aliases():
    from app.web.runtime import _news_mode

    assert _news_mode({}) == "follow"
    assert _news_mode({"news_mode": "shop"}) == "follow"
    assert _news_mode({"mode": "buy"}) == "follow"
    assert _news_mode({"news_mode": "sweep"}) == "sweep"
    assert _news_mode({"news_mode": "browse"}) == "browse"
    with pytest.raises(ValueError, match="follow, sweep, or browse"):
        _news_mode({"news_mode": "nope"})


def test_loop_rest_min_clamps():
    from app.web.runtime import _loop_rest_min

    assert _loop_rest_min({"loop_rest_min": 3}) == 3.0
    assert _loop_rest_min({"loop_rest_min": 999}) == 180.0
    assert _loop_rest_min({"loop_rest_min": -2}) == 0.0


def test_web_loop_visits_all_shops_then_rests(
    httpd: str, data_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.actions.farming import Action, ActionResult
    from app.web.runtime import BotRuntime

    seen: dict = {}
    rests: list[float] = []

    class News(_StubNews):
        def shop_from_newspaper(
            self, limit=1, should_stop=None, mode="shop", reset_home=True, until_done=False
        ):
            seen["until_done"] = until_done
            seen["mode"] = mode
            return ActionResult(True, Action("VISIT_SHOP", "done"))

    def fake_rest(self, minutes):
        rests.append(minutes)
        self.stop_event.set()

    monkeypatch.setattr(BotRuntime, "_rest_minutes", fake_rest)
    rt = BotRuntime(
        device_factory=lambda cfg: _FakeCtl(),
        farming_cls=_StubFarming,
        newspaper_cls=News,
        loop_pause_s=0.05,
    )
    monkeypatch.setattr("app.web.server.RUNTIME", rt)
    code, body = _request(
        f"{httpd}/api/run",
        "POST",
        {
            "action": "loop",
            "harvest": False,
            "plant": False,
            "newspaper": True,
            "news_mode": "sweep",
            "loop_rest_min": 7,
        },
    )
    assert code == 200
    snap = _wait_idle(rt, timeout=3)
    assert snap["status"] == "idle"
    assert seen["until_done"] is True
    assert seen["mode"] == "sweep"
    assert rests == [7.0]


def test_web_loop_skips_rest_when_column_not_found(
    httpd: str, data_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.actions.farming import Action, ActionResult
    from app.web.runtime import BotRuntime

    rests: list[float] = []

    class News(_StubNews):
        def shop_from_newspaper(
            self, limit=1, should_stop=None, mode="shop", reset_home=True, until_done=False
        ):
            return ActionResult(
                False, Action("FIND_COLUMN", "newspaper_stand"), "newspaper stand not found"
            )

    def fake_rest(self, minutes):
        rests.append(minutes)
        self.stop_event.set()

    monkeypatch.setattr(BotRuntime, "_rest_minutes", fake_rest)
    rt = BotRuntime(
        device_factory=lambda cfg: _FakeCtl(),
        farming_cls=_StubFarming,
        newspaper_cls=News,
        loop_pause_s=0.05,
    )
    monkeypatch.setattr("app.web.server.RUNTIME", rt)
    code, body = _request(
        f"{httpd}/api/run",
        "POST",
        {
            "action": "loop",
            "harvest": False,
            "plant": False,
            "newspaper": True,
            "news_mode": "sweep",
            "loop_rest_min": 7,
        },
    )
    assert code == 200
    time.sleep(0.2)
    rt.stop()
    snap = _wait_idle(rt, timeout=3)
    assert snap["status"] == "idle"
    assert rests == []



def test_runtime_farming_shares_newspaper_matcher(data_home: Path):
    from app.web.runtime import BotRuntime
    from types import SimpleNamespace

    seen: list[dict] = []

    class RecordingFarming(_StubFarming):
        def __init__(self, device, config=None, **kwargs):
            super().__init__(device, config)
            seen.append(kwargs)

    class NewsWithMatcher(_StubNews):
        def __init__(self, device, config=None, **kwargs):
            self.device = device
            self.matcher = SimpleNamespace(threshold=0.8)
            self.screens = SimpleNamespace()

    runtime = BotRuntime(
        device_factory=lambda cfg: _FakeCtl(),
        farming_cls=RecordingFarming,
        newspaper_cls=NewsWithMatcher,
    )
    runtime._ensure_device()
    news = runtime._newspaper_actor()
    runtime._farming_actor()
    runtime._farming_actor()
    assert [kw["matcher"] for kw in seen] == [news.matcher, news.matcher]
    assert all(kw["screens"] is news.screens for kw in seen)


def test_loop_survives_an_adb_hiccup(data_home: Path, monkeypatch):
    from app.actions.farming import Action, ActionResult
    from app.controller.adb import DeviceTimeout
    from app.web.runtime import BotRuntime

    monkeypatch.setattr("app.web.runtime.LOOP_ADB_WAIT_S", 0)
    rounds: list[str] = []

    class FlakyFarming(_StubFarming):
        def harvest_ready_fields(self, limit=1, should_stop=None):
            rounds.append("harvest")
            if len(rounds) == 1:
                raise DeviceTimeout("ADB hết thời gian: exec-out screencap")
            if len(rounds) >= 3:
                runtime.stop()
            return ActionResult(True, Action("HARVEST", "ok"))

    runtime = BotRuntime(
        device_factory=lambda cfg: _FakeCtl(),
        farming_cls=FlakyFarming,
        newspaper_cls=_StubNews,
        loop_pause_s=0,
    )
    runtime.start("loop", {"harvest": True, "plant": False})
    snap = _wait_idle(runtime)
    assert len(rounds) >= 3
    assert snap["status"] == "idle"
    assert snap["last_error"] is None


def test_loop_restarts_the_game_when_adb_keeps_failing(data_home: Path, monkeypatch):
    """Each run of LOOP_ADB_FAILURES errors earns one game restart; past the
    hourly cap the loop stops with the reason."""
    from app.controller.adb import DeviceTimeout
    from app.web.runtime import LOOP_ADB_FAILURES, LOOP_MAX_RESTARTS_PER_HOUR

    calls: list[int] = []

    class DeadFarming(_StubFarming):
        def harvest_ready_fields(self, limit=1, should_stop=None):
            calls.append(1)
            raise DeviceTimeout("ADB hết thời gian: exec-out screencap")

    ctl = _GameCtl()
    runtime = _recovering_runtime(ctl, DeadFarming, monkeypatch)
    runtime.start("loop", {"harvest": True, "plant": False})
    snap = _wait_idle(runtime, timeout=5.0)
    assert len(ctl.launched) == LOOP_MAX_RESTARTS_PER_HOUR
    assert len(calls) == LOOP_ADB_FAILURES * (LOOP_MAX_RESTARTS_PER_HOUR + 1)
    assert snap["status"] == "error"
    assert "restarted" in snap["last_error"]
    assert "hết thời gian" in snap["last_error"]


class _FakeScreens:
    def __init__(self, screens):
        self.screens = list(screens)

    def detect(self, _png):
        from app.vision.screen import GameScreen, ScreenDetection

        name = self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        return ScreenDetection(GameScreen[name], 1.0, [])


class _GameCtl(_FakeCtl):
    def __init__(self, cfg=None, front="letsfarm.com.playday"):
        super().__init__(cfg)
        self.front = front
        self.stopped: list[str] = []
        self.launched: list[str] = []

    def force_stop(self, package=None):
        self.stopped.append(package)

    def launch_app(self, package=None):
        self.launched.append(package)
        self.front = package

    def foreground_package(self):
        return self.front


def test_restart_game_waits_for_the_farm():
    from app.actions.game import restart_game

    ctl = _GameCtl()
    result = restart_game(
        ctl, _FakeScreens(["UNKNOWN", "UNKNOWN", "FARM"]), "letsfarm.com.playday",
        sleep=lambda _s: None,
    )
    assert result.success
    assert ctl.stopped == ["letsfarm.com.playday"]
    assert ctl.launched == ["letsfarm.com.playday"]


def test_restart_game_gives_up_after_its_timeout():
    from app.actions.game import restart_game

    result = restart_game(
        _GameCtl(), _FakeScreens(["UNKNOWN"]), "letsfarm.com.playday",
        timeout_s=0, sleep=lambda _s: None,
    )
    assert not result.success
    assert "farm not shown" in result.error


def test_restart_game_job_from_the_web(data_home: Path, monkeypatch):
    from app.web.runtime import BotRuntime

    ctl = _GameCtl()
    monkeypatch.setattr("app.actions.game.time.sleep", lambda _s: None)
    runtime = BotRuntime(
        device_factory=lambda cfg: ctl, farming_cls=_StubFarming, newspaper_cls=_StubNews
    )
    monkeypatch.setattr(
        "app.web.runtime.ScreenDetector", lambda *a, **k: _FakeScreens(["FARM"])
    )
    runtime.start("restart_game", {})
    snap = _wait_idle(runtime)
    assert snap["last_result"]["action"] == "RESTART_GAME"
    assert snap["last_result"]["ok"] is True
    assert ctl.launched == ["letsfarm.com.playday"]


def _recovering_runtime(ctl, farming_cls, monkeypatch):
    from app.web.runtime import BotRuntime

    monkeypatch.setattr("app.web.runtime.LOOP_ADB_WAIT_S", 0)
    monkeypatch.setattr("app.actions.game.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "app.web.runtime.ScreenDetector", lambda *a, **k: _FakeScreens(["FARM"])
    )
    return BotRuntime(
        device_factory=lambda cfg: ctl,
        farming_cls=farming_cls,
        newspaper_cls=_StubNews,
        loop_pause_s=0,
    )


def test_loop_restarts_a_game_that_is_not_in_front(data_home: Path, monkeypatch):
    from app.actions.farming import Action, ActionResult

    ctl = _GameCtl(front="com.bluestacks.launcher")
    rounds: list[int] = []

    class Farming(_StubFarming):
        def harvest_ready_fields(self, limit=1, should_stop=None):
            rounds.append(1)
            runtime.stop()
            return ActionResult(True, Action("HARVEST", "ok"))

    runtime = _recovering_runtime(ctl, Farming, monkeypatch)
    runtime.start("loop", {"harvest": True, "plant": False})
    snap = _wait_idle(runtime)
    # Restarted before any tap landed on the launcher, then played a round.
    assert ctl.launched == ["letsfarm.com.playday"]
    assert rounds == [1]
    assert snap["status"] == "idle"


def test_loop_restarts_after_rounds_that_never_reach_the_farm(
    data_home: Path, monkeypatch
):
    from app.actions.farming import Action, ActionResult
    from app.web.runtime import LOOP_STUCK_ROUNDS

    ctl = _GameCtl()
    homes: list[int] = []

    class LostNews(_StubNews):
        def go_home(self):
            homes.append(1)
            if ctl.launched:
                runtime.stop()
                return ActionResult(True, Action("GO_HOME", "house"))
            return ActionResult(False, Action("GO_HOME", "house"), "farm not drawn")

    runtime = _recovering_runtime(ctl, _StubFarming, monkeypatch)
    runtime.newspaper_cls = LostNews
    runtime.start("loop", {"harvest": True, "plant": False})
    snap = _wait_idle(runtime)
    assert ctl.launched == ["letsfarm.com.playday"]
    assert len(homes) == LOOP_STUCK_ROUNDS + 1
    assert snap["status"] == "idle"


def test_loop_gives_up_after_too_many_restarts(data_home: Path, monkeypatch):
    from app.web.runtime import LOOP_MAX_RESTARTS_PER_HOUR

    class Crashing(_GameCtl):
        def launch_app(self, package=None):
            self.launched.append(package)  # the game never stays up

    ctl = Crashing(front="com.bluestacks.launcher")
    runtime = _recovering_runtime(ctl, _StubFarming, monkeypatch)
    runtime.start("loop", {"harvest": True, "plant": False})
    snap = _wait_idle(runtime, timeout=5.0)
    assert len(ctl.launched) == LOOP_MAX_RESTARTS_PER_HOUR
    assert snap["status"] == "error"
    assert "restarted" in snap["last_error"]


def test_run_prefs_round_trip_and_clamp(httpd: str, data_home: Path):
    code, state = _request(f"{httpd}/api/state")
    assert code == 200
    # Nothing saved yet: newspaper on, rest from the config default.
    assert state["run_prefs"]["newspaper"] is True
    assert state["run_prefs"]["harvest"] is False
    assert state["run_prefs"]["loop_rest_min"] == AppConfig().loop_rest_min

    code, saved = _request(
        f"{httpd}/api/run-prefs",
        "PUT",
        {"harvest": True, "news_mode": "follow", "crop": "corn", "limit": 99, "loop_rest_min": -3},
    )
    assert code == 200
    prefs = saved["run_prefs"]
    assert prefs["harvest"] is True
    assert prefs["newspaper"] is True
    assert prefs["news_mode"] == "follow"
    assert prefs["crop"] == "corn"
    assert prefs["limit"] == 20
    assert prefs["loop_rest_min"] == 0.0

    # A partial save keeps the rest; junk falls back instead of failing.
    code, saved = _request(
        f"{httpd}/api/run-prefs", "PUT", {"plant": True, "news_mode": "nope", "crop": "../x"}
    )
    assert code == 200
    assert saved["run_prefs"]["harvest"] is True
    assert saved["run_prefs"]["plant"] is True
    assert saved["run_prefs"]["news_mode"] == "sweep"
    assert saved["run_prefs"]["crop"] == "wheat"
    code, state = _request(f"{httpd}/api/state")
    assert state["run_prefs"] == saved["run_prefs"]


def test_bulk_toggle_items(httpd: str, data_home: Path):
    for key in ("egg", "milk"):
        botdata.upsert_item(key, image_b64=_png_b64(), enabled=False)
    code, body = _request(
        f"{httpd}/api/items/bulk", "POST", {"ids": ["egg", "milk", "wheat"], "enabled": True}
    )
    assert code == 200
    assert body["changed"] == 2
    assert set(botdata.active_wishlist()) == {"egg", "milk", "wheat"}

    # An unknown id fails the whole call: nothing is half applied.
    code, err = _request(
        f"{httpd}/api/items/bulk", "POST", {"ids": ["egg", "ghost"], "enabled": False}
    )
    assert code == 400
    assert "ghost" in err["error"]
    assert "egg" in botdata.active_wishlist()

    code, _ = _request(f"{httpd}/api/items/bulk", "POST", {"ids": [], "enabled": False})
    assert code == 400
    code, _ = _request(f"{httpd}/api/items/bulk", "POST", {"ids": ["egg"], "enabled": "no"})
    assert code == 400


def test_buy_status_keeps_disabled_items(data_home: Path):
    botdata.upsert_item("egg", enabled=True)
    botdata.record_purchase("egg", kind="buy")
    botdata.update_item("egg", enabled=False)
    status = {row["id"]: row for row in botdata.wishlist_buy_status()}
    assert status["egg"]["enabled"] is False
    assert status["egg"]["buy_count"] == 1
    assert status["wheat"]["enabled"] is True


def test_status_endpoint_is_light(httpd: str, data_home: Path):
    code, body = _request(f"{httpd}/api/status")
    assert code == 200
    assert set(body) == {"run", "wishlist_buys", "buys_rev"}
    run = body["run"]
    for key in ("started_at", "rounds", "phase", "rest_until", "frame_seq", "now", "logs", "log_seq"):
        assert key in run


def test_status_skips_what_the_page_already_has(httpd: str, data_home: Path, monkeypatch):
    """Polled every second: with the log_seq and buys_rev it already holds,
    the page gets neither the log nor the purchase history again (and the
    server reads no purchase file or item PNG for it)."""
    first = _request(f"{httpd}/api/status")[1]
    log_seq, rev = first["run"]["log_seq"], first["buys_rev"]
    # A nested context: undoing it must not undo data_home's redirect of the
    # data directory, or the buy below lands in the real purchases.json.
    with monkeypatch.context() as patched:
        patched.setattr(
            "app.web.server.botdata.wishlist_buy_status",
            lambda: (_ for _ in ()).throw(AssertionError("history re-read")),
        )
        code, quiet = _request(f"{httpd}/api/status?log={log_seq}&buys={rev}")
    assert code == 200
    assert "wishlist_buys" not in quiet
    assert "logs" not in quiet["run"]
    assert quiet["buys_rev"] == rev
    assert botdata.purchases_path().parent == data_home

    # A buy moves buys_rev, so the history comes back.
    botdata.record_purchase("wheat", kind="buy")
    code, moved = _request(f"{httpd}/api/status?log={log_seq}&buys={rev}")
    assert moved["buys_rev"] != rev
    assert any(row["id"] == "wheat" and row["buy_count"] == 1 for row in moved["wishlist_buys"])


def test_status_sends_the_log_when_it_moved(data_home: Path):
    import logging

    rt = _runtime()
    seq = str(rt.log_seq)
    assert "logs" not in rt.snapshot(known_log_seq=seq)
    rt._log_handler.emit(
        logging.LogRecord("farmbot", logging.INFO, __file__, 1, "one more line", None, None)
    )
    snap = rt.snapshot(known_log_seq=seq)
    assert snap["logs"][-1].endswith("one more line")
    assert str(snap["log_seq"]) != seq


def test_rotated_logs_are_not_tracked():
    """bot.log.1/.2/.3 matched no ignore rule and ~145k log lines landed in a
    commit."""
    text = (Path(__file__).resolve().parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "logs/*.log.*" in text


def test_snapshot_tracks_rounds_frames_and_run_time(data_home: Path):
    from app.actions.farming import Action, ActionResult

    rounds: list[int] = []

    class Farming(_StubFarming):
        def harvest_ready_fields(self, limit=1, should_stop=None):
            self.device.screenshot()
            rounds.append(1)
            if len(rounds) >= 3:
                runtime.stop()
            return ActionResult(True, Action("HARVEST", "ok"))

    from app.web.runtime import BotRuntime

    runtime = BotRuntime(
        device_factory=lambda cfg: _FakeCtl(),
        farming_cls=Farming,
        newspaper_cls=_StubNews,
        loop_pause_s=0,
    )
    started = runtime.start("loop", {"harvest": True, "plant": False})
    assert started["started_at"] is not None
    assert started["finished_at"] is None
    snap = _wait_idle(runtime)
    assert snap["rounds"] == 2  # the third round was stopped mid-way
    assert snap["frame_seq"] == 3
    assert snap["frame_at"] is not None
    assert snap["finished_at"] >= snap["started_at"]
    assert snap["phase"] is None


def test_skip_rest_starts_the_next_round(data_home: Path):
    from app.actions.farming import Action, ActionResult
    from app.web.runtime import BotRuntime

    class News(_StubNews):
        def shop_from_newspaper(
            self, limit=1, should_stop=None, mode="shop", reset_home=True, until_done=False
        ):
            return ActionResult(True, Action("VISIT_SHOP", "done"))

    runtime = BotRuntime(
        device_factory=lambda cfg: _FakeCtl(),
        farming_cls=_StubFarming,
        newspaper_cls=News,
        loop_pause_s=0,
    )
    assert runtime.skip_rest()["rest_until"] is None  # idle: a no-op
    runtime.start("loop", {"harvest": False, "plant": False, "newspaper": True, "loop_rest_min": 60})
    deadline = time.time() + 2
    while runtime.snapshot()["rest_until"] is None and time.time() < deadline:
        time.sleep(0.01)
    resting = runtime.snapshot()
    assert resting["phase"] == "rest"
    assert resting["rest_s"] == 3600
    assert resting["rest_until"] > resting["now"] + 3000
    assert resting["rounds"] == 1

    runtime.skip_rest()
    deadline = time.time() + 3
    while runtime.snapshot()["rounds"] < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert runtime.snapshot()["rounds"] >= 2
    runtime.stop()
    snap = _wait_idle(runtime, timeout=3)
    assert snap["status"] == "idle"
    assert snap["rest_until"] is None


def test_skip_rest_api(httpd: str, data_home: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _runtime()
    monkeypatch.setattr("app.web.server.RUNTIME", rt)
    code, body = _request(f"{httpd}/api/skip-rest", "POST", {})
    assert code == 200
    assert body["run"]["status"] == "idle"


def test_bundled_font_is_served_and_cached(httpd: str):
    with urllib.request.urlopen(f"{httpd}/static/baloo2-vietnamese.woff2", timeout=5) as res:
        assert res.status == 200
        assert res.headers.get_content_type() == "font/woff2"
        assert "max-age" in res.headers["Cache-Control"]
        assert res.read(4) == b"wOF2"
    # Pages and scripts stay uncached so an edit shows on the next reload.
    with urllib.request.urlopen(f"{httpd}/static/app.js", timeout=5) as res:
        assert res.headers["Cache-Control"] == "no-store"


def test_game_skin_pieces_are_wired(httpd: str):
    _code, page = _request(f"{httpd}/")
    assert page.count(b'class="modal-x"') == page.count(b"<dialog ")
    assert b"rest-bar-label" in page
    _code, css = _request(f"{httpd}/static/style.css")
    for subset in (b"vietnamese", b"latin-ext", b"latin"):
        assert b"/static/baloo2-" + subset + b".woff2" in css
    _code, js = _request(f"{httpd}/static/app.js")
    assert b".modal-x" in js


def _put_config(httpd: str, **fields) -> dict:
    code, body = _request(f"{httpd}/api/config", "PUT", {"adb_host": "127.0.0.1", **fields})
    assert code == 200, body
    return body["config"]


def test_telegram_token_never_leaves_the_server(httpd: str, data_home: Path):
    from app.config import load_config

    saved = _put_config(httpd, telegram_token="123456:SECRETabcd", telegram_chat_id="42")
    assert saved["telegram_token_set"] is True
    assert saved["telegram_token_hint"] == "abcd"
    assert "telegram_token" not in saved
    code, state = _request(f"{httpd}/api/state")
    assert "SECRET" not in json.dumps(state)
    assert state["config"]["telegram_chat_id"] == "42"

    # Saving the form again (token field blank, as the page sends it) keeps it.
    _put_config(httpd, telegram_chat_id="42", debug=True)
    assert load_config().telegram_token == "123456:SECRETabcd"

    cleared = _put_config(httpd, telegram_token_clear=True)
    assert cleared["telegram_token_set"] is False
    assert load_config().telegram_token == ""


def test_telegram_buttons_explain_missing_setup(httpd: str, data_home: Path):
    code, body = _request(f"{httpd}/api/telegram/test", "POST", {})
    assert code == 400
    assert "token" in body["error"]
    code, body = _request(f"{httpd}/api/telegram/chat", "POST", {})
    assert code == 400
    assert "token" in body["error"]


def test_telegram_chat_id_is_found_and_saved(httpd: str, data_home: Path, monkeypatch):
    from app.config import load_config

    _put_config(httpd, telegram_token="123456:SECRETabcd")
    monkeypatch.setattr("app.web.server.detect_chat_id", lambda token: "777")
    code, body = _request(f"{httpd}/api/telegram/chat", "POST", {})
    assert code == 200
    assert body["chat_id"] == "777"
    assert load_config().telegram_chat_id == "777"
    assert load_config().telegram_token == "123456:SECRETabcd"
