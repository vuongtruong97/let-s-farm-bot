from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from letsfarm_auto.models import Point, Swipe, Tap


class Device(Protocol):
    name: str

    def screenshot(self) -> np.ndarray: ...
    def tap(self, action: Tap) -> None: ...
    def swipe(self, action: Swipe) -> None: ...
    def size(self) -> tuple[int, int]: ...


class DeviceError(RuntimeError):
    pass


BLUESTACKS_CONF_CANDIDATES = (
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "BlueStacks_nxt" / "bluestacks.conf",
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "BlueStacks" / "bluestacks.conf",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "BlueStacks_nxt" / "bluestacks.conf",
    Path(r"C:\Program Files\BlueStacks_msi5\bluestacks.conf"),
)

ADB_EXE_CANDIDATES = (
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "BlueStacks_nxt" / "HD-Adb.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "BlueStacks" / "HD-Adb.exe",
    Path(r"C:\Program Files\BlueStacks_msi5\HD-Adb.exe"),
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "BlueStacks_nxt"
    / "HD-Adb.exe",
)


def find_adb_executable() -> str:
    for candidate in ADB_EXE_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    which = shutil.which("adb")
    if which:
        return which
    raise DeviceError(
        "Không tìm thấy ADB. Cài Android Platform Tools hoặc dùng HD-Adb.exe đi kèm BlueStacks."
    )


def ports_from_bluestacks_conf(text: str) -> list[int]:
    found: list[int] = []
    for match in re.finditer(
        r"bst\.instance\.[^=\s]+\.status\.adb_port\s*=\s*\"?(\d+)\"?", text
    ):
        port = int(match.group(1))
        if port not in found:
            found.append(port)
    return found


def discover_bluestacks_ports() -> list[int]:
    ports: list[int] = []
    for path in BLUESTACKS_CONF_CANDIDATES:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for port in ports_from_bluestacks_conf(text):
            if port not in ports:
                ports.append(port)
    for fallback in (5555, 5556, 5565, 5575, 5585, 9999):
        if fallback not in ports:
            ports.append(fallback)
    return ports


def decode_png(raw: bytes) -> np.ndarray:
    if not raw:
        raise DeviceError("ADB trả về ảnh trống")
    cleaned = raw.replace(b"\r\n", b"\n")
    array = np.frombuffer(cleaned, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise DeviceError("Không đọc được PNG từ screencap")
    return image


class AdbDevice:
    def __init__(self, host: str = "127.0.0.1", port: int = 5555, adb_bin: str | None = None):
        self.host = host
        self.port = port
        self.serial = f"{host}:{port}"
        self.adb_bin = adb_bin or find_adb_executable()
        self.name = f"BlueStacks {self.serial}"

    def _run(self, args: list[str], timeout: float = 12.0, capture: bool = True) -> bytes:
        cmd = [self.adb_bin, *args]
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=capture,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise DeviceError(f"Không chạy được ADB: {self.adb_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DeviceError(f"ADB hết thời gian: {' '.join(args)}") from exc
        if completed.returncode != 0:
            err = (completed.stderr or b"").decode("utf-8", errors="ignore").strip()
            raise DeviceError(err or f"ADB lỗi ({completed.returncode}): {' '.join(args)}")
        return completed.stdout or b""

    def connect(self) -> str:
        self._run(["start-server"], timeout=8)
        out = self._run(["connect", self.serial], timeout=8).decode("utf-8", errors="ignore")
        devices = self._run(["devices"]).decode("utf-8", errors="ignore")
        if self.serial not in devices and "connected" not in out.lower():
            raise DeviceError(
                f"Chưa thấy máy {self.serial}. Bật ADB trong BlueStacks → Settings → Advanced."
            )
        if "offline" in devices and self.serial in devices:
            raise DeviceError(f"{self.serial} đang offline. Tắt/bật ADB trong BlueStacks rồi thử lại.")
        return devices

    def screenshot(self) -> np.ndarray:
        raw = self._run(["-s", self.serial, "exec-out", "screencap", "-p"], timeout=15)
        return decode_png(raw)

    def tap(self, action: Tap) -> None:
        self._run(
            [
                "-s",
                self.serial,
                "shell",
                "input",
                "swipe",
                str(action.point.x),
                str(action.point.y),
                str(action.point.x),
                str(action.point.y),
                str(max(20, action.hold_ms)),
            ]
        )

    def swipe(self, action: Swipe) -> None:
        self._run(
            [
                "-s",
                self.serial,
                "shell",
                "input",
                "swipe",
                str(action.start.x),
                str(action.start.y),
                str(action.end.x),
                str(action.end.y),
                str(max(80, action.duration_ms)),
            ]
        )

    def size(self) -> tuple[int, int]:
        out = self._run(["-s", self.serial, "shell", "wm", "size"]).decode("utf-8", errors="ignore")
        match = re.search(r"(\d+)\s*x\s*(\d+)", out)
        if not match:
            frame = self.screenshot()
            return frame.shape[1], frame.shape[0]
        return int(match.group(1)), int(match.group(2))


def try_connect(host: str, port: int, adb_bin: str | None = None) -> AdbDevice:
    device = AdbDevice(host=host, port=port, adb_bin=adb_bin)
    device.connect()
    return device


def scan_and_connect(host: str = "127.0.0.1", preferred_port: int | None = None) -> AdbDevice:
    adb_bin = find_adb_executable()
    ports = discover_bluestacks_ports()
    if preferred_port:
        ports = [preferred_port] + [p for p in ports if p != preferred_port]
    errors: list[str] = []
    for port in ports:
        try:
            return try_connect(host, port, adb_bin=adb_bin)
        except DeviceError as exc:
            errors.append(f"{host}:{port} → {exc}")
            time.sleep(0.05)
    raise DeviceError(
        "Không kết nối được BlueStacks. Bật Android Debug Bridge trong Settings → Advanced.\n"
        + "\n".join(errors[:6])
    )
