"""Push notifications to the player's phone through a Telegram bot.

Standard library only. Sending runs on a daemon thread and never raises into
the bot: a notification that fails must not cost a shop visit.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

from app.config import AppConfig
from app.storage.logger import get_logger

log = get_logger("NOTIFY")

TELEGRAM_API = "https://api.telegram.org"
TIMEOUT_S = 15
# Telegram caps an album at ten photos.
MEDIA_GROUP_MAX = 10
TEST_TEXT = "✅ Let's Farm Bot đã kết nối Telegram. Bot sẽ báo ở đây mỗi khi mua được hàng."

# (item id, quantity read off the crate or None, proof PNG or None)
Buy = tuple[str, "int | None", "bytes | None"]


class TelegramError(RuntimeError):
    pass


def telegram_ready(config: AppConfig) -> bool:
    return bool(config.telegram_token.strip() and str(config.telegram_chat_id).strip())


def buy_message(buys: list[Buy], when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%H:%M")
    lines = [f"🛒 Mua được {len(buys)} món · {stamp}"]
    for item, qty, _png in buys:
        lines.append(f"• {item} ×{qty}" if qty else f"• {item}")
    return "\n".join(lines)


def notify_buys(config: AppConfig, buys: list[Buy]) -> threading.Thread | None:
    """One message for what one shop visit bought, sent in the background.

    Returns the sending thread (tests join it), or None when nothing is sent:
    no buys, buy alerts off, or Telegram not set up.
    """
    if not buys or not config.telegram_buys or not telegram_ready(config):
        return None
    text = buy_message(buys)
    photos = [png for _item, _qty, png in buys if png][:MEDIA_GROUP_MAX]
    thread = threading.Thread(
        target=_send_buys,
        args=(config.telegram_token.strip(), str(config.telegram_chat_id).strip(), text, photos),
        daemon=True,
        name="notify-telegram",
    )
    thread.start()
    return thread


def _send_buys(token: str, chat: str, text: str, photos: list[bytes]) -> None:
    try:
        if len(photos) == 1:
            _telegram(
                token, "sendPhoto", {"chat_id": chat, "caption": text}, {"photo": ("buy.png", photos[0])}
            )
        elif photos:
            media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(photos))]
            media[0]["caption"] = text
            files = {f"p{i}": (f"buy{i}.png", png) for i, png in enumerate(photos)}
            _telegram(
                token,
                "sendMediaGroup",
                {"chat_id": chat, "media": json.dumps(media, ensure_ascii=False)},
                files,
            )
        else:
            _telegram(token, "sendMessage", {"chat_id": chat, "text": text})
        log.info(f"NOTIFY telegram sent: {text.splitlines()[0]}")
    except Exception as exc:  # never into the bot's loop
        log.info(f"NOTIFY FAIL telegram {exc}")


def send_test(config: AppConfig) -> None:
    """Send a hello right now; raises so the web page can show what is wrong."""
    if not config.telegram_token.strip():
        raise TelegramError("Chưa có token Telegram — dán token rồi bấm Lưu cấu hình")
    if not str(config.telegram_chat_id).strip():
        raise TelegramError("Chưa có chat ID — mở bot trên Telegram, bấm Start rồi bấm Lấy chat ID")
    _telegram(
        config.telegram_token.strip(),
        "sendMessage",
        {"chat_id": str(config.telegram_chat_id).strip(), "text": TEST_TEXT},
    )


def detect_chat_id(token: str) -> str | None:
    """Chat of the newest message to the bot (the player just pressed Start),
    or None when nobody has written to it yet."""
    updates = _telegram(token.strip(), "getUpdates", {}) or []
    for update in reversed(updates):
        for key in ("message", "edited_message", "my_chat_member", "channel_post"):
            chat = (update.get(key) or {}).get("chat") or {}
            if "id" in chat:
                return str(chat["id"])
    return None


def _telegram(token: str, method: str, fields: dict, files: dict | None = None):
    """POST one Bot API call and return its result. Errors never include the
    URL, which carries the token."""
    if files:
        body, content_type = _multipart(fields, files)
    else:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    request = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/{method}",
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except ValueError:
            raise TelegramError(f"Telegram trả HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TelegramError(f"Không kết nối được Telegram ({reason})") from None
    if not payload.get("ok"):
        raise TelegramError(payload.get("description") or "Telegram từ chối yêu cầu")
    return payload.get("result")


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts += [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"),
            b"\r\n",
        ]
    for name, (filename, data) in files.items():
        parts += [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode(),
            data,
            b"\r\n",
        ]
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
