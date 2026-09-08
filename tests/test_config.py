from __future__ import annotations

from letsfarm_auto.device import ports_from_bluestacks_conf
from letsfarm_auto.models import BotConfig, CellState, DeviceKind, Point, Rect, RunMode
from letsfarm_auto.store import load_config, save_config


def test_config_roundtrip(tmp_path):
    config = BotConfig(
        device=DeviceKind.ADB,
        adb_port=5625,
        rows=6,
        cols=10,
        run_mode=RunMode.HARVEST,
        farm=Rect(10, 20, 800, 400),
        seed_button=Point(120, 600),
        cycle_pause_s=3.5,
    )
    path = tmp_path / "config.json"
    save_config(config, path)
    loaded = load_config(path)
    assert loaded.adb_port == 5625
    assert loaded.rows == 6
    assert loaded.cols == 10
    assert loaded.run_mode == RunMode.HARVEST
    assert loaded.farm == Rect(10, 20, 800, 400)
    assert loaded.seed_button == Point(120, 600)
    assert loaded.cycle_pause_s == 3.5


def test_missing_config_uses_defaults(tmp_path):
    loaded = load_config(tmp_path / "nope.json")
    assert loaded.device == DeviceKind.DEMO
    assert loaded.rows == 5
    assert loaded.cols == 8


def test_parse_bluestacks_conf_ports():
    text = """
bst.instance.Pie64.status.adb_port="5555"
bst.instance.Rvc64.status.adb_port="5632"
bst.instance.Pie64.adb_port=1
"""
    assert ports_from_bluestacks_conf(text) == [5555, 5632]


def test_hit_cell_and_centers():
    config = BotConfig(farm=Rect(100, 100, 800, 400), rows=4, cols=8)
    assert config.hit_cell(50, 50) is None
    assert config.hit_cell(140, 140) == (0, 0)
    assert config.hit_cell(850, 450) == (3, 7)
    center = config.cell_center(0, 0)
    assert 100 < center.x < 200
    assert 100 < center.y < 200
