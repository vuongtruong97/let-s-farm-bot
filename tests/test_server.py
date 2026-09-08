from __future__ import annotations

from fastapi.testclient import TestClient

from letsfarm_auto.server import app, runtime


client = TestClient(app)


def setup_function() -> None:
    runtime.use_demo()
    if hasattr(runtime.device, "reset"):
        runtime.device.reset()


def test_health():
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_index_serves_ui():
    res = client.get("/")
    assert res.status_code == 200
    assert "Auto nông trại" in res.text


def test_state_and_config_update():
    state = client.get("/api/state").json()
    assert state["kind"] == "demo"
    assert state["connected"] is True
    res = client.put("/api/config", json={"rows": 4, "cols": 6, "run_mode": "harvest"})
    assert res.status_code == 200
    config = res.json()["config"]
    assert config["rows"] == 4
    assert config["cols"] == 6
    assert config["run_mode"] == "harvest"


def test_screenshot_and_scan():
    shot = client.get("/api/screenshot")
    assert shot.status_code == 200
    assert shot.headers["content-type"] == "image/jpeg"
    assert shot.content[:2] == b"\xff\xd8"
    scan = client.post("/api/scan").json()
    assert scan["ok"] is True
    assert "ready" in scan["stats"]
    assert scan["stats"]["ready"] + scan["stats"]["growing"] + scan["stats"]["empty"] >= 1


def test_start_stop_bot():
    started = client.post("/api/start").json()
    assert started["running"] is True
    stopped = client.post("/api/stop").json()
    assert stopped["running"] is False
