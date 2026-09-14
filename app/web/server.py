"""Local web UI for config, wishlist item crops, and seed data."""

from __future__ import annotations

import json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.config import AppConfig, load_config, save_config
from app.storage import botdata
from app.storage import macros as macrostore
from app.web.runtime import BusyError, RUNTIME

STATIC_DIR = Path(__file__).resolve().parent / "static"
SPA_PAGES = {
    "/",
    "/index.html",
    "/control",
    "/macro",
    "/wishlist",
    "/library",
    "/config",
    "/crops",
    "/templates",
    "/dev",
    "/develop",
}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48721
MAX_BODY = 2_500_000

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class BotWebHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        print(f"[WEB] {self.address_string()} {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        spa = path.rstrip("/") or "/"
        if spa in SPA_PAGES or path == "/index.html":
            self._send_file(STATIC_DIR / "index.html")
            return
        if path.startswith("/static/"):
            self._send_static(path[len("/static/") :])
            return
        if path == "/api/state":
            self._send_json(200, _state())
            return
        if path == "/api/macros":
            self._send_json(200, {"macros": macrostore.list_macros()})
            return
        if path == "/api/frame.png":
            frame = RUNTIME.frame_png()
            if not frame:
                self._send_json(404, {"error": "no screenshot yet"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            return
        if path.startswith("/api/templates/") and path.endswith(".png"):
            name = path.rsplit("/", 1)[-1]
            try:
                file_path = botdata.template_png(name)
            except ValueError:
                self._send_json(400, {"error": "bad template name"})
                return
            if not file_path.is_file():
                self._send_json(404, {"error": "not found"})
                return
            self._send_file(file_path, "image/png")
            return
        if path.startswith("/api/library/") and path.endswith(".png"):
            name = path.rsplit("/", 1)[-1]
            try:
                file_path = botdata.library_png(name)
            except ValueError:
                self._send_json(400, {"error": "bad library id"})
                return
            if not file_path.is_file():
                self._send_json(404, {"error": "not found"})
                return
            self._send_file(file_path, "image/png")
            return
        self._send_json(
            404,
            {"error": f"Không có {path}. Đóng tab web cũ rồi mở đúng cổng in trên terminal."},
        )

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            try:
                payload = self._read_json()
                saved = _save_config(payload)
            except (ValueError, json.JSONDecodeError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"config": saved})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if parsed.path == "/api/stop":
            RUNTIME.stop()
            self._send_json(200, _state())
            return
        if parsed.path == "/api/run":
            try:
                RUNTIME.start(str(payload.get("action") or ""), payload)
            except BusyError as exc:
                self._send_json(409, {"error": str(exc), **_state()})
                return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, _state())
            return
        if parsed.path == "/api/macros":
            try:
                saved = macrostore.save_macro(payload)
            except (ValueError, TypeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"id": saved.stem, **_state()})
            return
        if parsed.path == "/api/macros/record/start":
            try:
                RUNTIME.start_record(str(payload.get("name") or ""))
            except BusyError as exc:
                self._send_json(409, {"error": str(exc), **_state()})
                return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, _state())
            return
        if parsed.path == "/api/macros/record/stop":
            try:
                RUNTIME.stop_record()
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, _state())
            return
        if parsed.path == "/api/macros/gesture":
            try:
                RUNTIME.apply_gesture(payload)
            except BusyError as exc:
                self._send_json(409, {"error": str(exc), **_state()})
                return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, _state())
            return
        try:
            if parsed.path == "/api/items":
                if not (
                    payload.get("image")
                    or payload.get("news_image")
                    or payload.get("shop_library")
                    or payload.get("news_library")
                ):
                    raise ValueError("shop image or newspaper image required")
                row = botdata.upsert_item(
                    str(payload.get("id") or ""),
                    image_b64=payload.get("image"),
                    news_image_b64=payload.get("news_image"),
                    shop_library=payload.get("shop_library"),
                    news_library=payload.get("news_library"),
                    enabled=payload.get("enabled"),
                )
                self._send_json(200, {"item": row, **_state()})
                return
            if parsed.path == "/api/library":
                if not payload.get("image"):
                    raise ValueError("image required")
                row = botdata.save_library_png(
                    str(payload.get("kind") or "shop"),
                    botdata.decode_png(str(payload.get("image") or "")),
                    skip_similar=True,
                )
                self._send_json(200, {"saved": row, **_state()})
                return
            if parsed.path == "/api/crops":
                row = botdata.upsert_crop(
                    str(payload.get("id") or ""),
                    storage=str(payload.get("storage") or "silo"),
                    image_b64=payload.get("image"),
                )
                self._send_json(200, {"crop": row, **_state()})
                return
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(
            404,
            {"error": f"Không có {parsed.path}. Đóng tab web cũ rồi mở đúng cổng in trên terminal."},
        )

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            payload = self._read_json()
            if (
                len(parts) == 5
                and parts[:4] == ["api", "macros", "record", "steps"]
            ):
                RUNTIME.record_set_wait(int(parts[4]), int(payload.get("ms") or 0))
                self._send_json(200, _state())
                return
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "items":
                kwargs: dict = {}
                if payload.get("id"):
                    kwargs["new_id"] = str(payload["id"])
                if payload.get("image"):
                    kwargs["image_b64"] = payload["image"]
                if payload.get("news_image"):
                    kwargs["news_image_b64"] = payload["news_image"]
                if payload.get("shop_library"):
                    kwargs["shop_library"] = str(payload["shop_library"])
                if payload.get("news_library"):
                    kwargs["news_library"] = str(payload["news_library"])
                if "enabled" in payload:
                    kwargs["enabled"] = bool(payload["enabled"])
                row = botdata.update_item(unquote(parts[2]), **kwargs)
                self._send_json(200, {"item": row, **_state()})
                return
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if (
                len(parts) == 5
                and parts[:4] == ["api", "macros", "record", "steps"]
            ):
                RUNTIME.record_remove_step(int(parts[4]))
                self._send_json(200, _state())
                return
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "macros":
                macrostore.delete_macro(parts[2])
                self._send_json(200, _state())
                return
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "items":
                botdata.remove_item(parts[2], delete_image=True)
                self._send_json(200, _state())
                return
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "library":
                botdata.delete_library(unquote(parts[2]))
                self._send_json(200, _state())
                return
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "crops":
                botdata.remove_crop(parts[2], delete_image=False)
                self._send_json(200, _state())
                return
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(404, {"error": "not found"})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("invalid body")
        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, relative: str) -> None:
        name = Path(relative).name
        if name != relative.replace("\\", "/").split("/")[-1]:
            self._send_json(404, {"error": "not found"})
            return
        path = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in path.parents or not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        self._send_file(path)

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.is_file():
            self._send_json(404, {"error": "not found"})
            return
        data = path.read_bytes()
        mime = content_type or MIME.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _config_dict(config: AppConfig | None = None) -> dict:
    cfg = config or load_config()
    return {
        "adb_host": cfg.adb_host,
        "adb_port": cfg.adb_port,
        "adb_bin": cfg.adb_bin or "",
        "debug": cfg.debug,
        "allow_diamond_spending": False,
        "swipe_duration_ms": cfg.swipe_duration_ms,
        "package": cfg.package,
        "template_threshold": cfg.template_threshold,
        "buy_threshold": cfg.buy_threshold,
        "news_threshold": cfg.news_threshold,
    }


