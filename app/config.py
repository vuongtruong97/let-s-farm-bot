from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
SCREENSHOT_DIR = ROOT / "screenshots"
LOG_DIR = ROOT / "logs"


@dataclass
class AppConfig:
    adb_host: str = "127.0.0.1"
    adb_port: int | None = None
    adb_bin: str | None = None
    debug: bool = True
    allow_diamond_spending: bool = False
    swipe_duration_ms: int = 320
    package: str = ""
    template_threshold: float = 0.80
    buy_threshold: float = 0.72
    news_threshold: float = 0.72
    loop_rest_min: float = 5.0
    action_wait_s: float = 0.9
    buy_wait_s: float = 2.0

    def screenshot_dir(self) -> Path:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        (SCREENSHOT_DIR / "debug").mkdir(parents=True, exist_ok=True)
        return SCREENSHOT_DIR

    def log_dir(self) -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_DIR


def load_config(path: Path | None = None) -> AppConfig:
    target = path or CONFIG_PATH
    if not target.is_file():
        return AppConfig()
    data = json.loads(target.read_text(encoding="utf-8"))
    known = {field: getattr(AppConfig(), field) for field in AppConfig.__dataclass_fields__}
    kwargs = {key: data[key] for key in known if key in data}
    return AppConfig(**kwargs)


def save_config(config: AppConfig, path: Path | None = None) -> Path:
    target = path or CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "adb_host": config.adb_host,
        "adb_port": config.adb_port,
        "adb_bin": config.adb_bin,
        "debug": config.debug,
        "allow_diamond_spending": False,
        "swipe_duration_ms": config.swipe_duration_ms,
        "package": config.package,
        "template_threshold": config.template_threshold,
        "buy_threshold": config.buy_threshold,
        "news_threshold": config.news_threshold,
        "loop_rest_min": config.loop_rest_min,
        "action_wait_s": config.action_wait_s,
        "buy_wait_s": config.buy_wait_s,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
