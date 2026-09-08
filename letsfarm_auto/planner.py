from __future__ import annotations

from letsfarm_auto.models import BotConfig, Cell, CellState, Point, Swipe, Tap


def swipe_duration(start: Point, end: Point, config: BotConfig) -> int:
    dist = ((end.x - start.x) ** 2 + (end.y - start.y) ** 2) ** 0.5
    ms = int(config.swipe_min_ms + dist * config.swipe_ms_per_px)
    return max(config.swipe_min_ms, min(config.swipe_max_ms, ms))


def plan_swipes(grid: list[list[Cell]], target: CellState, config: BotConfig) -> list[Swipe]:
    """Group consecutive target plots on each row into one Let's Farm drag."""
    swipes: list[Swipe] = []
    for row in grid:
        run: list[Cell] = []
        for cell in row:
            if cell.state == target:
                run.append(cell)
                continue
            swipes.extend(_flush_run(run, config))
            run = []
        swipes.extend(_flush_run(run, config))
        if len(swipes) >= config.max_swipes_per_cycle:
            break
    return swipes[: config.max_swipes_per_cycle]


def _flush_run(run: list[Cell], config: BotConfig) -> list[Swipe]:
    if not run:
        return []
    start = run[0].center
    end = run[-1].center
    if start == end:
        span = 16
        if run[0].box is not None:
            span = max(12, run[0].box.w // 3)
        end = Point(start.x + span, start.y)
    cells = tuple((cell.row, cell.col) for cell in run)
    return [Swipe(start=start, end=end, duration_ms=swipe_duration(start, end, config), cells=cells)]


def count_states(grid: list[list[Cell]]) -> dict[str, int]:
    tally = {state.value: 0 for state in CellState}
    for row in grid:
        for cell in row:
            tally[cell.state.value] += 1
    return tally


def plan_cycle(
    grid: list[list[Cell]], config: BotConfig
) -> tuple[list[Tap], list[Swipe], list[Swipe]]:
    harvest = plan_swipes(grid, CellState.READY, config)
    plant = []
    taps: list[Tap] = []
    if config.run_mode in ("plant", "cycle"):
        plant = plan_swipes(grid, CellState.EMPTY, config)
        if plant:
            taps.append(Tap(config.seed_button))
    if config.run_mode == "harvest":
        plant = []
        taps = []
    if config.run_mode == "plant":
        harvest = []
    return taps, harvest, plant
