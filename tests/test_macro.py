from __future__ import annotations

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
from app.controller.recorder import (
    GestureAssembler,
    MacroRecorder,
    parse_abs_ranges,
    parse_getevent_line,
)
from app.actions.macro import play_macro
from app.storage import botdata, macros as macrostore
from app.web.server import BotWebHandler


TAP_LINES = """
[    1.000000] /dev/input/event2: EV_ABS       ABS_MT_TRACKING_ID   00000001
[    1.000000] /dev/input/event2: EV_ABS       ABS_MT_POSITION_X    000001f4
[    1.000000] /dev/input/event2: EV_ABS       ABS_MT_POSITION_Y    00000190
[    1.000000] /dev/input/event2: EV_SYN       SYN_REPORT           00000000
[    1.050000] /dev/input/event2: EV_ABS       ABS_MT_TRACKING_ID   ffffffff
""".strip()

SWIPE_LINES = """
[    2.000000] /dev/input/event2: EV_ABS       ABS_MT_TRACKING_ID   00000002
[    2.000000] /dev/input/event2: EV_ABS       ABS_MT_POSITION_X    00000064
[    2.000000] /dev/input/event2: EV_ABS       ABS_MT_POSITION_Y    000000c8
[    2.000000] /dev/input/event2: EV_SYN       SYN_REPORT           00000000
[    2.200000] /dev/input/event2: EV_ABS       ABS_MT_POSITION_X    00000190
[    2.200000] /dev/input/event2: EV_ABS       ABS_MT_POSITION_Y    000000c8
[    2.200000] /dev/input/event2: EV_SYN       SYN_REPORT           00000000
[    2.250000] /dev/input/event2: EV_ABS       ABS_MT_TRACKING_ID   ffffffff
""".strip()


def _feed(lines: str, width: int = 1920, height: int = 1080):
    assembler = GestureAssembler(width, height)
    last_t = 0.0
    out = []
    for line in lines.splitlines():
        event = parse_getevent_line(line, last_t)
        assert event is not None
        last_t = event.t or last_t
        gesture = assembler.feed(event)
        if gesture is not None:
            out.append(gesture)
    return out


def test_parse_getevent_tap():
    gestures = _feed(TAP_LINES)
    assert gestures == [{"type": "tap", "x": 500, "y": 400}]


def test_parse_getevent_swipe():
    gestures = _feed(SWIPE_LINES)
    assert len(gestures) == 1
    step = gestures[0]
    assert step["type"] == "swipe"
    assert (step["x1"], step["y1"]) == (100, 200)
    assert (step["x2"], step["y2"]) == (400, 200)
    assert step["duration_ms"] >= 80


def test_parse_abs_ranges_picks_mt_device():
    text = """
add device 1: /dev/input/event1
  name:     "gpio-keys"
add device 2: /dev/input/event2
  name:     "synaptics"
      0035  : value 0, min 0, max 32767, fuzz 0, flat 0, resolution 0
      0036  : value 0, min 0, max 18431, fuzz 0, flat 0, resolution 0
"""
    found = parse_abs_ranges(text)
    assert found[0].device == "/dev/input/event2"
    assert found[0].xmax == 32767
    assert found[0].ymax == 18431


