from __future__ import annotations

import sys
import threading

import pytest

from app import tray


@pytest.mark.parametrize(
    "run, text",
    [
        ({"status": "idle"}, "Sẵn sàng"),
        ({"status": "running", "job": "loop"}, "▶ Đang chạy vòng lặp"),
        ({"status": "running", "job": "harvest"}, "▶ Đang chạy: harvest"),
        ({"status": "running", "job": "loop", "rest_until": 123.0}, "⏸ Nghỉ giữa vòng"),
        ({"status": "running", "job": "loop", "stopping": True}, "Đang dừng…"),
        ({"status": "error"}, "⚠ Lỗi — mở giao diện để xem"),
    ],
)
def test_status_text_for_the_tooltip(run, text):
    assert tray.status_text(run) == text


def test_tray_image_is_a_filled_badge():
    image = tray.tray_image(64)
    assert image.size == (64, 64) and image.mode == "RGBA"
    assert image.getpixel((32, 32))[3] == 255  # drawn in the middle
    assert image.getpixel((0, 0))[3] == 0  # see-through corner


def test_already_running_sees_our_web_ui():
    from app.web.server import make_server

    httpd = make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        assert tray.already_running(url) is True
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert tray.already_running(url, timeout=0.5) is False


def test_redirect_streams_gives_pythonw_somewhere_to_write(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    tray.redirect_streams()
    try:
        print("one line per web request")
        print("Traceback: boom", file=sys.stderr)
        sys.stderr.flush()
        assert (tmp_path / "tray.log").read_text(encoding="utf-8") == "Traceback: boom\n"
    finally:
        sys.stdout.close()
        sys.stderr.close()


class _FakeRuntime:
    def __init__(self):
        self.status = "running"
        self.stopped = False
        self.log_seq = 0

    def stop(self):
        self.stopped = True
        self.status = "idle"

    def snapshot(self, known_log_seq=None):
        return {"status": self.status}


class _FakeHttpd:
    def __init__(self):
        self.calls: list[str] = []

    def shutdown(self):
        self.calls.append("shutdown")

    def server_close(self):
        self.calls.append("close")


class _FakeIcon:
    stopped = False

    def stop(self):
        self.stopped = True


def test_quit_stops_the_bot_then_the_web_then_the_icon():
    runtime, httpd = _FakeRuntime(), _FakeHttpd()
    app = tray.TrayApp(httpd, runtime)
    app.icon = _FakeIcon()
    app.quit()
    assert runtime.stopped
    assert httpd.calls == ["shutdown", "close"]
    assert app.icon.stopped
