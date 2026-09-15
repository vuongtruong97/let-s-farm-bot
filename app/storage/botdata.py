"""Wishlist, crops, and item/seed template files used by the bot and web UI."""

from __future__ import annotations

import json
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

from app import config as app_config

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
LIBRARY_ID_RE = re.compile(r"^(shop|news)_[0-9]{1,6}$")
PROTECTED_PREFIXES = ("hud_", "newspaper_", "shop_", "popup_", "price_")
MAX_PNG_BYTES = 2_000_000
MAX_EDGE = 1024
LIBRARY_MATCH_THRESHOLD = 0.90


def data_dir() -> Path:
    return app_config.DATA_DIR


def templates_dir() -> Path:
    path = data_dir() / "templates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def library_dir() -> Path:
    path = data_dir() / "library"
    path.mkdir(parents=True, exist_ok=True)
    return path


def wishlist_path() -> Path:
    return data_dir() / "wishlist.json"


def crops_path() -> Path:
    return data_dir() / "crops.json"


def column_path() -> Path:
    return data_dir() / "column.json"


MAX_PURCHASES = 200


def purchases_path() -> Path:
    return data_dir() / "purchases.json"


def load_purchases() -> list[dict]:
    data = load_json(purchases_path(), {"purchases": []})
    rows = data.get("purchases")
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = str(row.get("item") or "").strip()
        at = str(row.get("at") or "").strip()
        kind = str(row.get("kind") or "match").strip()
        if kind != "buy":
            kind = "match"
        if item and at:
            out.append({"item": item, "at": at, "kind": kind})
    return out


def list_purchases(limit: int = 80) -> list[dict]:
    cap = max(1, min(MAX_PURCHASES, int(limit)))
    return load_purchases()[:cap]


def wishlist_buy_status() -> list[dict]:
    """Enabled wishlist items with stall-match and verified-buy history."""
    matches: dict[str, list[dict]] = {}
    buys: dict[str, list[dict]] = {}
    for row in load_purchases():
        item = row["item"]
        event = {"at": row["at"]}
        if row["kind"] == "buy":
            buys.setdefault(item, []).append(event)
        else:
            matches.setdefault(item, []).append(event)
    rows: list[dict] = []
    for entry in wishlist_entries():
        if not entry.get("enabled", True):
            continue
        item_id = entry["id"]
        match_rows = matches.get(item_id, [])
        buy_rows = buys.get(item_id, [])
        rows.append(
            {
                "id": item_id,
                "template": entry["template"],
                "news_template": entry["news_template"],
                "has_image": entry["has_image"],
                "has_news_image": entry["has_news_image"],
                "match_count": len(match_rows),
                "buy_count": len(buy_rows),
                "last_match_at": match_rows[0]["at"] if match_rows else None,
                "last_buy_at": buy_rows[0]["at"] if buy_rows else None,
                "matches": match_rows,
                "buys": buy_rows,
            }
        )
    return rows


def record_purchase(
    item: str, when: datetime | None = None, kind: str = "match"
) -> dict:
    stamp = when or datetime.now().astimezone()
    event = "buy" if str(kind).strip() == "buy" else "match"
    row = {
        "item": str(item).strip() or "unknown",
        "at": stamp.isoformat(timespec="seconds"),
        "kind": event,
    }
    rows = [row, *load_purchases()][:MAX_PURCHASES]
    save_json(purchases_path(), {"purchases": rows})
    return row


def clear_purchases() -> None:
    save_json(purchases_path(), {"purchases": []})


def load_column() -> tuple[int, int] | None:
    data = load_json(column_path())
    try:
        x, y = int(data["x"]), int(data["y"])
    except (KeyError, TypeError, ValueError):
        return None
    if x < 0 or y < 0:
        return None
    return x, y


def save_column(x: int, y: int) -> Path:
    return save_json(column_path(), {"x": int(x), "y": int(y)})


def clear_column() -> None:
    path = column_path()
    if path.is_file():
        path.unlink()


def load_json(path: Path, default: dict | None = None) -> dict:
    if not path.is_file():
        return {} if default is None else dict(default)
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def save_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_wishlist() -> dict:
    return _normalize_wishlist(load_json(wishlist_path()))


def save_wishlist(wishlist: dict) -> Path:
    return save_json(wishlist_path(), _normalize_wishlist(wishlist))


def active_wishlist(wishlist: dict | None = None) -> dict:
    data = wishlist if wishlist is not None else load_wishlist()
    return {key: spec for key, spec in data.items() if spec.get("enabled", True)}


def load_crops() -> dict:
    return _normalize_crops(load_json(crops_path()))


