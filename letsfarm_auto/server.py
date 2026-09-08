from __future__ import annotations

import argparse
import asyncio
import base64
import threading
import time
from contextlib import asynccontextmanager
from collections import deque
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from letsfarm_auto.bot import FarmBot
from letsfarm_auto.demo_farm import DemoFarm
from letsfarm_auto.device import DeviceError, scan_and_connect
from letsfarm_auto.models import BotConfig, DeviceKind, RunMode
from letsfarm_auto.store import load_config, save_config
from letsfarm_auto.vision import encode_jpeg

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 48721


class PointIn(BaseModel):
    x: int
    y: int


class RectIn(BaseModel):
    x: int
    y: int
    w: int
    h: int


class ConfigPatch(BaseModel):
    device: DeviceKind | None = None
    adb_host: str | None = None
    adb_port: int | None = None
    rows: int | None = Field(default=None, ge=1, le=20)
    cols: int | None = Field(default=None, ge=1, le=24)
    run_mode: RunMode | None = None
    match_threshold: float | None = None
    cycle_pause_s: float | None = None
    jitter_px: int | None = None
    demo_grow_s: float | None = None
    farm: RectIn | None = None
    seed_button: PointIn | None = None
    deselect_point: PointIn | None = None


class ConnectIn(BaseModel):
    port: int | None = None
    host: str = "127.0.0.1"


class AppRuntime:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config = load_config()
        self.farm = DemoFarm(config=self.config)
        self.device: Any = self.farm
        self.bot = FarmBot(self.device, self.config)
        self.logs: deque[dict[str, Any]] = deque(maxlen=200)
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.bot.on_log = self._on_log
        self.bot.on_frame = self._on_frame
        self._on_log(
            "Bot tự tìm ruộng trên ảnh. Kết nối BlueStacks khi chạy trên Windows.",
            "info",
        )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            kind = "demo" if isinstance(self.device, DemoFarm) else "adb"
            return {
                "connected": True,
                "device_name": getattr(self.device, "name", "unknown"),
                "kind": kind,
                "running": self.bot.running,
                "cycles": self.bot.cycles,
                "config": self.config.to_dict(),
                "stats": self.bot.last_stats,
                "logs": list(self.logs)[-40:],
            }

    def apply_patch(self, patch: ConfigPatch) -> BotConfig:
        with self.lock:
            data = self.config.to_dict()
            payload = patch.model_dump(exclude_none=True)
            if "farm" in payload:
                payload["farm"] = payload["farm"]
            if "seed_button" in payload:
                payload["seed_button"] = payload["seed_button"]
            data.update(payload)
            self.config = BotConfig.from_dict(data)
            self.bot.config = self.config
            if isinstance(self.device, DemoFarm):
                self.device.config = self.config
            save_config(self.config)
            return self.config

    def use_demo(self) -> None:
        with self.lock:
            self.bot.stop()
            self.config.device = DeviceKind.DEMO
            self.farm = DemoFarm(config=self.config)
            self.device = self.farm
            self.bot = FarmBot(self.device, self.config)
            self.bot.on_log = self._on_log
            self.bot.on_frame = self._on_frame
            save_config(self.config)
        self._on_log("Đã chuyển sang mô phỏng nông trại.", "info")

    def use_adb(self, host: str, port: int | None) -> str:
        device = scan_and_connect(host=host, preferred_port=port)
        with self.lock:
            self.bot.stop()
            self.config.device = DeviceKind.ADB
            self.config.adb_host = host
            self.config.adb_port = port or device.port
            self.config.adb_serial = device.serial
            self.device = device
            self.bot = FarmBot(self.device, self.config)
            self.bot.on_log = self._on_log
            self.bot.on_frame = self._on_frame
            save_config(self.config)
        self._on_log(f"Đã kết nối {device.name}.", "info")
        return device.name

    def screenshot_jpeg(self, overlay: bool = True) -> bytes:
        payload = self.bot.scan_once() if overlay else None
        frame = self.bot.last_frame if overlay else self.device.screenshot()
        if frame is None:
            frame = self.device.screenshot()
        _ = payload
        return encode_jpeg(frame)

    def _on_log(self, message: str, level: str = "info") -> None:
        item = {"t": time.strftime("%H:%M:%S"), "level": level, "message": message}
        self.logs.append(item)
        self._broadcast({"type": "log", **item})

    def _on_frame(self, frame: Any, stats: dict[str, Any]) -> None:
        jpeg = encode_jpeg(frame)
        self._broadcast(
            {
                "type": "frame",
                "jpeg": base64.b64encode(jpeg).decode("ascii"),
                "stats": stats,
                "running": self.bot.running,
                "cycles": self.bot.cycles,
            }
        )

    def _broadcast(self, message: dict[str, Any]) -> None:
        loop = self.loop
        if loop is None:
            return

        async def _send() -> None:
            stale: list[WebSocket] = []
            for ws in list(self.clients):
                try:
                    await ws.send_json(message)
                except Exception:
                    stale.append(ws)
            for ws in stale:
                self.clients.discard(ws)

        try:
            asyncio.run_coroutine_threadsafe(_send(), loop)
        except RuntimeError:
            pass


runtime = AppRuntime()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runtime.loop = asyncio.get_running_loop()
    yield


app = FastAPI(title="Let's Farm Auto", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/state")
async def state() -> dict[str, Any]:
    return runtime.snapshot()


@app.put("/api/config")
async def update_config(patch: ConfigPatch) -> dict[str, Any]:
    config = runtime.apply_patch(patch)
    return {"ok": True, "config": config.to_dict()}


@app.post("/api/demo")
async def enable_demo() -> dict[str, Any]:
    runtime.use_demo()
    return runtime.snapshot()


@app.post("/api/demo/reset")
async def reset_demo() -> dict[str, Any]:
    if isinstance(runtime.device, DemoFarm):
        runtime.device.reset()
        runtime._on_log("Đã tạo lại nông trại mô phỏng.", "info")
    return runtime.snapshot()


@app.post("/api/connect")
async def connect(body: ConnectIn) -> dict[str, Any]:
    try:
        name = runtime.use_adb(body.host, body.port)
    except DeviceError as exc:
        return {"ok": False, "error": str(exc), **runtime.snapshot()}
    return {"ok": True, "device_name": name, **runtime.snapshot()}


@app.get("/api/screenshot")
async def screenshot(overlay: bool = True) -> Response:
    jpeg = runtime.screenshot_jpeg(overlay=overlay)
    return Response(content=jpeg, media_type="image/jpeg")


@app.post("/api/scan")
async def scan() -> dict[str, Any]:
    stats = runtime.bot.scan_once()
    return {"ok": True, **stats}


@app.post("/api/start")
async def start_bot() -> dict[str, Any]:
    runtime.bot.start()
    return runtime.snapshot()


@app.post("/api/stop")
async def stop_bot() -> dict[str, Any]:
    runtime.bot.stop()
    return runtime.snapshot()


@app.websocket("/ws")
async def ws_feed(ws: WebSocket) -> None:
    await ws.accept()
    runtime.clients.add(ws)
    await ws.send_json({"type": "state", **runtime.snapshot()})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        runtime.clients.discard(ws)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Let's Farm Auto cho BlueStacks")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--reload", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    uvicorn.run(
        "letsfarm_auto.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
