import pytest
from fastapi.testclient import TestClient

from buildmedic import db
from buildmedic.config import settings
from buildmedic.database import upgrade_database


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "data_dir", tmp_path)
    monkeypatch.setattr(cfg, "session_secret", "test-session-secret-" + "s" * 32)
    monkeypatch.setattr(cfg, "model_base_url", "")
    monkeypatch.setattr(cfg, "model_api_key", "")
    monkeypatch.setattr(cfg, "model_id", "")
    monkeypatch.setattr(cfg, "model_provider", "compatible")
    monkeypatch.setattr(cfg, "model_thinking", "disabled")
    monkeypatch.setattr(cfg, "adaptive_enabled", False)
    monkeypatch.setattr(cfg, "runner_token", "test-internal-runner-token")
    engine = db.make_engine("sqlite:///" + str(tmp_path / "test.db"))
    monkeypatch.setattr(db, "engine", engine)
    upgrade_database()
    yield
    engine.dispose()


@pytest.fixture
def client():
    from buildmedic.api import app

    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        client.get("/api/v1/demo-cases")
        yield client
