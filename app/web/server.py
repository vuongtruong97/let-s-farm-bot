"""Local web UI for config, wishlist item crops, and seed data."""

from __future__ import annotations

import json
import socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from app.config import AppConfig, load_config, save_config
from app.notify import TelegramError, detect_chat_id, send_test
from app.storage import botdata
from app.web.runtime import BusyError, RUNTIME

STATIC_DIR = Path(__file__).resolve().parent / "static"
SPA_PAGES = {
    "/",
    "/index.html",
    "/control",
    "/wishlist",
    "/library",
    "/config",
    "/crops",
    "/templates",
    "/dev",
    "/develop",
}
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 48721
MAX_BODY = 2_500_000

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
# Fonts never change under the same name; everything else is re-read so an
# edited page shows up on the next reload.
CACHED_SUFFIXES = {".woff2"}


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
        if path == "/api/share":
            host, port = self.server.server_address[:2]
            local_only = host not in {"0.0.0.0", "::"}
            urls = [] if local_only else [f"http://{ip}:{port}" for ip in _lan_ipv4()]
            self._send_json(200, {"urls": urls, "local_only": local_only, "port": port})
            return
        if path == "/api/status":
            self._send_json(200, _status(parse_qs(parsed.query)))
            return
        if path in ("/api/frame.png", "/api/frame.jpg"):
            # The live panel shows the small JPEG; the PNG is the full-size
            # original behind "open in a new tab".
            jpeg = path.endswith(".jpg")
            frame = RUNTIME.frame_jpeg() if jpeg else RUNTIME.frame_png()
            if not frame:
                self._send_json(404, {"error": "no screenshot yet"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg" if jpeg else "image/png")
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
        if path.startswith("/api/buy-proofs/") and path.endswith(".png"):
            name = path.rsplit("/", 1)[-1]
            try:
                file_path = botdata.buy_proof_png(name)
            except ValueError:
                self._send_json(400, {"error": "bad buy proof name"})
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
        if parsed.path == "/api/run-prefs":
            try:
                payload = self._read_json()
                saved = botdata.save_run_prefs(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"run_prefs": saved})
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
        if parsed.path == "/api/skip-rest":
            self._send_json(200, {"run": RUNTIME.skip_rest()})
            return
        if parsed.path == "/api/telegram/test":
            try:
                send_test(load_config())
            except TelegramError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True})
            return
        if parsed.path == "/api/telegram/chat":
            cfg = load_config()
            try:
                if not cfg.telegram_token:
                    raise TelegramError("Chưa có token Telegram — dán token rồi bấm Lưu cấu hình")
                chat = detect_chat_id(cfg.telegram_token)
            except TelegramError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if not chat:
                self._send_json(
                    400,
                    {
                        "error": "Bot chưa nhận tin nhắn nào — mở bot trên Telegram, "
                        "bấm Start (hoặc gửi một tin bất kỳ) rồi bấm lại"
                    },
                )
                return
            cfg.telegram_chat_id = chat
            save_config(cfg)
            self._send_json(200, {"chat_id": chat, "config": _config_dict(cfg)})
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
            if parsed.path == "/api/items/bulk":
                ids = payload.get("ids")
                if not isinstance(ids, list) or not ids:
                    raise ValueError("ids list required")
                if not isinstance(payload.get("enabled"), bool):
                    raise ValueError("enabled must be true or false")
                changed = botdata.set_items_enabled(ids, payload["enabled"])
                self._send_json(200, {"changed": changed, **_state()})
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
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "items":
                botdata.remove_item(parts[2], delete_image=True)
                self._send_json(200, _state())
                return
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "library":
                botdata.delete_library(unquote(parts[2]))
                self._send_json(200, _state())
                return
            if len(parts) == 2 and parts[0] == "api" and parts[1] == "purchases":
                botdata.clear_purchases()
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
        suffix = path.suffix.lower()
        mime = content_type or MIME.get(suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header(
            "Cache-Control",
            "public, max-age=604800" if suffix in CACHED_SUFFIXES else "no-store",
        )
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
        "loop_rest_min": cfg.loop_rest_min,
        "action_wait_s": cfg.action_wait_s,
        "buy_wait_s": cfg.buy_wait_s,
        "visit_wait_s": cfg.visit_wait_s,
        "poll_interval_s": cfg.poll_interval_s,
        "stall_swipe_ms": cfg.stall_swipe_ms,
        "screencap_raw": cfg.screencap_raw,
        # Never the token itself: the page has no password and anyone on the
        # Wi-Fi can open it. A hint is enough to tell which one is saved.
        "telegram_token_set": bool(cfg.telegram_token),
        "telegram_token_hint": cfg.telegram_token[-4:] if cfg.telegram_token else "",
        "telegram_chat_id": cfg.telegram_chat_id,
        "telegram_buys": cfg.telegram_buys,
    }