def _save_config(payload: dict) -> dict:
    current = load_config()
    port = payload.get("adb_port", current.adb_port)
    if port == "" or port is None:
        port = None
    else:
        port = int(port)
        if port < 1 or port > 65535:
            raise ValueError("adb_port out of range")
    threshold = float(payload.get("template_threshold", current.template_threshold))
    if not 0.5 <= threshold <= 0.90:
        raise ValueError("template_threshold must be 0.50–0.90")
    buy_threshold = float(payload.get("buy_threshold", current.buy_threshold))
    if not 0.5 <= buy_threshold <= 0.90:
        raise ValueError("buy_threshold must be 0.50–0.90")
    news_threshold = float(payload.get("news_threshold", current.news_threshold))
    if not 0.5 <= news_threshold <= 0.90:
        raise ValueError("news_threshold must be 0.50–0.90")
    swipe = int(payload.get("swipe_duration_ms", current.swipe_duration_ms))
    if swipe < 50 or swipe > 3000:
        raise ValueError("swipe_duration_ms must be 50–3000")
    updated = AppConfig(
        adb_host=str(payload.get("adb_host") or current.adb_host).strip() or "127.0.0.1",
        adb_port=port,
        adb_bin=(str(payload.get("adb_bin") or "").strip() or None),
        debug=bool(payload.get("debug", current.debug)),
        allow_diamond_spending=False,
        swipe_duration_ms=swipe,
        package=str(payload.get("package") or ""),
        template_threshold=threshold,
        buy_threshold=buy_threshold,
        news_threshold=news_threshold,
    )
    save_config(updated)
    return _config_dict(updated)


def _state() -> dict:
    return {
        "config": _config_dict(),
        "wishlist": botdata.wishlist_entries(),
        "crops": botdata.crop_entries(),
        "templates": botdata.list_templates(),
        "library": botdata.list_library(),
        "macros": macrostore.list_macros(),
        "run": RUNTIME.snapshot(),
    }


class BotHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    httpd = BotHTTPServer((host, port), BotWebHandler)
    print(f"Web UI  http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
