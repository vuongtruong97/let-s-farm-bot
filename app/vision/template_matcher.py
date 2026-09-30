from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from app.config import DATA_DIR
from app.vision.regions import UI_REGIONS

TEMPLATES_DIR = DATA_DIR / "templates"
# The emulator is pinned to 1920x1080 and templates are captured from it, so
# UI art always lands at 1:1. Item matching passes its own scales.
DEFAULT_SCALES = (1.0,)
# A coarse pass scores a little lower than full size (fine detail is averaged
# away); a spot within this much of the threshold earns a full-size look.
COARSE_SLACK = 0.15
# Best spots per scale that the coarse pass hands on to the full-size check.
COARSE_PEAKS = 2


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
        self._stamp: tuple = ()
        self.reload()

    def reload(self) -> None:
        self._templates = {}
        self._stamp = self._dir_stamp()
        if not self.templates_dir.is_dir():
            return
        for path in sorted(self.templates_dir.glob("*.png")):
            image = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if image is not None:
                self._templates[path.stem] = image

    def reload_if_changed(self) -> bool:
        """Reload only when a template file was added, removed or rewritten.
        Decoding every PNG on disk per job is pure overhead in the shop loop."""
        if self._templates and self._dir_stamp() == self._stamp:
            return False
        self.reload()
        return True

    def _dir_stamp(self) -> tuple:
        if not self.templates_dir.is_dir():
            return ()
        return tuple(
            sorted(
                (path.name, path.stat().st_mtime_ns, path.stat().st_size)
                for path in self.templates_dir.glob("*.png")
            )
        )

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
        coarse: float | None = None,
    ) -> TemplateMatch | None:
        """Best match of one template, or None under the threshold.

        coarse=0.5 first searches a half-size copy, then scores only the spots
        found there at full size. Same threshold and full-size score, at about
        a tenth of the cost of sweeping a whole 1920x1080 frame.
        """
        template = self._templates.get(name)
        if template is None:
            return None
        cutoff = self.threshold if threshold is None else threshold
        use_scales = scales if scales is not None else self.scales
        search, ox, oy = _search_roi(image, name, template.shape)
        if coarse:
            return _match_coarse(
                name, search, ox, oy, template, cutoff, use_scales, channels, coarse
            )
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


def _scaled_template(template: np.ndarray, scale: float) -> tuple[np.ndarray, int, int]:
    tw = max(8, int(template.shape[1] * scale))
    th = max(8, int(template.shape[0] * scale))
    if scale == 1.0:
        return template, tw, th
    return cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA), tw, th


def _match_coarse(
    name: str,
    search: np.ndarray,
    ox: int,
    oy: int,
    template: np.ndarray,
    cutoff: float,
    scales: tuple[float, ...],
    channels: str,
    factor: float,
) -> TemplateMatch | None:
    """Coarse-to-fine match_one: find candidate spots on a grey copy shrunk
    by `factor`, then score each at full size, in the asked channels, in a
    window one template wide around it. Only that full-size score is compared
    with the cutoff, so the cheap pass only proposes places to look."""
    img_h, img_w = search.shape[:2]
    small = cv2.resize(
        search,
        (max(1, int(img_w * factor)), max(1, int(img_h * factor))),
        interpolation=cv2.INTER_AREA,
    )
    haystack = _for_match(small, "gray")
    spots: list[tuple[float, int, int]] = []
    for scale in scales:
        _scaled, tw, th = _scaled_template(template, scale)
        sw, sh = max(4, int(tw * factor)), max(4, int(th * factor))
        if tw >= img_w or th >= img_h or sw >= small.shape[1] or sh >= small.shape[0]:
            continue
        needle = cv2.resize(template, (sw, sh), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(haystack, _for_match(needle, "gray"), cv2.TM_CCOEFF_NORMED)
        for _ in range(COARSE_PEAKS):
            _, score, _, loc = cv2.minMaxLoc(result)
            if score < cutoff - COARSE_SLACK:
                break
            spots.append((float(score), int(loc[0] / factor), int(loc[1] / factor)))
            # Blank out this peak so the next one is a different spot.
            x, y = loc
            result[max(0, y - sh) : y + sh + 1, max(0, x - sw) : x + sw + 1] = -1.0
    best: TemplateMatch | None = None
    seen: list[tuple[int, int]] = []
    for _score, cx, cy in sorted(spots, reverse=True):
        if any(abs(cx - sx) < 8 and abs(cy - sy) < 8 for sx, sy in seen):
            continue
        seen.append((cx, cy))
        for scale in scales:
            scaled, tw, th = _scaled_template(template, scale)
            x1, y1 = max(0, cx - tw), max(0, cy - th)
            x2, y2 = min(img_w, cx + 2 * tw), min(img_h, cy + 2 * th)
            if x2 - x1 <= tw or y2 - y1 <= th:
                continue
            window = _for_match(search[y1:y2, x1:x2], channels)
            result = cv2.matchTemplate(window, _for_match(scaled, channels), cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(result)
            if best is None or score > best.confidence:
                best = TemplateMatch(
                    name, x1 + int(loc[0]) + ox, y1 + int(loc[1]) + oy, tw, th, float(score)
                )
    if best is None or best.confidence < cutoff:
        return None
    return best


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
