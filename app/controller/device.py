from __future__ import annotations

import io
import re
import struct
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.config import AppConfig, SCREENSHOT_DIR
from app.controller.adb import (
    AdbClient,
    DeviceError,
    DeviceTimeout,
    discover_bluestacks_ports,
)
from app.controller.input import InputController
from app.storage.logger import get_logger

log = get_logger("DEVICE")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"IEND\xaeB`\x82"
# Raw screencap: width, height, format (+ colorspace on Android 9+), then pixels.
RAW_HEADER_SIZES = (12, 16)
# A frame normally lands in ~0.6s; a hung adb gets cut off well before the old
# 15s and retried after a reconnect.
SHOT_TIMEOUT_S = 8.0
TRANSIENT_RETRIES = 2
# adb errors that prove the command never reached the device, so resending an
# input cannot make it happen twice.
NOT_DELIVERED = (
    "not found",
    "offline",
    "no devices",
    "unauthorized",
    "cannot connect",
    "connection refused",
)
RAW_TO_BGR = {
    1: cv2.COLOR_RGBA2BGR,  # RGBA_8888
    2: cv2.COLOR_RGBA2BGR,  # RGBX_8888
    5: cv2.COLOR_BGRA2BGR,  # BGRA_8888
}


class RawFormatError(DeviceError):
    """The output is not a raw screencap this code understands (not a hiccup)."""