def save_crops(crops: dict) -> Path:
    return save_json(crops_path(), _normalize_crops(crops))


def clean_id(value: str) -> str:
    text = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not ID_RE.fullmatch(text):
        raise ValueError("id must be lowercase letters, digits, underscore (start with a letter)")
    return text


def item_template_name(item_id: str) -> str:
    return f"item_{clean_id(item_id)}"


def item_news_template_name(item_id: str) -> str:
    return f"{item_template_name(item_id)}_news"


def seed_template_name(crop_id: str) -> str:
    return f"seed_{clean_id(crop_id)}"


def is_protected(name: str) -> bool:
    return name.startswith(PROTECTED_PREFIXES)


def template_png(name: str) -> Path:
    stem = Path(name).stem
    if not ID_RE.fullmatch(stem) and not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", stem):
        raise ValueError("bad template name")
    path = (templates_dir() / f"{stem}.png").resolve()
    if templates_dir().resolve() not in path.parents:
        raise ValueError("bad template path")
    return path


def next_capture_id() -> str:
    taken = set(load_wishlist())
    for path in templates_dir().glob("item_cap_*.png"):
        taken.add(path.stem.removeprefix("item_"))
    for n in range(1, 1000):
        key = f"cap_{n:02d}"
        if key not in taken:
            return key
    raise ValueError("too many captured items")


def png_info(path: Path) -> dict | None:
    if not path.is_file():
        return None
    with Image.open(path) as image:
        return {"width": image.width, "height": image.height, "bytes": path.stat().st_size}


def _validate_png_bytes(png_bytes: bytes) -> bytes:
    if len(png_bytes) > MAX_PNG_BYTES:
        raise ValueError("image too large")
    with Image.open(BytesIO(png_bytes)) as image:
        image.load()
        if image.width > MAX_EDGE or image.height > MAX_EDGE:
            raise ValueError(f"image edge must be <= {MAX_EDGE}px")
        out = BytesIO()
        image.convert("RGBA").save(out, format="PNG")
        return out.getvalue()


def decode_png(image_b64: str) -> bytes:
    raw = image_b64.strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    import base64

    return _validate_png_bytes(base64.b64decode(raw))


def write_template(name: str, png_bytes: bytes) -> Path:
    path = template_png(name)
    if is_protected(path.stem):
        raise ValueError(f"cannot overwrite system template {path.stem}")
    path.write_bytes(png_bytes)
    return path


def delete_template(name: str) -> None:
    path = template_png(name)
    if is_protected(path.stem):
        raise ValueError(f"cannot delete system template {path.stem}")
    if path.is_file():
        path.unlink()


def list_templates() -> list[dict]:
    rows = []
    for path in sorted(templates_dir().glob("*.png")):
        info = png_info(path) or {}
        kind = "system"
        if path.stem.startswith("item_"):
            kind = "item"
        elif path.stem.startswith("seed_"):
            kind = "seed"
        rows.append(
            {
                "name": path.stem,
                "kind": kind,
                "protected": is_protected(path.stem),
                **info,
            }
        )
    return rows


def clean_library_id(name: str) -> str:
    stem = Path(str(name)).stem.lower().strip()
    if not LIBRARY_ID_RE.fullmatch(stem):
        raise ValueError("bad library id")
    return stem


def library_kind_of(name: str) -> str:
    return clean_library_id(name).split("_", 1)[0]


def _library_kind(kind: str) -> str:
    text = str(kind or "").strip().lower()
    if text not in {"shop", "news"}:
        raise ValueError("kind must be shop or news")
    return text


def library_png(name: str) -> Path:
    stem = clean_library_id(name)
    path = (library_dir() / f"{stem}.png").resolve()
    if library_dir().resolve() not in path.parents:
        raise ValueError("bad library path")
    return path


def library_entry(name: str) -> dict:
    stem = clean_library_id(name)
    path = library_png(stem)
    info = png_info(path) or {}
    return {"id": stem, "kind": library_kind_of(stem), **info}


def list_library(kind: str | None = None) -> list[dict]:
    want = _library_kind(kind) if kind else None
    rows = []
    for path in sorted(library_dir().glob("*.png")):
        try:
            stem = clean_library_id(path.stem)
        except ValueError:
            continue
        row_kind = library_kind_of(stem)
        if want and row_kind != want:
            continue
        info = png_info(path) or {}
        rows.append({"id": stem, "kind": row_kind, **info})
    return rows


