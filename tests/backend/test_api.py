import hashlib
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from buildmedic import store
from buildmedic.api import app
from buildmedic.cases import metadata
from buildmedic.schemas import CreateTask, Source


def payload(case_id="pagination"):
    return CreateTask(source=Source(case_id=case_id)).model_dump()


def create(client, case_id="pagination", key="idempotent-key-1"):
    response = client.post("/api/v1/tasks", json=payload(case_id), headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()["data"]["task_id"]


def test_cases_and_idempotency(client):
    assert len(client.get("/api/v1/demo-cases").json()["data"]) == 5
    tid = create(client)
    assert create(client) == tid
    assert (
        client.post(
            "/api/v1/tasks", json=payload("imports"), headers={"Idempotency-Key": "idempotent-key-1"}
        ).status_code
        == 409
    )
    assert client.post("/api/v1/tasks", json=payload()).status_code == 422
    assert client.get("/api/v1/tasks").json()["data"]["total"] == 1


def test_concurrent_same_key_has_one_task():
    body = payload()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(
            pool.map(lambda _: store.create("owner", "same-key", body, metadata("pagination")), range(8))
        )
    assert len(set(ids)) == 1
    assert store.list_tasks("owner")["total"] == 1


def test_duplicate_request_does_not_depend_on_source_availability(client, monkeypatch):
    tid = create(client)

    async def unavailable(_body):
        raise AssertionError("An accepted request must not fetch its source again")

    monkeypatch.setattr("buildmedic.api.source_metadata", unavailable)
    assert create(client) == tid
    response = client.post(
        "/api/v1/tasks", json=payload("imports"), headers={"Idempotency-Key": "idempotent-key-1"}
    )
    assert response.status_code == 409


def test_cross_session_task_and_artifact_are_private(client):
    tid = create(client)
    token, *_ = store.claim(tid)
    art = store.artifact(tid, "test_log", "original evidence", token)
    route = f"/api/v1/tasks/{tid}/artifacts/{art['id']}"
    response = client.get(route + "?download=true")
    assert response.content == b"original evidence"
    assert response.headers["etag"] == '"' + hashlib.sha256(response.content).hexdigest() + '"'
    assert "attachment" in response.headers["content-disposition"]
    with TestClient(app, backend_options={"use_uvloop": True}) as other:
        for path in (f"/api/v1/tasks/{tid}", route, f"/api/v1/tasks/{tid}/events"):
            assert other.get(path).status_code == 404
        assert other.post(f"/api/v1/tasks/{tid}/cancel").status_code == 404


def test_cancel_queued_and_retry_preserve_history(client):
    tid = create(client)
    assert client.post(f"/api/v1/tasks/{tid}/cancel").json()["data"]["lifecycle"] == "cancelled"
    assert store.claim(tid) is None
    response = client.post(f"/api/v1/tasks/{tid}/retry", headers={"Idempotency-Key": "retry-key-1"})
    assert response.status_code == 202
    new_id = response.json()["data"]["task_id"]
    assert new_id != tid
    assert store.snapshot(new_id)["parent_task_id"] == tid
    assert store.snapshot(tid)["lifecycle"] == "cancelled"


def test_sse_replay_uses_last_event_id_and_resets_future_cursor(client):
    tid = create(client)
    token, *_ = store.claim(tid)
    for phase in ("preflight", "reproduce", "diagnose"):
        store.mutate(tid, "phase.changed", {"phase": phase}, token=token)
    store.finish(tid, token, "completed", "blocked", "Missing environment")
    snap = store.snapshot(tid)
    response = client.get(f"/api/v1/tasks/{tid}/events?after=0", headers={"Last-Event-ID": "3"})
    ids = [int(line[4:]) for line in response.text.splitlines() if line.startswith("id: ")]
    assert ids == list(range(4, snap["last_event_seq"] + 1))
    assert "task.finished" in response.text
    assert "stream.reset" in client.get(f"/api/v1/tasks/{tid}/events?after=9999").text
    assert client.get(f"/api/v1/tasks/{tid}/events", headers={"Last-Event-ID": "nope"}).status_code == 422


def test_origin_and_adaptive_guard(client):
    response = client.post(
        "/api/v1/tasks",
        json=payload(),
        headers={"Idempotency-Key": "idempotent-key-1", "Origin": "https://untrusted.example"},
    )
    assert response.status_code == 403
    body = dict(payload(), mode="adaptive")
    response = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "idempotent-key-2"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "adaptive_unavailable"


def test_filters_and_validation_errors(client):
    tid = create(client)
    create(client, "imports", "idempotent-key-2")
    assert client.get("/api/v1/tasks?page_size=1").json()["data"]["total"] == 2
    assert len(client.get("/api/v1/tasks?page_size=1").json()["data"]["items"]) == 1
    client.post(f"/api/v1/tasks/{tid}/cancel")
    assert client.get("/api/v1/tasks?lifecycle=cancelled").json()["data"]["total"] == 1
    assert client.get("/api/v1/tasks?repo=nonexistent").json()["data"]["total"] == 0
    assert client.get("/api/v1/tasks?page=0").status_code == 422


def test_model_probe_exposes_only_public_result_and_requires_same_origin(client, monkeypatch):
    response = client.post("/api/v1/model/check")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "model_unconfigured"

    async def probe(_self):
        return {
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "status": "available",
            "checked_at": 1,
            "message": "fixture",
        }

    monkeypatch.setattr("buildmedic.api.ModelClient.probe", probe)
    assert client.post("/api/v1/model/check").json()["data"]["status"] == "available"
    assert (
        client.post("/api/v1/model/check", headers={"Origin": "https://attacker.invalid"}).status_code == 403
    )


def test_adaptive_creation_requires_real_capability_and_enough_budget(client, monkeypatch):
    monkeypatch.setattr("buildmedic.api.capability", lambda: {"ready": True, "message": "test capability"})
    body = dict(payload(), mode="adaptive")
    body["limits"]["max_model_calls"] = 4
    response = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "adaptive-budget-key"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "adaptive_budget_too_small"
    body["limits"].update(max_model_calls=12, max_tokens=100000)
    response = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "adaptive-budget-key"})
    assert response.status_code == 202
    tid = response.json()["data"]["task_id"]
    assert store.snapshot(tid)["mode"] == "adaptive"
    monkeypatch.setattr("buildmedic.api.capability", lambda: {"ready": False, "message": "disabled"})
    assert (
        client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "adaptive-budget-key"}).json()[
            "data"
        ]["task_id"]
        == tid
    )


