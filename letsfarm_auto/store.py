from __future__ import annotations

import json
from pathlib import Path

from letsfarm_auto.models import BotConfig

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"


def load_config(path: Path | None = None) -> BotConfig:
    target = path or CONFIG_PATH
    if not target.is_file():
        return BotConfig()
    data = json.loads(target.read_text(encoding="utf-8"))
    return BotConfig.from_dict(data)


def save_config(config: BotConfig, path: Path | None = None) -> Path:
    target = path or CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target
