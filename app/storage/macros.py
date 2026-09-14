"""Saved tap/swipe macros recorded from the web UI or ADB getevent."""

from __future__ import annotations

from pathlib import Path

from app.storage.botdata import clean_id, load_json, save_json
from app import config as app_config

STEP_TYPES = frozenset({"tap", "swipe", "wait", "back", "home"})
MAX_WAIT_MS = 5000


def macros_dir() -> Path:
    path = app_config.DATA_DIR / "macros"
    path.mkdir(parents=True, exist_ok=True)
    return path


def macro_path(macro_id: str) -> Path:
    key = clean_id(macro_id)
    return macros_dir() / f"{key}.json"


def list_macros() -> list[dict]:
    rows = []
    for path in sorted(macros_dir().glob("*.json")):
        data = load_macro(path.stem)
        rows.append(
            {
                "id": path.stem,
                "name": data.get("name") or path.stem,
                "width": data.get("width") or 0,
                "height": data.get("height") or 0,
                "steps": len(data.get("steps") or []),
            }
        )
    return rows


def load_macro(macro_id: str) -> dict:
    path = macro_path(macro_id)
    if not path.is_file():
        raise ValueError(f"macro {macro_id} not found")
    return normalize_macro(load_json(path), fallback_id=path.stem)


def save_macro(macro: dict) -> Path:
    data = normalize_macro(macro)
    return save_json(macro_path(data["name"]), data)


def delete_macro(macro_id: str) -> None:
    path = macro_path(macro_id)
    if path.is_file():
        path.unlink()


def normalize_macro(raw: dict, fallback_id: str | None = None) -> dict:
    name = clean_id(str(raw.get("name") or fallback_id or ""))
    width = max(0, int(raw.get("width") or 0))
    height = max(0, int(raw.get("height") or 0))
    steps = [normalize_step(step) for step in raw.get("steps") or []]
    return {"name": name, "width": width, "height": height, "steps": steps}


def normalize_step(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("step must be an object")
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in STEP_TYPES:
        raise ValueError(f"unknown step type {kind}")
    if kind == "tap":
        return {"type": "tap", "x": _coord(raw.get("x")), "y": _coord(raw.get("y"))}
    if kind == "swipe":
        return {
            "type": "swipe",
            "x1": _coord(raw.get("x1")),
            "y1": _coord(raw.get("y1")),
            "x2": _coord(raw.get("x2")),
            "y2": _coord(raw.get("y2")),
            "duration_ms": max(80, int(raw.get("duration_ms") or 320)),
        }
    if kind == "wait":
        return {"type": "wait", "ms": max(0, min(MAX_WAIT_MS, int(raw.get("ms") or 0)))}
    return {"type": kind}


def _coord(value) -> int:
    return max(0, int(value or 0))
