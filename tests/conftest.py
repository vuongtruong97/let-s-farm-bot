from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _keep_failure_frames_out_of_screenshots(tmp_path, monkeypatch):
    """Failure frames a test provokes must not land among the real ones (only
    the newest few are kept, so a test run would push real evidence out)."""
    monkeypatch.setattr("app.actions.shop.SCREENSHOT_DIR", tmp_path / "screenshots")
