from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.config import AppConfig, load_config, save_config
from app.controller.adb import ports_from_bluestacks_conf
from app.controller.device import DeviceController, _valid_png
from app.controller.adb import DeviceError
from app.controller.input import InputController
from app.main import main


def _png_bytes(size: tuple[int, int] = (8, 6)) -> bytes:
    image = Image.new("RGB", size, (20, 180, 90))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_parse_bluestacks_conf_ports():
    text = """
bst.instance.Pie64.status.adb_port="5555"
bst.instance.Rvc64.status.adb_port="5632"
bst.instance.Pie64.adb_port=1
"""
    assert ports_from_bluestacks_conf(text) == [5555, 5632]


def test_config_roundtrip(tmp_path: Path):
    path = tmp_path / "config.json"
    save_config(
        AppConfig(adb_port=5625, debug=True, allow_diamond_spending=True),
        path,
    )
    loaded = load_config(path)
    assert loaded.adb_port == 5625
    assert loaded.debug is True
    assert loaded.allow_diamond_spending is False
    assert loaded.buy_threshold == 0.72
    assert loaded.news_threshold == 0.72
    assert loaded.loop_rest_min == 5.0
    assert loaded.action_wait_s == 0.9
    assert loaded.buy_wait_s == 2.0
    assert loaded.visit_wait_s == 2.5
    assert loaded.poll_interval_s == 0.0
    assert loaded.stall_swipe_ms == 280

    save_config(
        AppConfig(
            adb_port=5625,
            debug=True,
            action_wait_s=1.5,
            buy_wait_s=3.0,
            visit_wait_s=1.8,
            poll_interval_s=0.3,
            stall_swipe_ms=200,
        ),
        path,
    )
    loaded = load_config(path)
    assert loaded.action_wait_s == 1.5
    assert loaded.buy_wait_s == 3.0
    assert loaded.visit_wait_s == 1.8
    assert loaded.poll_interval_s == 0.3
    assert loaded.stall_swipe_ms == 200


def test_valid_png_keeps_exec_out_bytes():
    raw = _png_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert _valid_png(raw) == raw


def test_valid_png_skips_decode_for_clean_frame():
    raw = _png_bytes()
    # Same object back: a clean frame must not be decoded and re-encoded.
    assert _valid_png(raw) is raw


def test_valid_png_rejects_junk():
    with pytest.raises(DeviceError):
        _valid_png(b"not a png at all")
    with pytest.raises(DeviceError):
        _valid_png(b"")


def test_valid_png_repairs_adb_shell_crlf():
    raw = _png_bytes().replace(b"\n", b"\r\n")
    repaired = _valid_png(raw)
    image = Image.open(BytesIO(repaired))
    assert image.size == (8, 6)


def test_input_builds_tap_and_swipe_commands():
    adb = MagicMock()
    ctl = InputController(adb, "127.0.0.1:5555")
    ctl.tap(100, 200)
    ctl.swipe(10, 20, 30, 40, 250)
    ctl.back()
    assert adb.run.call_args_list[0].args[0] == [
        "-s",
        "127.0.0.1:5555",
        "shell",
        "input",
        "tap",
        "100",
        "200",
    ]
    swipe = adb.run.call_args_list[1].args[0]
    assert swipe[-5:] == ["10", "20", "30", "40", "250"]
    assert adb.run.call_args_list[2].args[0][-1] == "4"


def test_device_screenshot_uses_exec_out(tmp_path: Path):
    png = _png_bytes((16, 9))
    adb = MagicMock()
    adb.run.return_value = png
    device = DeviceController(AppConfig(), adb=adb)
    device.serial = "127.0.0.1:5555"
    saved = device.save_screenshot(tmp_path / "shot.png")
    assert saved.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert adb.run.call_args.args[0][:4] == [
        "-s",
        "127.0.0.1:5555",
        "exec-out",
        "screencap",
    ]


def test_cli_devices(monkeypatch, capsys):
    monkeypatch.setattr(
        "app.main.AdbClient",
        lambda adb_bin=None: MagicMock(
            start_server=lambda: None,
            devices=lambda: [("127.0.0.1:5555", "device")],
        ),
    )
    assert main(["devices"]) == 0
    assert "127.0.0.1:5555" in capsys.readouterr().out


def test_cli_requires_command():
    with pytest.raises(SystemExit):
        main([])


def _raw_frame(width: int, height: int, header: int = 16, fmt: int = 1) -> bytes:
    import struct

    head = struct.pack("<III", width, height, fmt) + b"\x00" * (header - 12)
    # Every pixel red in RGBA order.
    return head + bytes([255, 0, 0, 255]) * (width * height)


