from __future__ import annotations

from letsfarm_auto.bot import FarmBot
from letsfarm_auto.demo_farm import DemoFarm
from letsfarm_auto.detect import analyze_screen, detect_seed_button
from letsfarm_auto.models import BotConfig, CellState, Point, Rect, RunMode


def test_auto_detects_default_grid_and_seed():
    farm = DemoFarm()
    layout = analyze_screen(farm.screenshot())
    assert layout.rows == 5
    assert layout.cols == 8
    assert layout.plot_count == 40
    seed = layout.seed
    assert abs(seed.x - farm.world_seed.x) < 45
    assert abs(seed.y - farm.world_seed.y) < 35


def test_auto_detects_other_grid_sizes():
    config = BotConfig(rows=4, cols=6)
    farm = DemoFarm(config=config)
    layout = analyze_screen(farm.screenshot())
    assert layout.rows == 4
    assert layout.cols == 6
    assert layout.plot_count == 24


def test_seed_button_is_leftmost_tray_tile():
    farm = DemoFarm()
    seed = detect_seed_button(farm.screenshot())
    assert abs(seed.x - farm.world_seed.x) < 45
    assert seed.x < 250


def test_bot_works_with_wrong_manual_calibration():
    farm = DemoFarm(config=BotConfig())
    for row in farm.plots:
        for plot in row:
            plot.state = CellState.READY
            plot.ready_at = 10**12
    blind = BotConfig(
        farm=Rect(0, 0, 40, 40),
        rows=1,
        cols=1,
        seed_button=Point(4, 4),
        run_mode=RunMode.HARVEST,
        jitter_px=0,
        tap_pause_s=0.0,
        cycle_pause_s=0.0,
    )
    bot = FarmBot(farm, blind)
    before = sum(p.state == CellState.READY for row in farm.plots for p in row)
    bot._cycle()
    after = sum(p.state == CellState.READY for row in farm.plots for p in row)
    assert before == 40
    assert after < before
    assert bot.config.rows == 5
    assert bot.config.cols == 8
