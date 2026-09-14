"""Parse ADB getevent touch streams into tap/swipe steps."""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass

from app.storage.logger import get_logger

log = get_logger("RECORD")

DEDUP_MS = 120
DEDUP_PX = 20
TAP_PX = 20
MAX_WAIT_MS = 5000
MIN_WAIT_MS = 50

EV_SYN = 0x0000
EV_KEY = 0x0001
EV_ABS = 0x0003
SYN_REPORT = 0x0000
ABS_MT_POSITION_X = 0x0035
ABS_MT_POSITION_Y = 0x0036
ABS_MT_TRACKING_ID = 0x0039
BTN_TOUCH = 0x014A

LABELED = re.compile(
    r"\[\s*([\d.]+)\]\s+(/dev/input/event\d+):\s+(\S+)\s+(\S+)\s+(\S+)"
)
RAW = re.compile(
    r"(/dev/input/event\d+):\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)"
)
ABS_NAMED = re.compile(
    r"ABS_MT_POSITION_([XY])\s*:.*?min\s+(-?\d+),\s*max\s+(-?\d+)",
    re.IGNORECASE,
)
ABS_CODE = re.compile(
    r"\b003([56])\s*:.*?min\s+(-?\d+),\s*max\s+(-?\d+)",
    re.IGNORECASE,
)
DEVICE_LINE = re.compile(r"add device \d+:\s+(/dev/input/event\d+)")


@dataclass(frozen=True)
class InputEvent:
    t: float
    device: str
    etype: str
    code: str
    value: int


@dataclass
class AbsRange:
    device: str
    xmin: int = 0
    xmax: int = 0
    ymin: int = 0
    ymax: int = 0


def parse_getevent_line(line: str, last_t: float = 0.0) -> InputEvent | None:
    text = line.strip()
    if not text:
        return None
    match = LABELED.search(text)
    if match:
        t, device, etype, code, raw = match.groups()
        return InputEvent(float(t), device, etype.upper(), code.upper(), _value(raw))
    match = RAW.search(text)
    if not match:
        return None
    device, etype_h, code_h, raw = match.groups()
    etype_n = int(etype_h, 16)
    code_n = int(code_h, 16)
    return InputEvent(
        last_t,
        device,
        _etype_name(etype_n),
        _code_name(etype_n, code_n),
        _value(raw),
    )


def parse_abs_ranges(text: str) -> list[AbsRange]:
    found: list[AbsRange] = []
    current: AbsRange | None = None
    for line in text.splitlines():
        device = DEVICE_LINE.search(line)
        if device:
            if current and (current.xmax or current.ymax):
                found.append(current)
            current = AbsRange(device.group(1))
            continue
        if current is None:
            continue
        axis = _abs_axis(line)
        if not axis:
            continue
        which, lo, hi = axis
        if which == "X":
            current.xmin, current.xmax = lo, hi
        else:
            current.ymin, current.ymax = lo, hi
    if current and (current.xmax or current.ymax):
        found.append(current)
    return found


def _abs_axis(line: str) -> tuple[str, int, int] | None:
    named = ABS_NAMED.search(line)
    if named:
        return named.group(1).upper(), int(named.group(2)), int(named.group(3))
    coded = ABS_CODE.search(line)
    if coded:
        return ("X" if coded.group(1) == "5" else "Y"), int(coded.group(2)), int(coded.group(3))
    return None


def map_abs(value: int, lo: int, hi: int, size: int) -> int:
    if size <= 1:
        return 0
    if hi <= lo or hi <= 0:
        return max(0, min(size - 1, value))
    if hi < size * 2:
        return max(0, min(size - 1, value))
    scaled = int(round((value - lo) * (size - 1) / (hi - lo)))
    return max(0, min(size - 1, scaled))


def steps_similar(a: dict, b: dict, px: int = DEDUP_PX) -> bool:
    if a.get("type") != b.get("type"):
        return False
    kind = a["type"]
    if kind == "tap":
        return abs(a["x"] - b["x"]) <= px and abs(a["y"] - b["y"]) <= px
    if kind == "swipe":
        return (
            abs(a["x1"] - b["x1"]) <= px
            and abs(a["y1"] - b["y1"]) <= px
            and abs(a["x2"] - b["x2"]) <= px
            and abs(a["y2"] - b["y2"]) <= px
        )
    return kind in {"back", "home"}


class GestureAssembler:
    def __init__(self, width: int, height: int, abs_range: AbsRange | None = None):
        self.width = max(1, width)
        self.height = max(1, height)
        self.abs_range = abs_range
        self.points: list[tuple[int, int]] = []
        self.started = 0.0
        self.last_t = 0.0
        self._x: int | None = None
        self._y: int | None = None
        self._down = False

    def feed(self, event: InputEvent) -> dict | None:
        if self.abs_range and event.device and event.device != self.abs_range.device:
            return None
        self.last_t = event.t or self.last_t
        if event.etype == "EV_ABS" and event.code == "ABS_MT_POSITION_X":
            self._x = self._map_x(event.value)
            return None
        if event.etype == "EV_ABS" and event.code == "ABS_MT_POSITION_Y":
            self._y = self._map_y(event.value)
            return None
        if event.etype == "EV_ABS" and event.code == "ABS_MT_TRACKING_ID":
            if event.value < 0:
                return self._finish()
            self._down = True
            if not self.points:
                self.started = event.t
            return None
        if event.etype == "EV_KEY" and event.code == "BTN_TOUCH":
            if event.value <= 0:
                return self._finish()
            self._down = True
            if not self.points:
                self.started = event.t
            return None
        if event.etype == "EV_SYN" and event.code == "SYN_REPORT":
            if self._x is not None and self._y is not None:
                if not self.points:
                    self.started = event.t or self.started
                    self._down = True
                self.points.append((self._x, self._y))
        return None

    def _map_x(self, value: int) -> int:
        rng = self.abs_range
        return map_abs(value, rng.xmin if rng else 0, rng.xmax if rng else 0, self.width)

    def _map_y(self, value: int) -> int:
        rng = self.abs_range
        return map_abs(value, rng.ymin if rng else 0, rng.ymax if rng else 0, self.height)

    def _finish(self) -> dict | None:
        if not self.points and self._x is not None and self._y is not None:
            self.points.append((self._x, self._y))
        points = self.points
        self.points = []
        self._x = self._y = None
        self._down = False
        if not points:
            return None
        x0, y0 = points[0]
        x1, y1 = points[-1]
        duration = max(80, int(round((self.last_t - self.started) * 1000))) if self.last_t else 80
        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        if dist < TAP_PX:
            return {"type": "tap", "x": x0, "y": y0}
        return {
            "type": "swipe",
            "x1": x0,
            "y1": y0,
            "x2": x1,
            "y2": y1,
            "duration_ms": duration,
        }


