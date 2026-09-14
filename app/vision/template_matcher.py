from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.config import DATA_DIR
from app.vision.regions import UI_REGIONS

TEMPLATES_DIR = DATA_DIR / "templates"
DEFAULT_SCALES = (1.0, 0.85, 0.92, 1.08, 1.15)


@dataclass(frozen=True)
class TemplateMatch:
    name: str
    x: int
    y: int
    width: int
    height: int
    confidence: float


def as_bgr(source: str | Path | bytes | np.ndarray | Image.Image) -> np.ndarray:
    if isinstance(source, np.ndarray):
        if source.ndim == 2:
            return cv2.cvtColor(source, cv2.COLOR_GRAY2BGR)
        if source.shape[2] == 4:
            return cv2.cvtColor(source, cv2.COLOR_BGRA2BGR)
        return source
    if isinstance(source, Image.Image):
        rgb = np.array(source.convert("RGB"))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    if isinstance(source, (bytes, bytearray)):
        arr = np.frombuffer(source, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Không decode được PNG")
        return image
    path = Path(source)
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(f"Không đọc được ảnh: {path}")
    return image


class TemplateMatcher:
    """OpenCV template matching. No business rules."""

    def __init__(
        self,
        templates_dir: Path | None = None,
        threshold: float = 0.80,
        scales: tuple[float, ...] = DEFAULT_SCALES,
    ):
        self.templates_dir = templates_dir or TEMPLATES_DIR
        self.threshold = threshold
        self.scales = scales
        self._templates: dict[str, np.ndarray] = {}
        self.reload()

    def reload(self) -> None:
        self._templates = {}
        if not self.templates_dir.is_dir():
            return
        for path in sorted(self.templates_dir.glob("*.png")):
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is not None:
                self._templates[path.stem] = image

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._templates)

    def match_one(
        self,
        image: np.ndarray,
        name: str,
        threshold: float | None = None,
        scales: tuple[float, ...] | None = None,
        channels: str = "bgr",
    ) -> TemplateMatch | None:
        template = self._templates.get(name)
        if template is None:
            return None
        cutoff = self.threshold if threshold is None else threshold
        use_scales = scales if scales is not None else self.scales
        search, ox, oy = _search_roi(image, name, template.shape)
        img_h, img_w = search.shape[:2]
        best: TemplateMatch | None = None
        haystack = _for_match(search, channels)
        for scale in use_scales:
            tw = max(8, int(template.shape[1] * scale))
            th = max(8, int(template.shape[0] * scale))
            if tw >= img_w or th >= img_h:
                continue
            scaled = (
                template
                if scale == 1.0
                else cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA)
            )
            needle = _for_match(scaled, channels)
            result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(result)
            if best is None or score > best.confidence:
                best = TemplateMatch(
                    name,
                    int(loc[0]) + ox,
                    int(loc[1]) + oy,
                    tw,
                    th,
                    float(score),
                )
            if scale == 1.0 and best is not None and best.confidence >= cutoff:
                return best
        if best is None or best.confidence < cutoff:
            return None
        return best

    def match_all(
        self,
        image: np.ndarray,
        names: tuple[str, ...] | None = None,
        threshold: float | None = None,
    ) -> list[TemplateMatch]:
        found: list[TemplateMatch] = []
        lookup = names if names is not None else self.names
        for name in lookup:
            hit = self.match_one(image, name, threshold=threshold)
            if hit is not None:
                found.append(hit)
        return found

    def match_many(
        self,
        image: np.ndarray,
        name: str,
        threshold: float | None = None,
        min_dist: int = 48,
        scales: tuple[float, ...] | None = None,
        channels: str = "bgr",
    ) -> list[TemplateMatch]:
        """All peaks for one template. Vision only — no ranking rules."""
        template = self._templates.get(name)
        if template is None:
            return []
        cutoff = self.threshold if threshold is None else threshold
        use_scales = scales if scales is not None else (1.0,)
        search, ox, oy = _search_roi(image, name, template.shape)
        img_h, img_w = search.shape[:2]
        haystack = _for_match(search, channels)
        peaks: list[TemplateMatch] = []
        for scale in use_scales:
            tw = max(8, int(template.shape[1] * scale))
            th = max(8, int(template.shape[0] * scale))
            if tw >= img_w or th >= img_h:
                continue
            scaled = (
                template
                if scale == 1.0
                else cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA)
            )
            needle = _for_match(scaled, channels)
            result = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(result >= cutoff)
            order = sorted(
                zip(ys.tolist(), xs.tolist()),
                key=lambda p: float(result[p[0], p[1]]),
                reverse=True,
            )
            for y, x in order:
                score = float(result[y, x])
                px, py = int(x) + ox, int(y) + oy
                if any(abs(px - h.x) < min_dist and abs(py - h.y) < min_dist for h in peaks):
                    continue
                peaks.append(TemplateMatch(name, px, py, tw, th, score))
        return peaks


def _for_match(image: np.ndarray, channels: str) -> np.ndarray:
    """BGR keeps colour; gray ignores newspaper print vs shop paint."""
    if channels == "bgr":
        return image
    if image.ndim == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image
    if channels == "gray":
        return gray
    raise ValueError(f"unknown match channels '{channels}'")


def _search_roi(
    image: np.ndarray, name: str, template_shape: tuple[int, ...]
) -> tuple[np.ndarray, int, int]:
    region = UI_REGIONS.get(name)
    if region is None and name.startswith("newspaper_stand"):
        region = UI_REGIONS.get("newspaper_stand")
    if region is None:
        return image, 0, 0
    x, y, w, h = region.to_pixels(image.shape[1], image.shape[0])
    if template_shape[0] >= h or template_shape[1] >= w:
        return image, 0, 0
    roi = image[y : y + h, x : x + w]
    if roi.size == 0:
        return image, 0, 0
    return roi, x, y
