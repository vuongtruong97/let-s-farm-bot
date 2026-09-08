from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from letsfarm_auto.detect import FarmLayout, analyze_screen
from letsfarm_auto.models import BotConfig, RunMode, Swipe, Tap
from letsfarm_auto.planner import count_states, plan_cycle
from letsfarm_auto.vision import overlay_grid

LogFn = Callable[[str, str], None]
FrameFn = Callable[[np.ndarray, dict[str, Any]], None]


class FarmBot:
    def __init__(self, device: Any, config: BotConfig):
        self.device = device
        self.config = config
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.last_frame: np.ndarray | None = None
        self.last_stats: dict[str, Any] = {}
        self.cycles = 0
        self.on_log: LogFn | None = None
        self.on_frame: FrameFn | None = None

    def log(self, message: str, level: str = "info") -> None:
        if self.on_log:
            self.on_log(message, level)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="letsfarm-bot", daemon=True)
        self._thread.start()
        self.log("Bot đã chạy.")

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        self.log("Đã gửi lệnh dừng.")

    def scan_once(self) -> dict[str, Any]:
        frame = self.device.screenshot()
        layout = self._see(frame)
        taps, harvest, plant = plan_cycle(layout.grid, self.config)
        stats = count_states(layout.grid)
        overlay = overlay_grid(frame, layout.grid, self.config, harvest + plant)
        self.last_frame = overlay
        payload = {
            "stats": stats,
            "harvest_swipes": len(harvest),
            "plant_swipes": len(plant),
            "taps": len(taps),
            "mode": str(self.config.run_mode),
            "detected_rows": layout.rows,
            "detected_cols": layout.cols,
            "plot_count": layout.plot_count,
            "seed": {"x": layout.seed.x, "y": layout.seed.y},
        }
        self.last_stats = payload
        if self.on_frame:
            self.on_frame(overlay, payload)
        return payload

    def _see(self, frame) -> FarmLayout:
        layout = analyze_screen(frame)
        self.config.farm = layout.farm
        self.config.rows = max(1, layout.rows)
        self.config.cols = max(1, layout.cols)
        self.config.seed_button = layout.seed
        self.config.deselect_point = layout.deselect
        return layout

    def _cycle(self) -> None:
        frame = self.device.screenshot()
        layout = self._see(frame)
        grid = layout.grid
        taps, harvest, plant = plan_cycle(grid, self.config)
        stats = count_states(grid)
        overlay = overlay_grid(frame, grid, self.config, harvest + plant)
        self.last_frame = overlay
        payload = {
            "stats": stats,
            "harvest_swipes": len(harvest),
            "plant_swipes": len(plant),
            "cycles": self.cycles + 1,
            "mode": str(self.config.run_mode),
            "detected_rows": layout.rows,
            "detected_cols": layout.cols,
            "plot_count": layout.plot_count,
            "seed": {"x": layout.seed.x, "y": layout.seed.y},
        }
        self.last_stats = payload
        if self.on_frame:
            self.on_frame(overlay, payload)

        if layout.plot_count == 0:
            self.log("Không thấy ô ruộng trên màn hình. Mở Let's Farm, zoom để thấy cả vườn.", "error")
            return

        self.log(
            f"Tự thấy {layout.rows}×{layout.cols} ô · "
            f"sẵn {stats.get('ready', 0)} · đang lớn {stats.get('growing', 0)} · "
            f"trống {stats.get('empty', 0)} · hạt giống ({layout.seed.x},{layout.seed.y})"
        )

        if self.config.run_mode in (RunMode.HARVEST, RunMode.CYCLE) and harvest:
            self.log(f"Thu hoạch {len(harvest)} hàng kéo.")
            for swipe in harvest:
                if self._stop.is_set():
                    return
                self.device.swipe(_jitter_swipe(swipe, self.config))
                self._sleep(0.12 + self.config.tap_pause_s * 0.4)

        if self.config.run_mode in (RunMode.PLANT, RunMode.CYCLE):
            if self.config.run_mode == RunMode.CYCLE:
                frame = self.device.screenshot()
                layout = self._see(frame)
                taps, _, plant = plan_cycle(layout.grid, self.config)
            if plant:
                self.log(f"Trồng {len(plant)} hàng kéo.")
                for swipe in plant:
                    if self._stop.is_set():
                        return
                    self.device.tap(Tap(self.config.seed_button))
                    self._sleep(self.config.tap_pause_s)
                    self.device.swipe(_jitter_swipe(swipe, self.config))
                    self._sleep(0.12 + self.config.tap_pause_s * 0.4)
                self.device.tap(Tap(self.config.deselect_point))

        if not harvest and not plant:
            self.log("Không có ô cần thao tác, chờ vòng sau.")

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._cycle()
                self.cycles += 1
                self._sleep(self.config.cycle_pause_s)
        except Exception as exc:  # noqa: BLE001 — surface to UI log
            self.log(f"Bot dừng vì lỗi: {exc}", "error")
        finally:
            self.running = False

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end:
            if self._stop.is_set():
                return
            time.sleep(0.05)


def _jitter_swipe(swipe: Swipe, config: BotConfig) -> Swipe:
    rng = np.random.default_rng()
    return Swipe(
        start=swipe.start.jitter(rng, config.jitter_px),
        end=swipe.end.jitter(rng, config.jitter_px),
        duration_ms=swipe.duration_ms + int(rng.integers(-40, 50)),
        cells=swipe.cells,
    )
