"""Wall-clock split for the shop loop: ADB vs sleep vs CPU.

CPU is derived, not measured: whatever a step spends outside ADB calls and
sleeps is template matching. That keeps the instrumentation to two call sites
(device I/O and sleep) instead of wrapping every vision call.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

SAFETY_COUNTERS = ("buys", "verify_fail", "buy_skip", "visit_fail", "deadline_hits")
ROOT_STEP = "shop"


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


@dataclass
class StepStat:
    n: int = 0
    total_s: float = 0.0
    last_s: float = 0.0
    min_s: float = 0.0
    max_s: float = 0.0
    adb_s: float = 0.0
    sleep_s: float = 0.0
    shots: int = 0
    deadline_s: float | None = None
    deadline_hits: int = 0

    def add(self, total_s: float, adb_s: float, sleep_s: float, shots: int) -> None:
        self.n += 1
        self.last_s = total_s
        self.total_s += total_s
        self.min_s = total_s if self.n == 1 else min(self.min_s, total_s)
        self.max_s = max(self.max_s, total_s)
        self.adb_s += adb_s
        self.sleep_s += sleep_s
        self.shots += shots

    def as_dict(self) -> dict:
        runs = max(1, self.n)
        cpu_s = max(0.0, self.total_s - self.adb_s - self.sleep_s)
        return {
            "n": self.n,
            "last_s": _round(self.last_s),
            "avg_s": _round(self.total_s / runs),
            "min_s": _round(self.min_s),
            "max_s": _round(self.max_s),
            "total_s": _round(self.total_s),
            "adb_s": _round(self.adb_s / runs),
            "sleep_s": _round(self.sleep_s / runs),
            "cpu_s": _round(cpu_s / runs),
            "shots": self.shots,
            "shots_avg": _round(self.shots / runs, 1),
            "deadline_s": (
                None if self.deadline_s is None else _round(self.deadline_s)
            ),
            "deadline_hits": self.deadline_hits,
        }


@dataclass
class _Frame:
    name: str
    started: float
    adb_s: float = 0.0
    sleep_s: float = 0.0
    shots: int = 0
    # Set by the caller when the run turned out not to be a real step, e.g. a
    # shop iteration that found no listing left. Keeps min/avg honest.
    discard: bool = False


@dataclass
class LoopTiming:
    """One session of the newspaper shop loop. Written by the bot thread,
    read by the web thread through snapshot()."""

    steps: dict[str, StepStat] = field(default_factory=dict)
    safety: dict[str, int] = field(default_factory=dict)
    waits: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.steps = {}
            self.safety = dict.fromkeys(SAFETY_COUNTERS, 0)
            self._stack: list[_Frame] = []
            self._started = time.monotonic()
            self._root_s = 0.0
            self._adb_s = 0.0
            self._sleep_s = 0.0
            self._shot_n = 0
            self._shot_s = 0.0
            self._shot_max_s = 0.0
            self._decode_s = 0.0

    def set_waits(self, **waits: float) -> None:
        with self._lock:
            self.waits = {key: _round(value) for key, value in waits.items()}

    @contextmanager
    def step(self, name: str, deadline_s: float | None = None):
        frame = _Frame(name, time.monotonic())
        with self._lock:
            self._stack.append(frame)
            stat = self.steps.setdefault(name, StepStat())
            if deadline_s is not None:
                stat.deadline_s = float(deadline_s)
        try:
            yield frame
        finally:
            elapsed = time.monotonic() - frame.started
            with self._lock:
                if frame in self._stack:
                    self._stack.remove(frame)
                if not frame.discard:
                    self.steps.setdefault(name, StepStat()).add(
                        elapsed, frame.adb_s, frame.sleep_s, frame.shots
                    )
                    if not self._stack:
                        self._root_s += elapsed

    @contextmanager
    def adb(self, *, shot: bool = False):
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            with self._lock:
                self._adb_s += elapsed
                for frame in self._stack:
                    frame.adb_s += elapsed
                    if shot:
                        frame.shots += 1
                if shot:
                    self._shot_n += 1
                    self._shot_s += elapsed
                    self._shot_max_s = max(self._shot_max_s, elapsed)

    def sleep(self, seconds: float) -> None:
        wanted = max(0.0, float(seconds))
        if wanted <= 0:
            return
        started = time.monotonic()
        time.sleep(wanted)
        elapsed = time.monotonic() - started
        with self._lock:
            self._sleep_s += elapsed
            for frame in self._stack:
                frame.sleep_s += elapsed

    def add_decode(self, seconds: float) -> None:
        with self._lock:
            self._decode_s += max(0.0, float(seconds))

    def deadline_hit(self, name: str) -> None:
        with self._lock:
            self.steps.setdefault(name, StepStat()).deadline_hits += 1
            self.safety["deadline_hits"] = self.safety.get("deadline_hits", 0) + 1

    def mark(self, counter: str, amount: int = 1) -> None:
        with self._lock:
            self.safety[counter] = self.safety.get(counter, 0) + amount

    @property
    def current(self) -> str | None:
        with self._lock:
            return self._stack[-1].name if self._stack else None

    def snapshot(self) -> dict:
        with self._lock:
            shops = self.steps[ROOT_STEP].n if ROOT_STEP in self.steps else 0
            shop_total = self.steps[ROOT_STEP].total_s if shops else 0.0
            cpu_s = max(0.0, self._root_s - self._adb_s - self._sleep_s)
            return {
                "current": self._stack[-1].name if self._stack else None,
                "shop_n": shops,
                "session_s": _round(time.monotonic() - self._started),
                "per_shop_s": _round(shop_total / shops) if shops else 0.0,
                "split_pct": _split_pct(self._adb_s, self._sleep_s, cpu_s),
                "waits": dict(self.waits),
                "shot": {
                    "n": self._shot_n,
                    "avg_s": _round(self._shot_s / self._shot_n)
                    if self._shot_n
                    else 0.0,
                    "max_s": _round(self._shot_max_s),
                    "decode_s": _round(self._decode_s),
                    "per_shop": _round(self._shot_n / shops, 1) if shops else 0.0,
                },
                "safety": dict(self.safety),
                "steps": {
                    name: stat.as_dict() for name, stat in sorted(self.steps.items())
                },
            }

    def summary_line(self) -> str:
        snap = self.snapshot()
        split = snap["split_pct"]
        safety = snap["safety"]
        return (
            f"TIME session shops={snap['shop_n']} "
            f"s/shop={snap['per_shop_s']:g} "
            f"adb={split['adb']}% sleep={split['sleep']}% cpu={split['cpu']}% "
            f"shots/shop={snap['shot']['per_shop']:g} "
            f"shot_avg={snap['shot']['avg_s']:g}s "
            f"buys={safety.get('buys', 0)} "
            f"verify_fail={safety.get('verify_fail', 0)} "
            f"deadline_hits={safety.get('deadline_hits', 0)}"
        )

    def step_line(self, name: str) -> str:
        with self._lock:
            stat = self.steps.get(name)
            data = stat.as_dict() if stat is not None else None
        if data is None:
            return f"TIME {name} n/a"
        cap = "" if data["deadline_s"] is None else f" cap={data['deadline_s']:g}"
        return (
            f"TIME {name} {data['last_s']:g}s adb={data['adb_s']:g} "
            f"cpu={data['cpu_s']:g} shots={data['shots_avg']:g}{cap}"
        )


def _split_pct(adb_s: float, sleep_s: float, cpu_s: float) -> dict:
    total = adb_s + sleep_s + cpu_s
    if total <= 0:
        return {"adb": 0, "sleep": 0, "cpu": 0}
    adb = round(adb_s / total * 100)
    sleep = round(sleep_s / total * 100)
    return {"adb": adb, "sleep": sleep, "cpu": max(0, 100 - adb - sleep)}
