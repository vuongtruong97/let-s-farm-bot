from __future__ import annotations

from collections import Counter

from letsfarm_auto.demo_farm import DemoFarm
from letsfarm_auto.detect import analyze_screen
from letsfarm_auto.models import CellState
from letsfarm_auto.vision import classify_cell_bgr


def _fill(farm: DemoFarm, state: CellState) -> None:
    import time

    ready_at = time.monotonic() + 999
    for row in farm.plots:
        for plot in row:
            plot.state = state
            plot.crop = 0
            plot.ready_at = ready_at


def test_color_classifier_on_rendered_plots():
    farm = DemoFarm()
    expected = {
        CellState.EMPTY: 0,
        CellState.GROWING: 0,
        CellState.READY: 0,
    }
    for state in expected:
        _fill(farm, state)
        frame = farm.screenshot()
        for r in range(farm.world_rows):
            for c in range(farm.world_cols):
                box = farm.cell_rect(r, c)
                crop = frame[box.y + 8 : box.y2 - 8, box.x + 8 : box.x2 - 8]
                got, _score = classify_cell_bgr(crop)
                if got == state:
                    expected[state] += 1
        total = farm.world_rows * farm.world_cols
        assert expected[state] / total >= 0.85, f"{state} accuracy {expected[state]}/{total}"


def test_auto_scan_matches_demo_state():
    farm = DemoFarm()
    _fill(farm, CellState.READY)
    farm.plots[0][0].state = CellState.EMPTY
    farm.plots[1][2].state = CellState.GROWING
    farm.plots[1][2].ready_at = 10**12
    grid = analyze_screen(farm.screenshot()).grid
    flat = [cell.state for row in grid for cell in row]
    counts = Counter(flat)
    assert counts[CellState.READY] >= 30
    assert grid[0][0].state == CellState.EMPTY
    assert grid[1][2].state in {CellState.GROWING, CellState.READY}
