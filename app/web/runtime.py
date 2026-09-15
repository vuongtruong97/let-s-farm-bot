"""Background bot runner used by the web UI. One job at a time."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from app.actions.farming import ActionResult, FarmingActions
from app.actions.macro import play_macro
from app.actions.shop import NewspaperActions
from app.config import load_config
from app.controller.camera import CameraManager
from app.controller.device import DeviceController
from app.controller.recorder import GeteventListener, MacroRecorder
from app.storage.botdata import clean_id
from app.storage.logger import ActionFormatter, get_logger
from app.storage.macros import save_macro
from app.vision.screen import ScreenDetector

log = get_logger("RUN")

JOBS = frozenset(
    {
        "loop",
        "harvest",
        "plant",
        "newspaper",
        "screenshot",
        "detect",
        "connect",
        "pan",
        "back",
        "home",
        "macro",
        "buy_shop",
        "capture_shop",
        "capture_news",
        "find_column",
        "open_newspaper",
        "scan_ads",
        "swipe_newspaper",
        "visit_shop",
        "buy_wishlist",
        "close_shop",
        "go_home",
    }
)
NEWS_STEP_METHODS = {
    "go_home": "go_home",
    "find_column": "find_column",
    "open_newspaper": "open_newspaper",
    "scan_ads": "scan_ads",
    "swipe_newspaper": "swipe_newspaper",
    "visit_shop": "visit_next_shop",
    "buy_wishlist": "buy_wishlist",
    "close_shop": "close_shop",
    "buy_shop": "buy_open_shop",
    "capture_shop": "capture_open_shop",
    "capture_news": "capture_newspaper_ads",
}
QUICK_DURING_RECORD = frozenset({"screenshot", "connect", "detect", "pan", "back", "home"})
MAX_LOGS = 80
MAX_LIMIT = 20


class BusyError(RuntimeError):
    pass


class BotRuntime:
    def __init__(
        self,
        device_factory=None,
        farming_cls=FarmingActions,
        newspaper_cls=NewspaperActions,
        loop_pause_s: float = 2.0,
    ):
        self.device_factory = device_factory or (lambda cfg: DeviceController(cfg))
        self.farming_cls = farming_cls
        self.newspaper_cls = newspaper_cls
        self.loop_pause_s = loop_pause_s
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.device = None
        self.camera: CameraManager | None = None
        self.status = "idle"
        self.job: str | None = None
        self.serial = ""
        self.screen: str | None = None
        self.last_error: str | None = None
        self.last_result: dict | None = None
        self.last_frame: bytes | None = None
        self.logs: deque[str] = deque(maxlen=MAX_LOGS)
        self.recorder: MacroRecorder | None = None
        self._news = None
        self._listener: GeteventListener | None = None
        self._hooked = False
        self._log_handler: logging.Handler | None = None
        self._attach_logs()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "job": self.job,
                "stopping": self.status == "running" and self.stop_event.is_set(),
                "connected": bool(self.serial),
                "serial": self.serial,
                "screen": self.screen,
                "last_error": self.last_error,
                "last_result": self.last_result,
                "has_frame": self.last_frame is not None,
                "logs": list(self.logs),
                "recording": self._recording_snap(),
            }

    def frame_png(self) -> bytes | None:
        with self.lock:
            return self.last_frame

    def start(self, action: str, payload: dict | None = None) -> dict:
        job = (action or "").strip().lower()
        if job not in JOBS:
            raise ValueError(f"unknown action '{action}'")
        payload = payload or {}
        if job == "loop":
            harvest = bool(payload.get("harvest", True))
            plant = bool(payload.get("plant", True))
            newspaper = bool(payload.get("newspaper", False))
            if not (harvest or plant or newspaper):
                raise ValueError("chọn ít nhất một hành vi")
        if job == "pan":
            direction = str(payload.get("direction") or "")
            if direction not in {"left", "right", "up", "down", "reset"}:
                raise ValueError("pan needs direction left/right/up/down/reset")
        if job == "macro" and not str(payload.get("name") or "").strip():
            raise ValueError("macro name required")
        if job in {"newspaper", "loop"}:
            _news_mode(payload)
        with self.lock:
            if self.status == "running":
                raise BusyError("bot is running")
            recording = self.status == "recording"
            if recording and job not in QUICK_DURING_RECORD:
                raise BusyError("bot is recording")
            if not recording:
                self.stop_event.clear()
                self.status = "running"
                self.job = job
                self.last_error = None
        target = self._quick_worker if recording else self._worker
        threading.Thread(target=target, args=(job, payload), daemon=True).start()
        return self.snapshot()

    def stop(self) -> dict:
        self.stop_event.set()
        log.info("stop requested")
        return self.snapshot()

    def start_record(self, name: str) -> dict:
        key = clean_id(name)
        with self.lock:
            if self.status == "running":
                raise BusyError("bot is running")
            if self.status == "recording":
                raise BusyError("already recording")
            self.status = "recording"
            self.job = "record"
            self.last_error = None
        try:
            self._ensure_device()
            width, height = self.device.resolution()
            recorder = MacroRecorder(key, width, height)
            with self.lock:
                self.recorder = recorder
            try:
                self.device.screenshot()
            except Exception:
                pass
            self._start_getevent(width, height)
        except Exception as exc:
            self._stop_getevent()
            with self.lock:
                self.recorder = None
                self.status = "error"
                self.job = None
                self.last_error = str(exc)
            raise
        log.info(f"RECORD start {key} {width}x{height}")
        return self.snapshot()

    def stop_record(self) -> dict:
        self._stop_getevent()
        rec = self.recorder
        saved = None
        if rec is not None:
            saved = save_macro(rec.to_macro())
            log.info(f"RECORD stop {rec.name} steps={len(rec.steps)} -> {saved}")
        with self.lock:
            self.recorder = None
            if self.status == "recording":
                self.status = "idle"
            self.job = None
        return self.snapshot()

    def apply_gesture(self, payload: dict) -> dict:
        kind = str(payload.get("type") or "").strip().lower()
        self._ensure_device()
        step = _gesture_step(kind, payload)
        if kind == "tap":
            self.device.tap(step["x"], step["y"])
        elif kind == "swipe":
            self.device.swipe(
                step["x1"], step["y1"], step["x2"], step["y2"], step["duration_ms"]
            )
        elif kind == "back":
            self.device.back()
        elif kind == "home":
            self.device.home()
        rec = self.recorder
        if rec is not None and self.status == "recording":
            rec.add_web_step(step)
        try:
            self.device.screenshot()
        except Exception:
            pass
        return self.snapshot()

    def record_set_wait(self, index: int, ms: int) -> dict:
        rec = self.recorder
        if rec is None:
            raise ValueError("not recording")
        rec.set_wait_ms(index, ms)
        return self.snapshot()

    def record_remove_step(self, index: int) -> dict:
        rec = self.recorder
        if rec is None:
            raise ValueError("not recording")
        rec.remove_step(index)
        return self.snapshot()

    def _recording_snap(self) -> dict | None:
        rec = self.recorder
        if rec is None and self.status != "recording":
            return None
        if rec is None:
            return {"active": True, "name": "", "width": 0, "height": 0, "steps": []}
        return {
            "active": self.status == "recording",
            "name": rec.name,
            "width": rec.width,
            "height": rec.height,
            "steps": rec.snapshot_steps(),
        }

    def _start_getevent(self, width: int, height: int) -> None:
        serial = self.serial or getattr(self.device, "serial", "")
        if not serial:
            return
        adb = getattr(self.device, "adb", None)
        if adb is None:
            return
        adb_bin = getattr(adb, "adb_bin", None) or load_config().adb_bin or "adb"
        try:
            listener = GeteventListener(adb_bin, serial, width, height)
            listener.start(self._on_getevent)
            self._listener = listener
        except Exception as exc:
            log.info(f"getevent skip {exc}")

    def _stop_getevent(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    def _on_getevent(self, step: dict) -> None:
        rec = self.recorder
        if rec is None or self.status != "recording":
            return
        rec.add_getevent_step(step)
        try:
            if self.device is not None:
                self.device.screenshot()
        except Exception:
            pass

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def _worker(self, job: str, payload: dict) -> None:
        try:
            self._ensure_device()
            self._dispatch(job, payload)
        except Exception as exc:
            log.info(f"job {job} FAIL {exc}")
            with self.lock:
                self.last_error = str(exc)
                self.status = "error"
                self.job = job
            return
        with self.lock:
            if self.status == "running":
                self.status = "idle"
            self.job = None

    def _quick_worker(self, job: str, payload: dict) -> None:
        try:
            self._ensure_device()
            self._dispatch(job, payload)
        except Exception as exc:
            log.info(f"job {job} FAIL {exc}")
            with self.lock:
                self.last_error = str(exc)

    def _dispatch(self, job: str, payload: dict) -> None:
        if job == "connect":
            self._set_result(True, "CONNECT", self.serial)
        elif job == "screenshot":
            self.device.screenshot()
            self._set_result(True, "SCREENSHOT", "ok")
        elif job == "detect":
            self._detect()
        elif job == "back":
            self.device.back()
            self._set_result(True, "BACK", "ok")
        elif job == "home":
            self.device.home()
            self._set_result(True, "HOME", "ok")
        elif job == "pan":
            self._pan(str(payload.get("direction")))
        elif job == "harvest":
            self._harvest(_limit(payload))
        elif job == "plant":
            self._plant(str(payload.get("crop") or "wheat"), _limit(payload))
        elif job == "newspaper":
            self._newspaper(_limit(payload), _news_mode(payload))
        elif job == "loop":
            self._loop(payload)
        elif job == "macro":
            self._macro(str(payload.get("name") or ""))
        elif job in NEWS_STEP_METHODS:
            self._news_step(job, payload)

    def _macro(self, name: str) -> None:
        result = play_macro(self.device, name, should_stop=self._stopped)
        self._store_action(result)

    def _newspaper_actor(self):
        cfg = load_config()
        if self._news is None or getattr(self._news, "device", None) is not self.device:
            self._news = self.newspaper_cls(self.device, cfg)
        else:
            self._news.config = cfg
            matcher = getattr(self._news, "matcher", None)
            if matcher is not None:
                matcher.threshold = cfg.template_threshold
                if hasattr(matcher, "reload"):
                    matcher.reload()
            news_det = getattr(self._news, "news", None)
            if news_det is not None:
                news_det.buy_threshold = cfg.buy_threshold
                news_det.news_threshold = cfg.news_threshold
            if hasattr(self._news, "wait_s"):
                self._news.wait_s = cfg.action_wait_s
            if hasattr(self._news, "buy_wait_s"):
                self._news.buy_wait_s = cfg.buy_wait_s
            if hasattr(self._news, "wishlist"):
                from app.storage.botdata import active_wishlist

                self._news.wishlist = active_wishlist()
        return self._news

    def _news_step(self, job: str, payload: dict) -> None:
        actor = self._newspaper_actor()
        method = getattr(actor, NEWS_STEP_METHODS[job])
        if job in {"buy_shop"}:
            result = method(limit=_limit(payload), should_stop=self._stopped)
        elif job in {"capture_shop", "capture_news"}:
            result = method(should_stop=self._stopped)
        else:
            result = method()
        self._store_action(result)

    def _ensure_device(self) -> None:
        cfg = load_config()
        if self.device is None:
            self.device = self.device_factory(cfg)
        if not getattr(self.device, "serial", ""):
            self.serial = self.device.connect()
        else:
            self.serial = getattr(self.device, "serial", "") or self.serial
        if self.camera is None or self.camera.device is not self.device:
            self.camera = CameraManager(self.device)
        self._hook_screenshot()

    def _hook_screenshot(self) -> None:
        if self._hooked or self.device is None:
            return
        original = self.device.screenshot

        def hooked():
            png = original()
            with self.lock:
                self.last_frame = png
            return png

        self.device.screenshot = hooked
        self._hooked = True

    def _detect(self) -> None:
        png = self.device.screenshot()
        result = ScreenDetector().detect(png)
        with self.lock:
            self.screen = result.screen.value
        self._set_result(True, "DETECT", result.screen.value)

    def _pan(self, direction: str) -> None:
        assert self.camera is not None
        if direction == "reset":
            self.camera.reset_camera()
        else:
            getattr(self.camera, f"pan_{direction}")()
        self._set_result(True, "PAN", direction)

    def _ensure_home(self) -> bool:
        result = self._newspaper_actor().go_home()
        self._store_action(result)
        return result.success

    def _harvest(self, limit: int) -> None:
        if not self._ensure_home():
            return
        result = self.farming_cls(self.device, load_config()).harvest_ready_fields(
            limit=limit, should_stop=self._stopped
        )
        self._store_action(result)

    def _plant(self, crop: str, limit: int) -> None:
        if not self._ensure_home():
            return
        result = self.farming_cls(self.device, load_config()).plant_empty_fields(
            crop=crop, limit=limit, should_stop=self._stopped
        )
        self._store_action(result)

    def _newspaper(
        self,
        limit: int,
        mode: str = "shop",
        reset_home: bool = True,
        until_done: bool = False,
    ) -> ActionResult:
        result = self._newspaper_actor().shop_from_newspaper(
            limit=limit,
            should_stop=self._stopped,
            mode=mode,
            reset_home=reset_home,
            until_done=until_done,
        )
        self._store_action(result)
        return result

    def _loop(self, payload: dict) -> None:
        harvest = bool(payload.get("harvest", True))
        plant = bool(payload.get("plant", True))
        newspaper = bool(payload.get("newspaper", False))
        crop = str(payload.get("crop") or "wheat")
        limit = _limit(payload)
        news_mode = _news_mode(payload)
        rest_min = _loop_rest_min(payload)
        while not self._stopped():
            if not self._ensure_home():
                break
            if harvest:
                result = self.farming_cls(self.device, load_config()).harvest_ready_fields(
                    limit=limit, should_stop=self._stopped
                )
                self._store_action(result)
            if self._stopped():
                break
            if plant:
                result = self.farming_cls(self.device, load_config()).plant_empty_fields(
                    crop=crop, limit=limit, should_stop=self._stopped
                )
                self._store_action(result)
            if self._stopped():
                break
            if newspaper:
                result = self._newspaper(
                    limit, news_mode, reset_home=False, until_done=True
                )
                if self._stopped():
                    break
                if _newspaper_cycle_done(result):
                    self._rest_minutes(rest_min)
                else:
                    log.info(
                        "LOOP skip rest — "
                        f"{result.action.type} {result.error or result.action.target}"
                    )
                    if self.loop_pause_s:
                        time.sleep(self.loop_pause_s)
            elif self.loop_pause_s:
                time.sleep(self.loop_pause_s)
        self._set_result(True, "STOP" if self._stopped() else "LOOP", "done")

    def _rest_minutes(self, minutes: float) -> None:
        seconds = max(0.0, float(minutes) * 60.0)
        if seconds <= 0:
            return
        log.info(f"LOOP rest {minutes:g} min")
        self._set_result(True, "REST", f"{minutes:g} min")
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self._stopped():
            time.sleep(min(1.0, end - time.monotonic()))

    def _store_action(self, result: ActionResult) -> None:
        with self.lock:
            self.last_result = {
                "ok": result.success,
                "action": result.action.type,
                "target": result.action.target,
                "error": result.error,
            }
        log.info(
            f"{result.action.type} {'ok' if result.success else 'FAIL'} "
            f"{result.action.target} {result.error or ''}".strip()
        )

    def _set_result(self, ok: bool, action: str, target: str, error: str | None = None) -> None:
        with self.lock:
            self.last_result = {
                "ok": ok,
                "action": action,
                "target": target,
                "error": error,
            }

    def _attach_logs(self) -> None:
        logger = logging.getLogger("farmbot")
        for existing in list(logger.handlers):
            if isinstance(existing, _DequeHandler):
                logger.removeHandler(existing)
        handler = _DequeHandler(self.logs, self.lock)
        handler.setFormatter(ActionFormatter())
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)
        self._log_handler = handler


class _DequeHandler(logging.Handler):
    def __init__(self, lines: deque[str], lock: threading.Lock):
        super().__init__()
        self.lines = lines
        self._lock = lock

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._lock:
            self.lines.append(msg)


def _limit(payload: dict) -> int:
    try:
        value = int(payload.get("limit") or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(MAX_LIMIT, value))


def _news_mode(payload: dict) -> str:
    kind = str(payload.get("news_mode") or payload.get("mode") or "follow").strip().lower()
    if kind in {"browse", "xem"}:
        return "browse"
    if kind in {"follow", "shop", "buy", ""}:
        return "follow"
    if kind == "sweep":
        return "sweep"
    raise ValueError("news_mode must be follow, sweep, or browse")


def _loop_rest_min(payload: dict) -> float:
    raw = payload.get("loop_rest_min", payload.get("rest_min"))
    if raw is None or raw == "":
        return float(load_config().loop_rest_min)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(load_config().loop_rest_min)
    return max(0.0, min(180.0, value))


def _newspaper_cycle_done(result: ActionResult) -> bool:
    """True only after every planned shop was visited — not column/open failures."""
    if not result.success:
        return False
    if result.action.type == "VISIT_SHOP" and result.action.target == "done":
        return True
    return result.error == "all shops done"


def _gesture_step(kind: str, payload: dict) -> dict:
    if kind == "tap":
        return {"type": "tap", "x": int(payload.get("x") or 0), "y": int(payload.get("y") or 0)}
    if kind == "swipe":
        return {
            "type": "swipe",
            "x1": int(payload.get("x1") or 0),
            "y1": int(payload.get("y1") or 0),
            "x2": int(payload.get("x2") or 0),
            "y2": int(payload.get("y2") or 0),
            "duration_ms": max(80, int(payload.get("duration_ms") or 320)),
        }
    if kind in {"back", "home"}:
        return {"type": kind}
    raise ValueError(f"unknown gesture '{kind}'")


RUNTIME = BotRuntime()
