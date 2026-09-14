from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

from PIL import Image

from app.config import AppConfig, SCREENSHOT_DIR
from app.controller.adb import AdbClient, DeviceError, discover_bluestacks_ports
from app.controller.input import InputController
from app.storage.logger import get_logger

log = get_logger("DEVICE")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class DeviceController:
    """High-level device API used by later milestones. No game decisions here."""

    def __init__(self, config: AppConfig | None = None, adb: AdbClient | None = None):
        self.config = config or AppConfig()
        self.adb = adb or AdbClient(self.config.adb_bin)
        self.serial = ""
        self.input: InputController | None = None

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

    def screenshot(self) -> bytes:
        raw = self.adb.run(
            ["-s", self.serial or self.connect(), "exec-out", "screencap", "-p"],
            timeout=15,
        )
        png = _valid_png(raw)
        log.info(f"screenshot {len(png)} bytes")
        return png

    def save_screenshot(self, path: Path | None = None) -> Path:
        png = self.screenshot()
        target = path or SCREENSHOT_DIR / f"capture_{_stamp()}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
        log.info(f"saved {target}")
        return target

    def tap(self, x: int, y: int) -> None:
        log.info(f"tap {x},{y}")
        self._require_input().tap(x, y)

    def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration: int | None = None
    ) -> None:
        ms = duration if duration is not None else self.config.swipe_duration_ms
        log.info(f"swipe {x1},{y1} -> {x2},{y2} ({ms}ms)")
        self._require_input().swipe(x1, y1, x2, y2, ms)

    def back(self) -> None:
        log.info("back")
        self._require_input().back()

    def home(self) -> None:
        log.info("home")
        self._require_input().home()

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
        image = Image.open(io.BytesIO(self.screenshot()))
        return image.size


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _valid_png(raw: bytes) -> bytes:
    if not raw:
        raise DeviceError("ADB trả về ảnh trống")
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