def _telegram_token(payload: dict, current: AppConfig) -> str:
    """A blank token field keeps the saved one (the page never holds it)."""
    if payload.get("telegram_token_clear"):
        return ""
    token = str(payload.get("telegram_token") or "").strip()
    return token or current.telegram_token


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
    loop_rest_min = float(payload.get("loop_rest_min", current.loop_rest_min))
    if not 0 <= loop_rest_min <= 180:
        raise ValueError("loop_rest_min must be 0–180")
    action_wait_s = float(payload.get("action_wait_s", current.action_wait_s))
    if not 0 <= action_wait_s <= 10:
        raise ValueError("action_wait_s must be 0–10")
    buy_wait_s = float(payload.get("buy_wait_s", current.buy_wait_s))
    if not 0 <= buy_wait_s <= 10:
        raise ValueError("buy_wait_s must be 0–10")
    visit_wait_s = float(payload.get("visit_wait_s", current.visit_wait_s))
    if not 0 <= visit_wait_s <= 10:
        raise ValueError("visit_wait_s must be 0–10")
    poll_interval_s = float(payload.get("poll_interval_s", current.poll_interval_s))
    if not 0 <= poll_interval_s <= 2:
        raise ValueError("poll_interval_s must be 0–2")
    stall_swipe_ms = int(payload.get("stall_swipe_ms", current.stall_swipe_ms))
    if stall_swipe_ms < 50 or stall_swipe_ms > 3000:
        raise ValueError("stall_swipe_ms must be 50–3000")
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
        loop_rest_min=loop_rest_min,
        action_wait_s=action_wait_s,
        buy_wait_s=buy_wait_s,
        visit_wait_s=visit_wait_s,
        poll_interval_s=poll_interval_s,
        stall_swipe_ms=stall_swipe_ms,
        screencap_raw=bool(payload.get("screencap_raw", current.screencap_raw)),
        telegram_token=_telegram_token(payload, current),
        telegram_chat_id=str(payload.get("telegram_chat_id", current.telegram_chat_id) or "").strip(),
        telegram_buys=bool(payload.get("telegram_buys", current.telegram_buys)),
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
        "run": RUNTIME.snapshot(),
        "run_prefs": botdata.load_run_prefs(),
        "purchases": botdata.list_purchases(),
        "wishlist_buys": botdata.wishlist_buy_status(),
        "buys_rev": botdata.buys_revision(),
    }


def _status(query: dict[str, list[str]]) -> dict:
    """What the page polls every second while the bot runs.

    The page sends back the log_seq and buys_rev it already holds; the log
    and the purchase history ride along only when they moved, so a quiet poll
    is ~1 KB and reads no file (the full history re-reads purchases.json and
    every item PNG, ~20 KB).
    """
    have_log = (query.get("log") or [None])[0]
    have_buys = (query.get("buys") or [""])[0]
    out: dict = {"run": RUNTIME.snapshot(known_log_seq=have_log)}
    rev = botdata.buys_revision()
    out["buys_rev"] = rev
    if have_buys != rev:
        out["wishlist_buys"] = botdata.wishlist_buy_status()
    return out


class BotHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False


def _lan_ipv4() -> list[str]:
    ips: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        probe.close()
        if ip and not ip.startswith("127."):
            ips.append(ip)
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def listen_urls(host: str, port: int) -> list[str]:
    if host in {"0.0.0.0", "::"}:
        return [f"http://127.0.0.1:{port}", *[f"http://{ip}:{port}" for ip in _lan_ipv4()]]
    return [f"http://{host}:{port}"]


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> BotHTTPServer:
    """Bound but not yet serving; the tray runs it on a thread of its own."""
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    return BotHTTPServer((host, port), BotWebHandler)


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    httpd = make_server(host, port)
    print("Web UI", flush=True)
    for url in listen_urls(host, port):
        print(f"  {url}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
