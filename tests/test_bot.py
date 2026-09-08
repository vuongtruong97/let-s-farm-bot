from __future__ import annotations

from letsfarm_auto.bot import FarmBot
from letsfarm_auto.demo_farm import DemoFarm, cells_on_segment
from letsfarm_auto.models import BotConfig, CellState, Point, RunMode, Swipe, Tap


def _count(farm: DemoFarm, state: CellState) -> int:
    return sum(plot.state == state for row in farm.plots for plot in row)


def test_harvest_swipe_clears_ready_row():
    config = BotConfig(rows=5, cols=8, jitter_px=0)
    farm = DemoFarm(config=config)
    for plot in farm.plots[2]:
        plot.state = CellState.READY
    start = farm.cell_center(2, 0)
    end = farm.cell_center(2, 7)
    farm.swipe(Swipe(start=start, end=end, duration_ms=400))
    assert all(plot.state == CellState.EMPTY for plot in farm.plots[2])


def test_plant_requires_seed_then_swipe():
    config = BotConfig(rows=5, cols=8, jitter_px=0)
    farm = DemoFarm(config=config)
    for plot in farm.plots[0]:
        plot.state = CellState.EMPTY
    farm.swipe(
        Swipe(start=farm.cell_center(0, 0), end=farm.cell_center(0, 3), duration_ms=300)
    )
    assert all(plot.state == CellState.EMPTY for plot in farm.plots[0][:4])
    farm.tap(Tap(farm.world_seed))
    assert farm.selected_seed
    farm.swipe(
        Swipe(start=farm.cell_center(0, 0), end=farm.cell_center(0, 3), duration_ms=300)
    )
    assert all(plot.state == CellState.GROWING for plot in farm.plots[0][:4])


def test_cells_on_segment_walks_a_row():
    farm = DemoFarm()
    start = farm.cell_center(1, 1)
    end = farm.cell_center(1, 4)
    cells = cells_on_segment(start, end, farm)
    assert cells[0] == (1, 1)
    assert cells[-1] == (1, 4)
    assert (1, 2) in cells and (1, 3) in cells


def test_bot_harvest_cycle_reduces_ready_plots():
    config = BotConfig(
        rows=5,
        cols=8,
        run_mode=RunMode.HARVEST,
        jitter_px=0,
        tap_pause_s=0.0,
        cycle_pause_s=0.0,
    )
    farm = DemoFarm(config=config)
    for row in farm.plots:
        for plot in row:
            plot.state = CellState.READY
    before = _count(farm, CellState.READY)
    bot = FarmBot(farm, config)
    bot._cycle()
    after = _count(farm, CellState.READY)
    assert before == 40
    assert after < before
    assert _count(farm, CellState.EMPTY) > 0


def test_bot_cycle_plants_after_harvest():
    config = BotConfig(
        rows=5,
        cols=8,
        run_mode=RunMode.CYCLE,
        jitter_px=0,
        tap_pause_s=0.0,
        cycle_pause_s=0.0,
        demo_grow_s=30,
    )
    farm = DemoFarm(config=config)
    for r, row in enumerate(farm.plots):
        for c, plot in enumerate(row):
            plot.state = CellState.READY if r == 0 else CellState.EMPTY
    bot = FarmBot(farm, config)
    bot._cycle()
    assert _count(farm, CellState.READY) == 0
    assert _count(farm, CellState.GROWING) >= 20
    assert _count(farm, CellState.EMPTY) <= 8