@pytest.mark.parametrize("header", [12, 16])
def test_device_screenshot_parses_raw_screencap(header: int):
    adb = MagicMock()
    adb.run.return_value = _raw_frame(4, 3, header)
    device = DeviceController(AppConfig(), adb=adb)
    device.serial = "127.0.0.1:5555"
    frame = device.screenshot()
    assert frame.shape == (3, 4, 3)
    assert frame[0, 0].tolist() == [0, 0, 255]  # BGR red
    assert adb.run.call_args.args[0][-1] == "screencap"


def test_device_screenshot_falls_back_to_png_once():
    png = _png_bytes((16, 9))
    adb = MagicMock()
    adb.run.return_value = png
    device = DeviceController(AppConfig(), adb=adb)
    device.serial = "127.0.0.1:5555"
    assert device.screenshot().shape == (9, 16, 3)
    assert device.screenshot().shape == (9, 16, 3)
    # One raw attempt, then PNG for the rest of the session.
    calls = [c.args[0][-1] for c in adb.run.call_args_list]
    assert calls == ["screencap", "-p", "-p"]


def test_device_screenshot_png_only_when_raw_disabled():
    adb = MagicMock()
    adb.run.return_value = _png_bytes((16, 9))
    device = DeviceController(AppConfig(screencap_raw=False), adb=adb)
    device.serial = "127.0.0.1:5555"
    device.screenshot()
    assert adb.run.call_args_list[0].args[0][-1] == "-p"


def test_log_handler_rotates_and_survives_failed_rename(tmp_path: Path, monkeypatch):
    import logging

    from app.storage import logger as logmod

    path = tmp_path / "bot.log"
    handler = logmod._RotatingHandler(path, maxBytes=200, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("farmbot", logging.INFO, "", 0, "x" * 80, None, None)
    for _ in range(6):
        handler.emit(record)
    assert (tmp_path / "bot.log.1").is_file()

    def refuse(*_args):
        raise PermissionError("file in use")

    monkeypatch.setattr(handler, "rotate", refuse)
    for _ in range(6):
        handler.emit(record)  # must not raise or stop writing
    handler.close()
    assert path.stat().st_size > 200


def _device_with(run, monkeypatch):
    monkeypatch.setattr("app.controller.device.time.sleep", lambda _s: None)
    adb = MagicMock()
    adb.run.side_effect = run
    device = DeviceController(AppConfig(), adb=adb)
    device.serial = "127.0.0.1:5555"
    device.input = InputController(adb, device.serial)
    return device, adb


def test_screenshot_survives_an_adb_timeout(monkeypatch):
    from app.controller.adb import DeviceTimeout

    replies = [DeviceTimeout("ADB hết thời gian"), _raw_frame(4, 3)]

    def run(args, timeout=12.0):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    device, adb = _device_with(run, monkeypatch)
    assert device.screenshot().shape == (3, 4, 3)
    adb.connect.assert_called_once_with("127.0.0.1:5555")


def test_cut_off_raw_frame_is_retried_not_downgraded(monkeypatch):
    full = _raw_frame(4, 3)
    replies = [full[:-8], full]
    device, _adb = _device_with(lambda args, timeout=12.0: replies.pop(0), monkeypatch)
    assert device.screenshot().shape == (3, 4, 3)
    assert device._raw_screencap is True


def test_screenshot_gives_up_after_its_retries(monkeypatch):
    from app.controller.adb import DeviceTimeout

    def run(args, timeout=12.0):
        raise DeviceTimeout("ADB hết thời gian")

    device, adb = _device_with(run, monkeypatch)
    with pytest.raises(DeviceTimeout):
        device.screenshot()
    assert adb.run.call_count == 3


def test_timed_out_tap_is_not_sent_twice(monkeypatch):
    from app.controller.adb import DeviceTimeout

    def run(args, timeout=12.0):
        raise DeviceTimeout("ADB hết thời gian")

    device, adb = _device_with(run, monkeypatch)
    device.tap(10, 20)  # must not raise
    taps = [c for c in adb.run.call_args_list if "tap" in c.args[0]]
    assert len(taps) == 1
    adb.connect.assert_called_once()


def test_tap_to_a_lost_device_is_resent_after_reconnect(monkeypatch):
    replies = [DeviceError("error: device '127.0.0.1:5555' not found"), b""]

    def run(args, timeout=12.0):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    device, adb = _device_with(run, monkeypatch)
    device.tap(10, 20)
    taps = [c for c in adb.run.call_args_list if "tap" in c.args[0]]
    assert len(taps) == 2


def test_unknown_tap_error_is_not_swallowed(monkeypatch):
    def run(args, timeout=12.0):
        raise DeviceError("ADB lỗi (3221225786)")

    device, _adb = _device_with(run, monkeypatch)
    with pytest.raises(DeviceError):
        device.tap(10, 20)
