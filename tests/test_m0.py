from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

from app.config import AppConfig, load_config, save_config
from app.controller.adb import ports_from_bluestacks_conf
from app.controller.device import DeviceController, _valid_png
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

    save_config(
        AppConfig(adb_port=5625, debug=True, action_wait_s=1.5, buy_wait_s=3.0),
        path,
    )
    loaded = load_config(path)
    assert loaded.action_wait_s == 1.5
    assert loaded.buy_wait_s == 3.0


def test_valid_png_keeps_exec_out_bytes():
    raw = _png_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert _valid_png(raw) == raw


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