def test_adaptive_rejects_remote_source_before_network_access(client, monkeypatch):
    monkeypatch.setattr("buildmedic.api.capability", lambda: {"ready": True, "message": "test capability"})
    body = {
        "source": {
            "type": "github_actions",
            "run_url": "https://github.com/a/b/actions/runs/1",
            "run_attempt": 1,
            "job_id": 2,
        },
        "mode": "adaptive",
    }
    response = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "adaptive-remote-key"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "adaptive_source_unsupported"


def test_experience_is_explicit_and_uses_a_separate_new_task_budget(client, monkeypatch):
    monkeypatch.setattr("buildmedic.api.capability", lambda: {"ready": True, "message": "test capability"})
    body = dict(payload(), experience=True)
    headers = {"Idempotency-Key": "experience-budget-key"}
    assert client.post("/api/v1/tasks", json=body, headers=headers).status_code == 422
    body["mode"] = "adaptive"
    body["limits"].update(max_model_calls=7, max_tokens=150000)
    response = client.post("/api/v1/tasks", json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "adaptive_budget_too_small"
    body["limits"]["max_model_calls"] = 8
    response = client.post("/api/v1/tasks", json=body, headers=headers)
    assert response.status_code == 202
    tid = response.json()["data"]["task_id"]
    assert store.snapshot(tid)["experience"]["entries"] == []
    monkeypatch.setattr("buildmedic.api.capability", lambda: {"ready": False, "message": "disabled"})
    assert client.post("/api/v1/tasks", json=body, headers=headers).json()["data"]["task_id"] == tid
    body["experience"] = False
    assert (
        client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "standard-oversize"}).status_code
        == 422
    )
