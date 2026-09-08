from __future__ import annotations

from letsfarm_auto.models import BotConfig, Cell, CellState, Point, RunMode
from letsfarm_auto.planner import count_states, plan_cycle, plan_swipes


def _cell(row: int, col: int, state: CellState) -> Cell:
    return Cell(row=row, col=col, state=state, center=Point(50 + col * 40, 50 + row * 40))


def test_row_swipes_group_consecutive_ready_plots():
    grid = [
        [
            _cell(0, 0, CellState.READY),
            _cell(0, 1, CellState.READY),
            _cell(0, 2, CellState.READY),
            _cell(0, 3, CellState.EMPTY),
            _cell(0, 4, CellState.READY),
        ]
    ]
    config = BotConfig(rows=1, cols=5)
    swipes = plan_swipes(grid, CellState.READY, config)
    assert len(swipes) == 2
    assert swipes[0].cells == ((0, 0), (0, 1), (0, 2))
    assert swipes[1].cells == ((0, 4),)
    assert swipes[0].duration_ms >= config.swipe_min_ms


def test_plan_cycle_harvest_only_skips_planting():
    grid = [
        [_cell(0, 0, CellState.READY), _cell(0, 1, CellState.EMPTY)],
    ]
    config = BotConfig(rows=1, cols=2, run_mode=RunMode.HARVEST)
    taps, harvest, plant = plan_cycle(grid, config)
    assert taps == []
    assert plant == []
    assert len(harvest) == 1


def test_plan_cycle_plant_taps_seed_bag():
    grid = [
        [_cell(0, 0, CellState.EMPTY), _cell(0, 1, CellState.EMPTY)],
    ]
    config = BotConfig(rows=1, cols=2, run_mode=RunMode.PLANT)
    taps, harvest, plant = plan_cycle(grid, config)
    assert harvest == []
    assert taps[0].point == config.seed_button
    assert len(plant) == 1
    assert plant[0].cells == ((0, 0), (0, 1))


def test_count_states():
    grid = [
        [_cell(0, 0, CellState.READY), _cell(0, 1, CellState.GROWING)],
        [_cell(1, 0, CellState.EMPTY), _cell(1, 1, CellState.UNKNOWN)],
    ]
    assert count_states(grid) == {
        "empty": 1,
        "growing": 1,
        "ready": 1,
        "unknown": 1,
    }
