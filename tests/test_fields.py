from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from app.state.field_state import FieldStatus
from app.vision.fields import FieldDetector

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FARM = FIXTURES / "farm.png"
UNKNOWN = FIXTURES / "unknown.png"


def test_farm_fixture_has_ready_and_empty():
    fields = FieldDetector().detect_fields(FARM)
    states = {f.state for f in fields}
    assert FieldStatus.READY in states
    assert FieldStatus.EMPTY in states
    assert all(f.confidence > 0 for f in fields)


def test_unknown_crop_still_finds_ready():
    fields = FieldDetector().detect_fields(UNKNOWN)
    assert any(f.state is FieldStatus.READY for f in fields)


def test_synthetic_gold_patch_is_ready():
    image = np.zeros((1080, 1920, 3), dtype=np.uint8)
    lawn = cv2.cvtColor(np.uint8([[[40, 200, 180]]]), cv2.COLOR_HSV2BGR)[0, 0]
    gold = cv2.cvtColor(np.uint8([[[27, 220, 230]]]), cv2.COLOR_HSV2BGR)[0, 0]
    image[:, :] = lawn
    image[400:540, 820:1180] = gold
    fields = FieldDetector().detect_fields(image)
    ready = [f for f in fields if f.state is FieldStatus.READY]
    assert len(ready) >= 1
    cx, cy = ready[0].center
    assert 820 <= cx <= 1180
    assert 400 <= cy <= 540


def test_field_ids_are_stable_order():
    fields = FieldDetector().detect_fields(FARM)
    ids = [f.id for f in fields]
    assert ids == [f"field:{i}" for i in range(len(fields))]
    ys = [f.y for f in fields]
    assert ys == sorted(ys)


def test_3x3_cluster_splits_into_nine_cells():
    image = np.zeros((1080, 1920, 3), dtype=np.uint8)
    lawn = cv2.cvtColor(np.uint8([[[40, 200, 180]]]), cv2.COLOR_HSV2BGR)[0, 0]
    soil = cv2.cvtColor(np.uint8([[[12, 160, 90]]]), cv2.COLOR_HSV2BGR)[0, 0]
    image[:, :] = lawn
    # Isometric parallelogram (~25 degrees), sized like a Hay Day 3x3 cluster.
    quad = np.array([[700, 520], [1040, 380], [1180, 520], [840, 660]], dtype=np.int32)
    cv2.fillConvexPoly(image, quad, (int(soil[0]), int(soil[1]), int(soil[2])))
    cells = FieldDetector().detect_cells(image)
    empty = [c for c in cells if c.state is FieldStatus.EMPTY]
    assert len(empty) >= 6
    assert {c.row for c in empty} <= {0, 1, 2}
    assert {c.col for c in empty} <= {0, 1, 2}