def next_library_id(kind: str) -> str:
    prefix = _library_kind(kind)
    taken = {path.stem for path in library_dir().glob(f"{prefix}_*.png")}
    for n in range(1, 10_000):
        key = f"{prefix}_{n:02d}" if n < 100 else f"{prefix}_{n}"
        if key not in taken:
            return key
    raise ValueError("too many library icons")


def library_contains(png_bytes: bytes, kind: str, threshold: float = LIBRARY_MATCH_THRESHOLD) -> str | None:
    import cv2
    import numpy as np

    kind = _library_kind(kind)
    crop = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if crop is None:
        return None
    best_name = None
    best_score = threshold
    for path in library_dir().glob(f"{kind}_*.png"):
        existing = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if existing is None:
            continue
        score = _template_score(crop, existing, gray=kind == "news")
        if score >= best_score:
            best_score = score
            best_name = path.stem
    return best_name


def _template_score(a, b, *, gray: bool) -> float:
    import cv2

    hay, needle = a, b
    if needle.shape[0] > hay.shape[0] or needle.shape[1] > hay.shape[1]:
        hay, needle = needle, hay
    if needle.shape[0] > hay.shape[0] or needle.shape[1] > hay.shape[1]:
        return 0.0
    if gray:
        if hay.ndim == 3:
            hay = cv2.cvtColor(hay, cv2.COLOR_BGR2GRAY)
        if needle.ndim == 3:
            needle = cv2.cvtColor(needle, cv2.COLOR_BGR2GRAY)
    result = cv2.matchTemplate(hay, needle, cv2.TM_CCOEFF_NORMED)
    return float(result.max())


def resolve_library_png(name: str, expected_kind: str) -> bytes:
    stem = clean_library_id(name)
    kind = _library_kind(expected_kind)
    if library_kind_of(stem) != kind:
        raise ValueError(f"library {stem} is not a {kind} icon")
    path = library_png(stem)
    if not path.is_file():
        raise ValueError(f"library {stem} not found")
    return path.read_bytes()


def save_library_png(kind: str, png_bytes: bytes, *, skip_similar: bool = True) -> dict:
    kind = _library_kind(kind)
    png_bytes = _validate_png_bytes(png_bytes)
    if skip_similar:
        hit = library_contains(png_bytes, kind)
        if hit:
            return {**library_entry(hit), "duplicate": True}
    name = next_library_id(kind)
    library_png(name).write_bytes(png_bytes)
    return {**library_entry(name), "duplicate": False}


def delete_library(name: str) -> None:
    path = library_png(name)
    if path.is_file():
        path.unlink()


def wishlist_entries() -> list[dict]:
    rows = []
    for item_id, spec in load_wishlist().items():
        name = spec.get("template") or item_template_name(item_id)
        news_name = spec.get("news_template") or item_news_template_name(item_id)
        path = templates_dir() / f"{name}.png"
        news_path = templates_dir() / f"{news_name}.png"
        info = png_info(path)
        news_info = png_info(news_path)
        rows.append(
            {
                "id": item_id,
                "template": name,
                "news_template": news_name,
                "enabled": bool(spec.get("enabled", True)),
                "has_image": info is not None,
                "has_news_image": news_info is not None,
                **(info or {}),
                "news_width": (news_info or {}).get("width"),
                "news_height": (news_info or {}).get("height"),
            }
        )
    return rows


def crop_entries() -> list[dict]:
    rows = []
    for crop_id, spec in load_crops().items():
        name = spec.get("seed_template") or seed_template_name(crop_id)
        path = templates_dir() / f"{name}.png"
        info = png_info(path)
        rows.append(
            {
                "id": crop_id,
                "storage": spec.get("storage") or "silo",
                "seed_template": name,
                "has_image": info is not None,
                **(info or {}),
            }
        )
    return rows


def save_item_png(item_id: str, png_bytes: bytes, *, overwrite: bool = False, enabled: bool = True) -> dict:
    key = clean_id(item_id)
    dest = template_png(item_template_name(key))
    if dest.is_file() and not overwrite:
        return upsert_item(key, enabled=enabled)
    write_template(item_template_name(key), png_bytes)
    return upsert_item(key, enabled=enabled)


def upsert_item(
    item_id: str,
    image_b64: str | None = None,
    news_image_b64: str | None = None,
    shop_library: str | None = None,
    news_library: str | None = None,
    enabled: bool | None = None,
) -> dict:
    return update_item(
        item_id,
        image_b64=image_b64,
        news_image_b64=news_image_b64,
        shop_library=shop_library,
        news_library=news_library,
        enabled=enabled,
    )