class DeviceController:
    """High-level device API used by later milestones. No game decisions here."""

    def __init__(self, config: AppConfig | None = None, adb: AdbClient | None = None):
        self.config = config or AppConfig()
        self.adb = adb or AdbClient(self.config.adb_bin)
        self.serial = ""
        self.input: InputController | None = None
        # Optional app.actions.timing.LoopTiming; duck-typed to keep this layer
        # free of action imports.
        self.timing = None
        # Flipped off for the session the first time raw output does not parse.
        self._raw_screencap = bool(getattr(self.config, "screencap_raw", True))

    def connect(self, host: str | None = None, port: int | None = None) -> str:
        host = host or self.config.adb_host
        preferred = port if port is not None else self.config.adb_port
        ports = discover_bluestacks_ports()
        if preferred:
            ports = [preferred] + [p for p in ports if p != preferred]
        errors: list[str] = []
        for candidate in ports:
            serial = f"{host}:{candidate}"
            try:
                self.adb.connect(serial)
                self.adb.wait_for_device(serial, timeout=6)
                self.serial = serial
                self.input = InputController(self.adb, serial)
                self.config.adb_port = candidate
                log.info(f"connected {serial}")
                return serial
            except DeviceError as exc:
                errors.append(f"{serial} → {exc}")
        raise DeviceError(
            "Không kết nối được emulator. Bật ADB trong BlueStacks → Settings → Advanced.\n"
            + "\n".join(errors[:6])
        )

    def _require_input(self) -> InputController:
        if not self.serial or self.input is None:
            self.connect()
        assert self.input is not None
        return self.input

    def screenshot(self) -> np.ndarray:
        """Current frame as a BGR array, decoded once for every detector.

        Raw screencap skips the PNG encode on the device and the decode here,
        which is most of a frame's cost. `screencap -p` stays as the fallback
        for devices whose raw output this code cannot read. A capture is
        read-only, so a hiccup (timeout, lost device, cut-off frame) is retried
        after reconnecting instead of ending the job.
        """
        for attempt in range(TRANSIENT_RETRIES + 1):
            try:
                return self._screenshot_once()
            except DeviceError as exc:
                if attempt == TRANSIENT_RETRIES:
                    raise
                self._reconnect(exc)
                time.sleep(0.5 * (attempt + 1))
        raise AssertionError("unreachable")

    def _screenshot_once(self) -> np.ndarray:
        serial = self.serial or self.connect()
        frame = None
        if self._raw_screencap:
            raw = self.adb.run(
                ["-s", serial, "exec-out", "screencap"], timeout=SHOT_TIMEOUT_S
            )
            started = time.perf_counter()
            try:
                frame = _raw_to_bgr(raw)
            except RawFormatError as exc:
                log.info(f"raw screencap unusable ({exc}) — falling back to PNG")
                self._raw_screencap = False
        if frame is None:
            raw = self.adb.run(
                ["-s", serial, "exec-out", "screencap", "-p"], timeout=SHOT_TIMEOUT_S
            )
            started = time.perf_counter()
            frame = _png_to_bgr(_valid_png(raw))
        decode_s = time.perf_counter() - started
        if self.timing is not None:
            self.timing.add_decode(decode_s)
        # Debug level: one line per shot floods the web log panel.
        log.debug(
            f"screenshot {frame.shape[1]}x{frame.shape[0]} "
            f"{len(raw)} bytes decode={decode_s * 1000:.0f}ms"
        )
        return frame

    def _reconnect(self, exc: Exception) -> None:
        """Re-attach the same emulator after an adb hiccup; search ports only
        if it is gone (an adb server restart drops 127.0.0.1:5555)."""
        serial = self.serial
        log.info(f"ADB hiccup ({exc}) — reconnecting {serial or 'emulator'}")
        if serial:
            try:
                self.adb.connect(serial)
                self.adb.wait_for_device(serial, timeout=6)
                return
            except DeviceError as again:
                log.info(f"reconnect {serial} failed ({again}) — searching ports")
        self.serial = ""
        self.input = None
        self.connect()

    def _send_input(self, what: str, send) -> None:
        """Run one input command, surviving an adb hiccup.

        A timed-out tap may still have landed, and tapping twice could buy a
        second crate, so it is never resent. Only an error proving the device
        never got the command is retried.
        """
        try:
            send(self._require_input())
        except DeviceTimeout as exc:
            log.info(f"{what} timed out — not resent, the game may have taken it")
            self._reconnect(exc)
        except DeviceError as exc:
            if not any(key in str(exc).lower() for key in NOT_DELIVERED):
                raise
            self._reconnect(exc)
            send(self._require_input())

    def save_screenshot(self, path: Path | None = None) -> Path:
        png = frame_png_bytes(self.screenshot())
        target = path or SCREENSHOT_DIR / f"capture_{_stamp()}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
        log.info(f"saved {target}")
        return target

    def tap(self, x: int, y: int) -> None:
        log.info(f"tap {x},{y}")
        self._send_input("tap", lambda ctl: ctl.tap(x, y))

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration: int | None = None
    ) -> None:
        ms = duration if duration is not None else self.config.swipe_duration_ms
        log.info(f"swipe {x1},{y1} -> {x2},{y2} ({ms}ms)")
        self._send_input("swipe", lambda ctl: ctl.swipe(x1, y1, x2, y2, ms))

    def back(self) -> None:
        log.info("back")
        self._send_input("back", lambda ctl: ctl.back())

    def home(self) -> None:
        log.info("home")
        self._send_input("home", lambda ctl: ctl.home())

    def force_stop(self, package: str | None = None) -> None:
        pkg = package or self.config.package
        log.info(f"force-stop {pkg}")
        self._send_input("force-stop", lambda ctl: ctl.force_stop(pkg))

    def foreground_package(self) -> str:
        return self._require_input().foreground_package()

    def launch_app(self, package: str | None = None) -> None:
        pkg = package or self.config.package
        log.info(f"launch {pkg}")
        self._require_input().launch_app(pkg)

    def resolution(self) -> tuple[int, int]:
        serial = self.serial or self.connect()
        out = self.adb.run(["-s", serial, "shell", "wm", "size"]).decode(
            "utf-8", errors="ignore"
        )
        match = re.search(r"(\d+)\s*x\s*(\d+)", out)
        if match:
            return int(match.group(1)), int(match.group(2))
        height, width = self.screenshot().shape[:2]
        return width, height


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def frame_png_bytes(frame) -> bytes:
    """PNG bytes for a frame from screenshot(); bytes pass through untouched."""
    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)
    ok, buf = cv2.imencode(".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not ok:
        raise DeviceError("Không encode được PNG")
    return buf.tobytes()


def _raw_to_bgr(raw: bytes) -> np.ndarray:
    if len(raw) < RAW_HEADER_SIZES[0]:
        raise DeviceError("raw screencap quá ngắn")
    width, height, fmt = struct.unpack_from("<III", raw, 0)
    code = RAW_TO_BGR.get(fmt)
    if code is None or not (0 < width <= 8192 and 0 < height <= 8192):
        raise RawFormatError(f"raw screencap header {width}x{height} format {fmt}")
    pixels = width * height * 4
    header = len(raw) - pixels
    if header < RAW_HEADER_SIZES[0]:
        # A sane header with too few pixels behind it: the transfer was cut off.
        raise DeviceError(f"raw screencap bị cụt ({len(raw)} bytes)")
    if header not in RAW_HEADER_SIZES:
        raise RawFormatError(f"raw screencap sai kích thước ({len(raw)} bytes)")
    pixels_view = np.frombuffer(raw, dtype=np.uint8, count=pixels, offset=header)
    return cv2.cvtColor(pixels_view.reshape(height, width, 4), code)


def _png_to_bgr(png: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise DeviceError("Không decode được PNG từ screencap")
    return image


def _valid_png(raw: bytes) -> bytes:
    if not raw:
        raise DeviceError("ADB trả về ảnh trống")
    # Magic + IEND is enough to trust screencap output. Decoding every frame
    # here only to throw the pixels away doubles the cost of a screenshot.
    if raw.startswith(PNG_MAGIC) and raw.endswith(PNG_IEND):
        return raw
    for candidate in (raw, raw.replace(b"\r\n", b"\n") if b"\r\n" in raw else b""):
        if not candidate:
            continue
        try:
            image = Image.open(io.BytesIO(candidate))
            image.load()
            if candidate.startswith(PNG_MAGIC):
                return candidate
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        except OSError:
            continue
    raise DeviceError("Không đọc được PNG từ screencap")
