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
    assert main(["web", "--host", "127.0.0.1", "--port", "48722"]) == 0
    assert called == {"host": "127.0.0.1", "port": 48722}


@pytest.fixture
def httpd(data_home: Path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), BotWebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


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

    code, page = _request(f"{httpd}/")
    assert code == 200
    assert b"Wishlist" in page
    assert b"Macro" in page
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
        },
    )
    assert code == 200
    assert cfg["config"]["allow_diamond_spending"] is False
    assert cfg["config"]["adb_port"] == 5625
    assert cfg["config"]["buy_threshold"] == 0.65
    assert cfg["config"]["news_threshold"] == 0.78

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

    def shop_from_newspaper(self, limit=1, should_stop=None, mode="shop", reset_home=True):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("BUY", "wheat"))

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
    code, err = _request(f"{httpd}/api/run", "POST", {"action": "explode"})
    assert code == 400
    code, err = _request(
        f"{httpd}/api/run",
        "POST",
        {"action": "loop", "harvest": False, "plant": False, "newspaper": False},
    )
    assert code == 400


def test_web_run_newspaper_step(httpd: str, data_home: Path, monkeypatch: pytest.MonkeyPatch):
    rt = _runtime()
    monkeypatch.setattr("app.web.server.RUNTIME", rt)
    code, body = _request(f"{httpd}/api/run", "POST", {"action": "scan_ads"})
    assert code == 200
    snap = _wait_idle(rt)
    assert snap["last_result"]["action"] == "SCAN_ADS"
    assert snap["last_result"]["ok"] is True