def test_save_load_macro(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
    path = macrostore.save_macro(
        {
            "name": "open_shop",
            "width": 1920,
            "height": 1080,
            "steps": [
                {"type": "tap", "x": 10, "y": 20},
                {"type": "wait", "ms": 9000},
                {"type": "back"},
            ],
        }
    )
    assert path.name == "open_shop.json"
    loaded = macrostore.load_macro("open_shop")
    assert loaded["steps"][1]["ms"] == 5000
    rows = macrostore.list_macros()
    assert rows[0]["id"] == "open_shop"
    assert rows[0]["steps"] == 3


def test_web_gesture_dedup_skips_getevent():
    rec = MacroRecorder("demo", 1920, 1080)
    rec.add_web_step({"type": "tap", "x": 100, "y": 200}, now=1.0)
    skipped = rec.add_getevent_step({"type": "tap", "x": 110, "y": 205}, now=1.05)
    assert skipped is None
    assert rec.snapshot_steps() == [{"type": "tap", "x": 100, "y": 200}]
    added = rec.add_getevent_step({"type": "tap", "x": 400, "y": 500}, now=1.4)
    assert added is not None
    steps = rec.snapshot_steps()
    assert steps[1]["type"] == "wait"
    assert steps[1]["ms"] == 400
    assert steps[2] == {"type": "tap", "x": 400, "y": 500}


class _PlayDevice:
    def __init__(self):
        self.taps = []
        self.swipes = []
        self.backs = 0
        self.homes = 0

    def tap(self, x, y):
        self.taps.append((x, y))

    def swipe(self, x1, y1, x2, y2, duration=None):
        self.swipes.append((x1, y1, x2, y2, duration))

    def back(self):
        self.backs += 1

    def home(self):
        self.homes += 1


def test_play_macro_on_fake_device(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.actions.macro.time.sleep", lambda _s: None)
    device = _PlayDevice()
    result = play_macro(
        device,
        {
            "name": "demo",
            "width": 1920,
            "height": 1080,
            "steps": [
                {"type": "wait", "ms": 120},
                {"type": "tap", "x": 50, "y": 60},
                {"type": "swipe", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "duration_ms": 200},
                {"type": "back"},
                {"type": "home"},
            ],
        },
    )
    assert result.success is True
    assert device.taps == [(50, 60)]
    assert device.swipes == [(1, 2, 3, 4, 200)]
    assert device.backs == 1
    assert device.homes == 1


def test_play_macro_stops(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.actions.macro.time.sleep", lambda _s: None)
    device = _PlayDevice()
    result = play_macro(
        device,
        {
            "name": "demo",
            "steps": [
                {"type": "tap", "x": 1, "y": 1},
                {"type": "tap", "x": 2, "y": 2},
            ],
        },
        should_stop=lambda: True,
    )
    assert result.success is False
    assert device.taps == []


def _png_bytes(size: tuple[int, int] = (32, 32)) -> bytes:
    image = Image.new("RGB", size, (20, 180, 90))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class _FakeCtl:
    def __init__(self, cfg=None):
        self.serial = ""
        self.taps = []
        self.swipes = []
        self.backs = 0
        self.homes = 0

    def connect(self, host=None, port=None):
        self.serial = "127.0.0.1:5555"
        return self.serial

    def screenshot(self) -> bytes:
        return _png_bytes()

    def tap(self, x, y):
        self.taps.append((int(x), int(y)))

    def swipe(self, x1, y1, x2, y2, duration=None):
        self.swipes.append((x1, y1, x2, y2, duration))

    def back(self):
        self.backs += 1

    def home(self):
        self.homes += 1

    def resolution(self):
        return 1920, 1080


class _StubFarming:
    def __init__(self, device, config=None, **kwargs):
        pass

    def harvest_ready_fields(self, limit=1, should_stop=None):
        from app.actions.farming import Action, ActionResult

        return ActionResult(True, Action("HARVEST", "ok"))


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


@pytest.fixture
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("app.config.DATA_DIR", tmp_path)
    monkeypatch.setattr("app.config.CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "templates").mkdir()
    save_config(AppConfig(debug=True, adb_port=5555), tmp_path / "config.json")
    botdata.save_wishlist({"wheat": {"template": "item_wheat"}})
    botdata.save_crops({"wheat": {"storage": "silo", "seed_template": "seed_wheat"}})
    return tmp_path


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


def _runtime(device=None):
    from app.web.runtime import BotRuntime

    ctl = device or _FakeCtl()
    return BotRuntime(
        device_factory=lambda cfg: ctl,
        farming_cls=_StubFarming,
        newspaper_cls=_StubNews,
        loop_pause_s=0.05,
    ), ctl


def _wait_idle(runtime, timeout: float = 2.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = runtime.snapshot()
        if snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError(runtime.snapshot())


def test_web_record_gesture_writes_file(
    httpd: str, data_home: Path, monkeypatch: pytest.MonkeyPatch
):
    rt, ctl = _runtime()
    monkeypatch.setattr("app.web.server.RUNTIME", rt)
    code, body = _request(f"{httpd}/api/macros/record/start", "POST", {"name": "open_shop"})
    assert code == 200
    assert body["run"]["status"] == "recording"
    code, body = _request(
        f"{httpd}/api/macros/gesture",
        "POST",
        {"type": "tap", "x": 120, "y": 340},
    )
    assert code == 200
    assert ctl.taps == [(120, 340)]
    steps = body["run"]["recording"]["steps"]
    assert steps[-1] == {"type": "tap", "x": 120, "y": 340}
    code, busy = _request(f"{httpd}/api/run", "POST", {"action": "harvest"})
    assert code == 409
    code, shot = _request(f"{httpd}/api/run", "POST", {"action": "screenshot"})
    assert code == 200
    assert shot["run"]["status"] == "recording"
    time.sleep(0.05)
    code, stopped = _request(f"{httpd}/api/macros/record/stop", "POST", {})
    assert code == 200
    assert stopped["run"]["status"] == "idle"
    saved = macrostore.load_macro("open_shop")
    assert saved["steps"][-1]["type"] == "tap"
    assert (data_home / "macros" / "open_shop.json").is_file()


def test_web_play_macro(httpd: str, data_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.actions.macro.time.sleep", lambda _s: None)
    rt, ctl = _runtime()
    monkeypatch.setattr("app.web.server.RUNTIME", rt)
    macrostore.save_macro(
        {
            "name": "demo",
            "width": 1920,
            "height": 1080,
            "steps": [{"type": "tap", "x": 9, "y": 8}, {"type": "back"}],
        }
    )
    code, body = _request(f"{httpd}/api/run", "POST", {"action": "macro", "name": "demo"})
    assert code == 200
    snap = _wait_idle(rt)
    assert snap["status"] == "idle"
    assert ctl.taps == [(9, 8)]
    assert ctl.backs == 1
    code, listed = _request(f"{httpd}/api/macros")
    assert code == 200
    assert listed["macros"][0]["id"] == "demo"
    code, _ = _request(f"{httpd}/api/macros/demo", "DELETE")
    assert code == 200
    assert macrostore.list_macros() == []
