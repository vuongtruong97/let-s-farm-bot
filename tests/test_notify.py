from __future__ import annotations

import json
from datetime import datetime

import pytest

from app import notify
from app.config import AppConfig


def _config(**kwargs) -> AppConfig:
    return AppConfig(telegram_token="123:tok", telegram_chat_id="42", **kwargs)


@pytest.fixture
def sent(monkeypatch):
    """Every Bot API call, instead of the network."""
    calls: list[tuple] = []

    def fake(token, method, fields, files=None):
        calls.append((token, method, fields, files))
        return {"message_id": 1}

    monkeypatch.setattr(notify, "_telegram", fake)
    return calls


def _send(config, buys):
    thread = notify.notify_buys(config, buys)
    if thread is not None:
        thread.join(timeout=5)
    return thread


def test_buy_message_lists_items_with_their_quantity():
    text = notify.buy_message(
        [("so_do", 4, None), ("pho_mai", None, None)], when=datetime(2026, 9, 30, 14, 20)
    )
    assert text == "🛒 Mua được 2 món · 14:20\n• so_do ×4\n• pho_mai"


@pytest.mark.parametrize(
    "config",
    [
        AppConfig(),
        AppConfig(telegram_token="123:tok"),
        _config(telegram_buys=False),
    ],
)
def test_nothing_is_sent_without_setup(sent, config):
    assert _send(config, [("so_do", 4, b"png")]) is None
    assert sent == []


def test_nothing_is_sent_for_a_visit_without_buys(sent):
    assert _send(_config(), []) is None
    assert sent == []


def test_one_proof_goes_as_a_photo(sent):
    _send(_config(), [("so_do", 4, b"png1")])
    [(token, method, fields, files)] = sent
    assert (token, method) == ("123:tok", "sendPhoto")
    assert fields["chat_id"] == "42"
    assert "so_do ×4" in fields["caption"]
    assert files == {"photo": ("buy.png", b"png1")}


def test_several_proofs_go_as_one_album(sent):
    _send(_config(), [("so_do", 4, b"a"), ("pho_mai", 2, b"b")])
    [(_token, method, fields, files)] = sent
    assert method == "sendMediaGroup"
    media = json.loads(fields["media"])
    assert [m["media"] for m in media] == ["attach://p0", "attach://p1"]
    assert "pho_mai ×2" in media[0]["caption"]
    assert "caption" not in media[1]
    assert set(files) == {"p0", "p1"}


def test_buys_without_proofs_go_as_text(sent):
    _send(_config(), [("so_do", None, None)])
    [(_token, method, fields, _files)] = sent
    assert method == "sendMessage"
    assert fields["text"].endswith("• so_do")


def test_a_failed_send_never_reaches_the_bot(monkeypatch):
    def boom(*_args, **_kwargs):
        raise notify.TelegramError("Unauthorized")

    monkeypatch.setattr(notify, "_telegram", boom)
    thread = _send(_config(), [("so_do", 1, b"png")])
    assert thread is not None and not thread.is_alive()


def test_send_test_explains_what_is_missing(sent):
    with pytest.raises(notify.TelegramError, match="token"):
        notify.send_test(AppConfig())
    with pytest.raises(notify.TelegramError, match="chat ID"):
        notify.send_test(AppConfig(telegram_token="123:tok"))
    notify.send_test(_config())
    assert sent[0][1] == "sendMessage"


def test_detect_chat_id_takes_the_newest_message(monkeypatch):
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 111}}},
        {"update_id": 2, "message": {"chat": {"id": 222}}},
    ]
    monkeypatch.setattr(notify, "_telegram", lambda *_a, **_k: updates)
    assert notify.detect_chat_id("123:tok") == "222"
    monkeypatch.setattr(notify, "_telegram", lambda *_a, **_k: [])
    assert notify.detect_chat_id("123:tok") is None


def test_multipart_carries_fields_and_the_photo():
    body, content_type = notify._multipart({"chat_id": "42"}, {"photo": ("buy.png", b"\x89PNG")})
    boundary = content_type.split("boundary=")[1]
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert b'name="chat_id"\r\n\r\n42\r\n' in body
    assert b'filename="buy.png"\r\nContent-Type: image/png\r\n\r\n\x89PNG\r\n' in body
    assert body.endswith(f"--{boundary}--\r\n".encode())
