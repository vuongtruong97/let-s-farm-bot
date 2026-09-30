"""Background bot runner used by the web UI. One job at a time."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from app.actions.farming import ActionResult, FarmingActions
from app.actions.game import restart_game
from app.actions.shop import NewspaperActions
from app.config import game_package, load_config
from app.controller.camera import CameraManager
from app.controller.adb import DeviceError
from app.controller.device import DeviceController, frame_jpeg_bytes, frame_png_bytes
from app.storage.logger import ActionFormatter, get_logger
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
        "restart_game",
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
MAX_LOGS = 80
# An adb hiccup that outlives the device's own retries ends one loop round,
# not the loop: wait, then start the round over from home. Only this many in a
# row (the emulator is really gone) stop it.
LOOP_ADB_FAILURES = 3
LOOP_ADB_WAIT_S = 10.0
# Rounds in a row that could not get back to the farm before the game is
# restarted, and how many restarts an hour are allowed before the loop gives up
# (a game that will not come back needs a person, not a restart loop).
LOOP_STUCK_ROUNDS = 2
LOOP_MAX_RESTARTS_PER_HOUR = 3
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
        self.skip_rest_event = threading.Event()
        self.device = None
        self.camera: CameraManager | None = None
        self.status = "idle"
        self.job: str | None = None
        self.serial = ""
        self.screen: str | None = None
        self.last_error: str | None = None
        self.last_result: dict | None = None
        # Wall clock (time.time()) so the page can show elapsed time and the
        # rest countdown; the snapshot carries "now" to cancel clock skew.
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.rounds = 0
        self.phase: str | None = None
        self.rest_until: float | None = None
        self.rest_s: float | None = None
        # Whatever device.screenshot() returned; PNG/JPEG bytes are made on
        # demand, once per frame however many pages ask.
        self.last_frame = None
        self._frame_png: bytes | None = None
        self._frame_jpeg: bytes | None = None
        # Bumped per screenshot so the page refetches the frame only when new.
        self.frame_seq = 0
        self.frame_at: float | None = None
        self.logs: deque[str] = deque(maxlen=MAX_LOGS)
        self._news = None
        self._hooked = False
        self._log_handler: logging.Handler | None = None
        self._attach_logs()

    @property
    def log_seq(self) -> int:
        """Log lines written so far; the page asks for logs only when it moved."""
        handler = self._log_handler
        return getattr(handler, "seq", 0)

    def snapshot(self, known_log_seq: str | None = None) -> dict:
        """Run state for the page. The log lines come along unless the caller
        already holds them (known_log_seq equals the current log_seq)."""
        with self.lock:
            snap = {
                "status": self.status,
                "job": self.job,
                "stopping": self.status == "running" and self.stop_event.is_set(),
                "connected": bool(self.serial),
                "serial": self.serial,
                "screen": self.screen,
                "last_error": self.last_error,
                "last_result": self.last_result,
                "has_frame": self.last_frame is not None,
                "frame_seq": self.frame_seq,
                "frame_at": self.frame_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "rounds": self.rounds,
                "phase": self.phase,
                "rest_until": self.rest_until,
                "rest_s": self.rest_s,
                "now": time.time(),
                "log_seq": self.log_seq,
                "timing": self._timing_snap(),
            }
            if known_log_seq != str(snap["log_seq"]):
                snap["logs"] = list(self.logs)
            return snap

    def _timing_snap(self) -> dict | None:
        """Per-step wall clock of the newspaper shop loop, for the live table."""
        timing = getattr(self._news, "timing", None)
        if timing is None:
            return None
        return timing.snapshot()

    def frame_png(self) -> bytes | None:
        """Full-size PNG of the last screenshot (the "open original" link)."""
        return self._encoded_frame("_frame_png", frame_png_bytes)

    def frame_jpeg(self) -> bytes | None:
        """Small JPEG of the last screenshot, what the live panel shows."""
        return self._encoded_frame("_frame_jpeg", frame_jpeg_bytes)

    def _encoded_frame(self, slot: str, encode) -> bytes | None:
        # Encode outside the lock: the bot's screenshot hook takes that lock,
        # and a 1920x1080 PNG costs ~120 ms the bot would sit waiting.
        with self.lock:
            frame, cached = self.last_frame, getattr(self, slot)
        if frame is None or cached is not None:
            return cached if frame is not None else None
        data = encode(frame)
        with self.lock:
            if self.last_frame is frame:
                setattr(self, slot, data)
        return data

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
        if job in {"newspaper", "loop"}:
            _news_mode(payload)
        with self.lock:
            if self.status == "running":
                raise BusyError("bot is running")
            self.stop_event.clear()
            self.skip_rest_event.clear()
            self.status = "running"
            self.job = job
            self.last_error = None
            self.started_at = time.time()
            self.finished_at = None
            self.rounds = 0
            self.phase = None
            self.rest_until = None
            self.rest_s = None
        threading.Thread(target=self._worker, args=(job, payload), daemon=True).start()
        return self.snapshot()

    def stop(self) -> dict:
        self.stop_event.set()
        log.info("stop requested")
        return self.snapshot()

    def skip_rest(self) -> dict:
        """End the rest between loop rounds now; the next round starts at once."""
        with self.lock:
            resting = self.rest_until is not None
        if resting:
            self.skip_rest_event.set()
            log.info("LOOP rest skipped from the web")
        return self.snapshot()

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
                self._end_run()
            return
        with self.lock:
            if self.status == "running":
                self.status = "idle"
            self.job = None
            self._end_run()

    def _end_run(self) -> None:
        # Caller holds the lock.
        self.finished_at = time.time()
        self.phase = None
        self.rest_until = None
        self.rest_s = None

    def _set_phase(self, phase: str | None) -> None:
        with self.lock:
            self.phase = phase

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
        elif job == "restart_game":
            self._restart_game()
        elif job in NEWS_STEP_METHODS:
            self._news_step(job, payload)

    def _newspaper_actor(self):
        cfg = load_config()
        if self._news is None or getattr(self._news, "device", None) is not self.device:
            self._news = self.newspaper_cls(self.device, cfg)
        else:
            self._news.config = cfg
            matcher = getattr(self._news, "matcher", None)
            if matcher is not None:
                matcher.threshold = cfg.template_threshold
                # Re-decoding every template PNG on each job is pure overhead.
                if hasattr(matcher, "reload_if_changed"):
                    matcher.reload_if_changed()
                elif hasattr(matcher, "reload"):
                    matcher.reload()
            news_det = getattr(self._news, "news", None)
            if news_det is not None:
                news_det.buy_threshold = cfg.buy_threshold
                news_det.news_threshold = cfg.news_threshold
            if hasattr(self._news, "wait_s"):
                self._news.wait_s = cfg.action_wait_s
            if hasattr(self._news, "buy_wait_s"):
                self._news.buy_wait_s = cfg.buy_wait_s
            if hasattr(self._news, "visit_wait_s"):
                self._news.visit_wait_s = cfg.visit_wait_s
            if hasattr(self._news, "poll_interval_s"):
                self._news.poll_interval_s = cfg.poll_interval_s
            if hasattr(self._news, "stall_swipe_ms"):
                self._news.stall_swipe_ms = cfg.stall_swipe_ms
            if hasattr(self._news, "wishlist"):
                from app.storage.botdata import active_wishlist

                self._news.wishlist = active_wishlist()
        return self._news

    def _farming_actor(self):
        """Farming borrows the newspaper actor's matcher and screen detector.

        A fresh TemplateMatcher decodes every PNG in data/templates, and the
        loop builds a farming actor per harvest and per plant.
        """
        news = self._newspaper_actor()
        shared = {}
        for name in ("matcher", "screens"):
            value = getattr(news, name, None)
            if value is not None:
                shared[name] = value
        return self.farming_cls(self.device, load_config(), **shared)

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
                self._frame_png = None
                self._frame_jpeg = None
                self.frame_seq += 1
                self.frame_at = time.time()
            return png

        self.device.screenshot = hooked
        self._hooked = True

    def _detect(self) -> None:
        png = self.device.screenshot()
        result = ScreenDetector().detect(png, full=True)
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
        result = self._farming_actor().harvest_ready_fields(
            limit=limit, should_stop=self._stopped
        )
        self._store_action(result)

    def _plant(self, crop: str, limit: int) -> None:
        if not self._ensure_home():
            return
        result = self._farming_actor().plant_empty_fields(
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
        failures = 0
        stuck = 0
        restarts: list[float] = []
        while not self._stopped():
            try:
                missing = self._game_not_in_front()
                if missing:
                    self._recover_game(missing, restarts)
                    stuck = 0
                    continue
                outcome = self._loop_round(
                    harvest, plant, newspaper, crop, limit, news_mode, rest_min
                )
                failures = 0
                if outcome == "stop":
                    break
                if outcome == "stuck":
                    stuck += 1
                    log.info(f"LOOP not back on the farm ({stuck}/{LOOP_STUCK_ROUNDS})")
                    if stuck >= LOOP_STUCK_ROUNDS:
                        self._recover_game(
                            f"{stuck} rounds without reaching the farm", restarts
                        )
                        stuck = 0
                    elif self.loop_pause_s:
                        self._wait_unless_stopped(self.loop_pause_s)
                else:
                    stuck = 0
            except DeviceError as exc:
                failures += 1
                if failures >= LOOP_ADB_FAILURES:
                    # Reconnects already failed inside the device; one restart
                    # is the last thing worth trying before giving up.
                    self._recover_game(f"adb failed {failures} times: {exc}", restarts)
                    failures = 0
                    continue
                log.info(
                    f"LOOP adb error {failures}/{LOOP_ADB_FAILURES} ({exc}) — "
                    f"retrying from home in {LOOP_ADB_WAIT_S:g}s"
                )
                self._wait_unless_stopped(LOOP_ADB_WAIT_S)
        self._set_result(True, "STOP" if self._stopped() else "LOOP", "done")

    def _loop_round(
        self,
        harvest: bool,
        plant: bool,
        newspaper: bool,
        crop: str,
        limit: int,
        news_mode: str,
        rest_min: float,
    ) -> str:
        """One pass of the loop: "ok", "stuck" (the farm never came back) or
        "stop"."""
        self._set_phase("home")
        if not self._ensure_home():
            return "stop" if self._stopped() else "stuck"
        if harvest:
            self._set_phase("harvest")
            result = self._farming_actor().harvest_ready_fields(
                limit=limit, should_stop=self._stopped
            )
            self._store_action(result)
        if self._stopped():
            return "stop"
        if plant:
            self._set_phase("plant")
            result = self._farming_actor().plant_empty_fields(
                crop=crop, limit=limit, should_stop=self._stopped
            )
            self._store_action(result)
        if self._stopped():
            return "stop"
        if newspaper:
            self._set_phase("newspaper")
            result = self._newspaper(limit, news_mode, reset_home=False, until_done=True)
            if self._stopped():
                return "stop"
            self._round_done()
            if _newspaper_cycle_done(result):
                self._rest_minutes(rest_min)
            else:
                log.info(
                    "LOOP skip rest — "
                    f"{result.action.type} {result.error or result.action.target}"
                )
                if self.loop_pause_s:
                    time.sleep(self.loop_pause_s)
        else:
            self._round_done()
            if self.loop_pause_s:
                time.sleep(self.loop_pause_s)
        return "ok"

    def _round_done(self) -> None:
        with self.lock:
            self.rounds += 1
            self.phase = None

    def _game_not_in_front(self) -> str | None:
        """Why the game needs restarting before this round, or None.

        HUD taps on the Android launcher (the game crashed) or on another app
        would hit whatever sits there, so this is checked before any tap.
        """
        probe = getattr(self.device, "foreground_package", None)
        if probe is None:
            return None
        package = game_package(load_config())
        front = probe()
        if not front or front == package:
            return None
        return f"{front} is in front, not {package}"

    def _recover_game(self, reason: str, restarts: list[float]) -> None:
        now = time.monotonic()
        restarts[:] = [t for t in restarts if now - t < 3600.0]
        if len(restarts) >= LOOP_MAX_RESTARTS_PER_HOUR:
            raise RuntimeError(
                f"game restarted {len(restarts)} times in the last hour — "
                f"stopping ({reason})"
            )
        restarts.append(now)
        log.info(f"LOOP restart game ({reason})")
        self._restart_game()

    def _restart_game(self) -> ActionResult:
        news = self._newspaper_actor()
        screens = getattr(news, "screens", None) or ScreenDetector()
        result = restart_game(
            self.device,
            screens,
            game_package(load_config()),
            should_stop=self._stopped,
        )
        if hasattr(news, "reset_loop_state"):
            news.reset_loop_state()
        if self.camera is not None:
            self.camera.clear_history()
        self._store_action(result)
        return result

    def _wait_unless_stopped(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < end and not self._stopped():
            time.sleep(min(0.5, end - time.monotonic()))

    def _rest_minutes(self, minutes: float) -> None:
        seconds = max(0.0, float(minutes) * 60.0)
        if seconds <= 0:
            return
        log.info(f"LOOP rest {minutes:g} min")
        self._set_result(True, "REST", f"{minutes:g} min")
        self.skip_rest_event.clear()
        with self.lock:
            self.phase = "rest"
            self.rest_until = time.time() + seconds
            self.rest_s = seconds
        end = time.monotonic() + seconds
        try:
            while (
                time.monotonic() < end
                and not self._stopped()
                and not self.skip_rest_event.is_set()
            ):
                time.sleep(max(0.0, min(1.0, end - time.monotonic())))
        finally:
            self.skip_rest_event.clear()
            with self.lock:
                self.phase = None
                self.rest_until = None
                self.rest_s = None

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
        # Lines written so far; the page skips the log when this has not moved.
        self.seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._lock:
            self.lines.append(msg)
            self.seq += 1


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


RUNTIME = BotRuntime()
