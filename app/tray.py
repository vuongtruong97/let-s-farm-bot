"""Run the bot from the Windows system tray, without a console window.

    pythonw -m app.tray        (what farm_bot.bat starts)

The web UI runs on a thread of this process. A wheat icon next to the clock
opens it (click), shows the bot's state (hover) and has a menu to open the
log folder or quit everything. Starting it again while it runs only opens the
web page.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser

from PIL import Image, ImageDraw, ImageFont

from app.config import LOG_DIR, load_config
from app.storage.logger import get_logger, setup_logging

PORT = 48721
LOCAL_URL = f"http://127.0.0.1:{PORT}/"
TITLE = "Let's Farm Bot"
STATUS_EVERY_S = 3.0
# Tracebacks of a window-less process; the bot's own log stays in bot.log.
TRAY_LOG = "tray.log"
TRAY_LOG_MAX = 1_000_000


def _log():
    # Not at import: get_logger would set up console logging before main()
    # has given pythonw a stderr, and every record would then fail.
    return get_logger("TRAY")


def redirect_streams() -> None:
    """pythonw has no stdout or stderr. Send stdout (one line per web request)
    nowhere and stderr to logs/tray.log, so a crash still leaves a trace."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / TRAY_LOG
    if path.is_file() and path.stat().st_size > TRAY_LOG_MAX:
        path.unlink()
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(path, "a", encoding="utf-8", buffering=1)


def already_running(url: str = LOCAL_URL, timeout: float = 1.5) -> bool:
    """True when this bot's web UI already answers on the port."""
    try:
        with urllib.request.urlopen(f"{url}api/status", timeout=timeout) as res:
            return "run" in json.loads(res.read().decode("utf-8"))
    except (OSError, ValueError):
        return False


def status_text(run: dict) -> str:
    """One line for the icon's tooltip."""
    if run.get("status") == "running":
        if run.get("stopping"):
            return "Đang dừng…"
        if run.get("rest_until"):
            return "⏸ Nghỉ giữa vòng"
        job = run.get("job")
        return "▶ Đang chạy vòng lặp" if job == "loop" else f"▶ Đang chạy: {job or ''}".rstrip(": ")
    if run.get("status") == "error":
        return "⚠ Lỗi — mở giao diện để xem"
    return "Sẵn sàng"


def tray_image(size: int = 64) -> Image.Image:
    """The 🌾 on a yellow game badge with a white ring; plain shapes stand in
    when the Windows emoji font is missing."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ring = max(2, size // 16)
    draw.ellipse((1, 1, size - 2, size - 2), fill=(255, 201, 40, 255), outline=(255, 255, 255, 255), width=ring)
    try:
        font = ImageFont.truetype("seguiemj.ttf", int(size * 0.6))
        draw.text((size / 2, size * 0.54), "🌾", font=font, embedded_color=True, anchor="mm")
    except OSError:
        stem = (79, 174, 29, 255)
        draw.line((size * 0.5, size * 0.78, size * 0.5, size * 0.3), fill=stem, width=max(2, size // 12))
        for dy in (0.28, 0.4, 0.52):
            y = size * dy
            for dx in (-0.1, 0.1):
                x = size * (0.5 + dx)
                draw.ellipse((x - size * 0.07, y - size * 0.05, x + size * 0.07, y + size * 0.05), fill=(240, 165, 30, 255))
    return image


def alert(text: str) -> None:
    """A message box: a tray app has no console to print an error into."""
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)
    else:
        print(text, file=sys.stderr)


class TrayApp:
    def __init__(self, httpd, runtime):
        self.httpd = httpd
        self.runtime = runtime
        self.icon = None
        self._stopping = threading.Event()

    def run(self) -> None:
        import pystray

        self.icon = pystray.Icon(
            "letsfarm-bot",
            tray_image(),
            f"{TITLE} — Sẵn sàng",
            menu=pystray.Menu(
                pystray.MenuItem("Mở giao diện", self.open_ui, default=True),
                pystray.MenuItem("Mở thư mục log", self.open_logs),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Thoát", self.quit),
            ),
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="web").start()
        threading.Thread(target=self._watch, daemon=True, name="tray-status").start()
        _log().info(f"TRAY started, web on {LOCAL_URL}")
        self.icon.run(setup=self._ready)

    def _ready(self, icon) -> None:
        icon.visible = True
        self.open_ui()

    def open_ui(self, *_args) -> None:
        webbrowser.open(LOCAL_URL)

    def open_logs(self, *_args) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(LOG_DIR)

    def _watch(self) -> None:
        while not self._stopping.wait(STATUS_EVERY_S):
            try:
                run = self.runtime.snapshot(known_log_seq=str(self.runtime.log_seq))
                if self.icon is not None:
                    # Windows cuts tooltips at 127 characters.
                    self.icon.title = f"{TITLE} — {status_text(run)}"[:120]
            except Exception as exc:  # a tooltip must never take the app down
                _log().debug(f"TRAY status {exc}")

    def quit(self, *_args) -> None:
        self._stopping.set()
        self.runtime.stop()
        # Let the current step finish, so the game is not left mid-swipe.
        deadline = time.monotonic() + 5.0
        while self.runtime.snapshot(known_log_seq=str(self.runtime.log_seq))["status"] == "running":
            if time.monotonic() > deadline:
                break
            time.sleep(0.2)
        self.httpd.shutdown()
        self.httpd.server_close()
        _log().info("TRAY quit")
        if self.icon is not None:
            self.icon.stop()


def main() -> int:
    redirect_streams()
    setup_logging(load_config(), console=False)
    if already_running():
        webbrowser.open(LOCAL_URL)
        return 0
    from app.web.runtime import RUNTIME
    from app.web.server import DEFAULT_HOST, make_server

    try:
        httpd = make_server(DEFAULT_HOST, PORT)
    except OSError as exc:
        alert(f"Không mở được cổng {PORT}: có chương trình khác đang dùng cổng này?\n\n{exc}")
        return 1
    TrayApp(httpd, RUNTIME).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