def update_item(
    old_id: str,
    new_id: str | None = None,
    image_b64: str | None = None,
    news_image_b64: str | None = None,
    shop_library: str | None = None,
    news_library: str | None = None,
    enabled: bool | None = None,
) -> dict:
    old_key = clean_id(old_id)
    wishlist = load_wishlist()
    spec = dict(
        wishlist.get(old_key)
        or {
            "template": item_template_name(old_key),
            "news_template": item_news_template_name(old_key),
            "enabled": True,
        }
    )
    new_key = clean_id(new_id) if new_id else old_key
    png = decode_png(image_b64) if image_b64 else None
    if png is None and shop_library:
        png = resolve_library_png(shop_library, "shop")
    news_png = decode_png(news_image_b64) if news_image_b64 else None
    if news_png is None and news_library:
        news_png = resolve_library_png(news_library, "news")
    if new_key != old_key:
        if old_key not in wishlist:
            raise ValueError(f"item {old_key} not found")
        if new_key in wishlist:
            raise ValueError(f"item {new_key} already exists")
        _rename_template(
            spec.get("template") or item_template_name(old_key),
            item_template_name(new_key),
        )
        _rename_template(
            spec.get("news_template") or item_news_template_name(old_key),
            item_news_template_name(new_key),
        )
        wishlist.pop(old_key)
    spec["template"] = item_template_name(new_key)
    spec["news_template"] = item_news_template_name(new_key)
    if enabled is not None:
        spec["enabled"] = bool(enabled)
    spec.setdefault("enabled", True)
    if png is not None:
        write_template(spec["template"], png)
        if image_b64:
            save_library_png("shop", png, skip_similar=True)
    if news_png is not None:
        write_template(spec["news_template"], news_png)
        if news_image_b64:
            save_library_png("news", news_png, skip_similar=True)
    wishlist[new_key] = spec
    save_wishlist(wishlist)
    return next(row for row in wishlist_entries() if row["id"] == new_key)


def _rename_template(old_name: str, new_name: str) -> None:
    old_path = template_png(old_name)
    new_path = template_png(new_name)
    if not old_path.is_file() or old_path.resolve() == new_path.resolve():
        return
    if new_path.is_file():
        raise ValueError(f"template {new_path.name} already exists")
    old_path.replace(new_path)


def remove_item(item_id: str, delete_image: bool = True) -> None:
    key = clean_id(item_id)
    wishlist = load_wishlist()
    spec = wishlist.pop(key, None)
    save_wishlist(wishlist)
    if delete_image:
        name = (spec or {}).get("template") or item_template_name(key)
        news_name = (spec or {}).get("news_template") or item_news_template_name(key)
        if name.startswith("item_"):
            delete_template(name)
        if news_name.startswith("item_") and news_name.endswith("_news"):
            delete_template(news_name)


def upsert_crop(crop_id: str, storage: str = "silo", image_b64: str | None = None) -> dict:
    key = clean_id(crop_id)
    if storage not in {"silo", "barn"}:
        raise ValueError("storage must be silo or barn")
    name = seed_template_name(key)
    crops = load_crops()
    spec = dict(crops.get(key) or {})
    spec["storage"] = storage
    spec["seed_template"] = name
    if image_b64:
        write_template(name, decode_png(image_b64))
    crops[key] = spec
    save_crops(crops)
    return next(row for row in crop_entries() if row["id"] == key)


def remove_crop(crop_id: str, delete_image: bool = False) -> None:
    key = clean_id(crop_id)
    crops = load_crops()
    spec = crops.pop(key, None)
    save_crops(crops)
    if delete_image:
        name = (spec or {}).get("seed_template") or seed_template_name(key)
        if name.startswith("seed_") and name != "seed_wheat":
            delete_template(name)


def _normalize_wishlist(raw: dict) -> dict:
    out = {}
    for key, spec in raw.items():
        item_id = clean_id(str(key))
        if not isinstance(spec, dict):
            spec = {}
        template = spec.get("template") or item_template_name(item_id)
        news_template = spec.get("news_template") or item_news_template_name(item_id)
        out[item_id] = {
            "template": str(template),
            "news_template": str(news_template),
            "enabled": bool(spec.get("enabled", True)),
        }
    return out


def _normalize_crops(raw: dict) -> dict:
    out = {}
    for key, spec in raw.items():
        crop_id = clean_id(str(key))
        if not isinstance(spec, dict):
            spec = {}
        storage = spec.get("storage") or "silo"
        if storage not in {"silo", "barn"}:
            storage = "silo"
        out[crop_id] = {
            "storage": storage,
            "seed_template": spec.get("seed_template") or seed_template_name(crop_id),
        }
    return out
