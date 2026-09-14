from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from app.storage.logger import get_logger

log = get_logger("ADB")


class DeviceError(RuntimeError):
    pass


BLUESTACKS_CONF_CANDIDATES = (
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "BlueStacks_nxt" / "bluestacks.conf",
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "BlueStacks" / "bluestacks.conf",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "BlueStacks_nxt" / "bluestacks.conf",
    Path(r"C:\Program Files\BlueStacks_msi5") / "bluestacks.conf",
)

ADB_EXE_CANDIDATES = (
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "BlueStacks_nxt" / "HD-Adb.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "BlueStacks" / "HD-Adb.exe",
    Path(r"C:\Program Files\BlueStacks_msi5") / "HD-Adb.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "BlueStacks_nxt"
    / "HD-Adb.exe",
)


def find_adb_executable(explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        raise DeviceError(f"Không thấy ADB tại {explicit}")
    for candidate in ADB_EXE_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    which = shutil.which("adb")
    if which:
        return which
    raise DeviceError(
        "Không tìm thấy ADB. Cài Android Platform Tools hoặc dùng HD-Adb.exe của BlueStacks."
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


class AdbClient:
    """Thin wrapper around the adb binary. No game logic here."""

    def __init__(self, adb_bin: str | None = None):
        self.adb_bin = find_adb_executable(adb_bin)

    def run(self, args: list[str], timeout: float = 12.0) -> bytes:
        cmd = [self.adb_bin, *args]
        log.debug(" ".join(cmd))
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
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

    def start_server(self) -> None:
        self.run(["start-server"], timeout=8)

    def devices(self) -> list[tuple[str, str]]:
        raw = self.run(["devices"]).decode("utf-8", errors="ignore")
        found: list[tuple[str, str]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("List of"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                found.append((parts[0], parts[1]))
        return found

    def connect(self, serial: str) -> str:
        self.start_server()
        out = self.run(["connect", serial], timeout=8).decode("utf-8", errors="ignore")
        return out

    def wait_for_device(self, serial: str, timeout: float = 8.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            states = {s: st for s, st in self.devices()}
            if states.get(serial) == "device":
                return
            time.sleep(0.2)
        raise DeviceError(f"Máy {serial} chưa sẵn sàng (adb devices).")