class MacroRecorder:
    def __init__(self, name: str, width: int, height: int):
        self.name = name
        self.width = width
        self.height = height
        self.steps: list[dict] = []
        self._last_action_at = 0.0
        self._web_step: dict | None = None
        self._web_at = 0.0
        self._lock = threading.Lock()

    def add_web_step(self, step: dict, now: float | None = None) -> dict | None:
        return self._add(step, source="web", now=now)

    def add_getevent_step(self, step: dict, now: float | None = None) -> dict | None:
        return self._add(step, source="getevent", now=now)

    def remove_step(self, index: int) -> None:
        with self._lock:
            if index < 0 or index >= len(self.steps):
                raise ValueError("step index out of range")
            self.steps.pop(index)

    def set_wait_ms(self, index: int, ms: int) -> None:
        with self._lock:
            if index < 0 or index >= len(self.steps):
                raise ValueError("step index out of range")
            step = self.steps[index]
            if step.get("type") != "wait":
                raise ValueError("step is not wait")
            step["ms"] = max(0, min(MAX_WAIT_MS, int(ms)))

    def snapshot_steps(self) -> list[dict]:
        with self._lock:
            return [dict(step) for step in self.steps]

    def to_macro(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "width": self.width,
                "height": self.height,
                "steps": [dict(step) for step in self.steps],
            }

    def _add(self, step: dict, source: str, now: float | None) -> dict | None:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            if source == "getevent" and self._web_step is not None:
                if steps_similar(step, self._web_step) and (stamp - self._web_at) * 1000 <= DEDUP_MS:
                    return None
            if self._last_action_at:
                gap = int(round((stamp - self._last_action_at) * 1000))
                if gap >= MIN_WAIT_MS:
                    self.steps.append({"type": "wait", "ms": min(MAX_WAIT_MS, gap)})
            self.steps.append(step)
            self._last_action_at = stamp
            if source == "web":
                self._web_step = dict(step)
                self._web_at = stamp
            return step


class GeteventListener:
    def __init__(self, adb_bin: str, serial: str, width: int, height: int):
        self.adb_bin = adb_bin
        self.serial = serial
        self.width = width
        self.height = height
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, on_gesture) -> None:
        abs_range = self._probe_range()
        assembler = GestureAssembler(self.width, self.height, abs_range)
        self._stop.clear()
        self._proc = subprocess.Popen(
            [self.adb_bin, "-s", self.serial, "shell", "getevent", "-t", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        self._thread = threading.Thread(
            target=self._read, args=(assembler, on_gesture), daemon=True
        )
        self._thread.start()
        log.info("getevent listener start")

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()
        self._proc = None
        log.info("getevent listener stop")

    def _probe_range(self) -> AbsRange | None:
        try:
            completed = subprocess.run(
                [self.adb_bin, "-s", self.serial, "shell", "getevent", "-p"],
                capture_output=True,
                timeout=4,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = (completed.stdout or b"").decode("utf-8", errors="ignore")
        ranges = parse_abs_ranges(text)
        return ranges[0] if ranges else None

    def _read(self, assembler: GestureAssembler, on_gesture) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        last_t = 0.0
        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                event = parse_getevent_line(line, last_t)
                if event is None:
                    continue
                last_t = event.t or last_t
                gesture = assembler.feed(event)
                if gesture is not None:
                    on_gesture(gesture)
        except Exception as exc:
            log.info(f"getevent listener FAIL {exc}")


def _value(raw: str) -> int:
    text = (raw or "").strip().lower()
    if text.startswith("-") and re.fullmatch(r"-?\d+", text):
        return int(text, 10)
    n = int(text, 16)
    if n >= 0x80000000:
        n -= 0x100000000
    return n


def _etype_name(n: int) -> str:
    return {EV_SYN: "EV_SYN", EV_KEY: "EV_KEY", EV_ABS: "EV_ABS"}.get(n, f"0x{n:04x}")


def _code_name(etype: int, code: int) -> str:
    if etype == EV_SYN:
        return "SYN_REPORT" if code == SYN_REPORT else f"0x{code:04x}"
    if etype == EV_KEY:
        return "BTN_TOUCH" if code == BTN_TOUCH else f"0x{code:04x}"
    if etype == EV_ABS:
        names = {
            ABS_MT_POSITION_X: "ABS_MT_POSITION_X",
            ABS_MT_POSITION_Y: "ABS_MT_POSITION_Y",
            ABS_MT_TRACKING_ID: "ABS_MT_TRACKING_ID",
        }
        return names.get(code, f"0x{code:04x}")
    return f"0x{code:04x}"
