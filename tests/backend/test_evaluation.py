import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from buildmedic import db, evaluation, store
from buildmedic.api import app
from buildmedic.cases import metadata
from buildmedic.config import settings
from buildmedic.errors import Blocked


def spec(**changes):
    return evaluation.ExperimentSpec.model_validate(
        {
            "name": "development-test",
            "cases": [
                {"case_id": "pagination", "snapshot_sha256": metadata("pagination")["snapshot_sha256"]}
            ],
            **changes,
        }
    )


def test_preregistration_is_idempotent_private_and_does_not_execute(client):
    body = spec().model_dump()
    response = client.post(
        "/api/v1/experiments", json=body, headers={"Idempotency-Key": "evaluation-test-key"}
    )
    assert response.status_code == 201, response.text
    first = response.json()["data"]
    eid = first["id"]
    assert first["planned"] == 2 and first["finished"] == 0
    assert all(s["planned"] == 1 and s["finished"] == 0 and s["cost_usd"] is None for s in first["summary"])
    assert client.get("/api/v1/tasks").json()["data"]["total"] == 0
    assert (
        client.post(
            "/api/v1/experiments", json=body, headers={"Idempotency-Key": "evaluation-test-key"}
        ).json()["data"]["id"]
        == eid
    )
    body["name"] = "changed"
    assert (
        client.post(
            "/api/v1/experiments", json=body, headers={"Idempotency-Key": "evaluation-test-key"}
        ).status_code
        == 409
    )
    assert client.get("/api/v1/experiments").json()["data"]["total"] == 1
    report = client.get(f"/api/v1/experiments/{eid}/report")
    assert (
        evaluation.recompute(report.json()) == first and "attachment" in report.headers["content-disposition"]
    )
    with TestClient(app) as other:
        for suffix, method in [("", "GET"), ("/report", "GET"), ("/advance", "POST"), ("/cancel", "POST")]:
            assert other.request(method, f"/api/v1/experiments/{eid}{suffix}").status_code == 404
        assert other.get("/api/v1/experiments").json()["data"]["total"] == 0


def test_concurrent_advance_atomic_outbox_and_fixed_denominators():
    eid = evaluation.create("owner", "evaluation-test-key", spec())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: evaluation.advance(eid, "owner"), range(8)))
    tids = {r["task_id"] for r in results}
    assert len(tids) == 1
    tid = tids.pop()
    with db.transaction() as conn:
        assert len(list(conn.execute(select(db.outbox)))) == 1
    token, *_ = store.claim(tid)
    store.finish(tid, token, "completed", "blocked", "Explicit test environment block")
    second = evaluation.advance(eid, "owner")["task_id"]
    assert second != tid
    detail = evaluation.get(eid, "owner")
    assert detail["finished"] == 1
    assert [(s["public_verified"], s["planned"]) for s in detail["summary"]] == [(0, 1), (0, 1)]
    assert detail["summary"][0]["blocked"] == 1
    evaluation.cancel(eid, "owner")
    stopped = evaluation.get(eid, "owner")
    assert stopped["status"] == "cancelled" and stopped["finished"] == 2
    assert stopped["rows"][1]["metrics"]["lifecycle"] == "cancelled"
    assert evaluation.advance(eid, "owner") == {"status": "cancelled", "task_id": None}


def test_drift_or_quota_cannot_partially_create_next_slot(monkeypatch):
    eid = evaluation.create("owner", "evaluation-test-key", spec())
    monkeypatch.setattr(settings(), "model_id", "changed-model")
    with pytest.raises(Blocked):
        evaluation.advance(eid, "owner")
    monkeypatch.setattr(settings(), "model_id", "")
    monkeypatch.setattr(settings(), "max_active_tasks", 0)
    with pytest.raises(store.Conflict):
        evaluation.advance(eid, "owner")
    assert evaluation.get(eid, "owner")["rows"][0]["task_id"] is None
    assert store.list_tasks("owner")["total"] == 0


@pytest.mark.parametrize("change", ["duplicate", "foreign", "small", "unknown", "split", "experience"])
def test_manifest_rejects_unsupported_or_ambiguous_design(change):
    body = spec().model_dump()
    if change == "duplicate":
        body["cases"] *= 2
    if change == "foreign":
        body["cases"][0]["case_id"] = "private-hidden"
    if change == "small":
        body["limits"]["max_model_calls"] = 2
    if change == "unknown":
        body["metrics"] = {"verified": 100}
    if change == "split":
        body["dataset"] = "held-out"
    if change == "experience":
        body["experience"] = True
    with pytest.raises(ValueError):
        evaluation.ExperimentSpec.model_validate(body)


def test_archive_checksum_stops_silent_metric_changes():
    eid = evaluation.create("owner", "evaluation-test-key", spec())
    tid = evaluation.advance(eid, "owner")["task_id"]
    store.cancel(tid, "owner")
    evaluation.advance(eid, "owner")
    with db.transaction() as conn:
        row = evaluation.load(conn, eid, "owner")
        content = copy.deepcopy(row["content"])
        content["slots"][0]["frozen"]["snapshot"]["usage"]["tokens"] += 1
        conn.execute(update(db.experiments).where(db.experiments.c.id == eid).values(content=content))
    with pytest.raises(store.Conflict, match="指纹"):
        evaluation.get(eid, "owner")


def test_claiming_repaired_without_independent_evidence_is_never_verified():
    eid = evaluation.create("owner", "evaluation-test-key", spec())
    tid = evaluation.advance(eid, "owner")["task_id"]
    snap = store.snapshot(tid)
    snap.update(lifecycle="completed", outcome="repaired")
    assert evaluation.observed(snap)["public_verified"] is False


def test_run_order_is_alternated_before_any_execution():
    body = spec(repeats=2)
    eid = evaluation.create("owner", "evaluation-test-key", body)
    rows = evaluation.get(eid, "owner")["rows"]
    assert [(r["repeat"], r["mode"]) for r in rows] == [
        (1, "standard"),
        (1, "adaptive"),
        (2, "adaptive"),
        (2, "standard"),
    ]
    wrong = spec()
    wrong.cases[0].snapshot_sha256 = "0" * 64
    with pytest.raises(store.Conflict):
        evaluation.create("owner", "another-evaluation-key", wrong)


def four_arm_spec(**changes):
    record = evaluation.captured_log("pagination")
    return evaluation.parse_spec(
        {
            **spec().model_dump(),
            "schema_version": "2.0",
            "cases": [
                {
                    "case_id": "pagination",
                    "snapshot_sha256": record.snapshot_sha256,
                    "log_sha256": record.log_sha256,
                }
            ],
            **changes,
        }
    )


def test_four_arm_api_pins_logs_and_keeps_unscored_diagnosis_separate(client):
    cases = client.get("/api/v1/evaluation/cases").json()["data"]
    assert len(cases) == 3 and all(c["log_origin"] == "local-docker-target-capture" for c in cases)
    response = client.post(
        "/api/v1/experiments",
        json=four_arm_spec().model_dump(),
        headers={"Idempotency-Key": "four-arm-api-test"},
    )
    assert response.status_code == 201, response.text
    detail = response.json()["data"]
    assert detail["planned"] == 4
    assert [s["mode"] for s in detail["summary"]] == ["logs", "standard", "multi", "adaptive"]
    assert all(s["diagnosis_accuracy"] is None for s in detail["summary"])
    assert detail["summary"][0]["repair_applicable"] is False
    tid = client.post(f"/api/v1/experiments/{detail['id']}/advance").json()["data"]["task_id"]
    task = client.get(f"/api/v1/tasks/{tid}").json()["data"]
    assert task["experiment_binding"]["arm"] == "logs"
    assert task["evaluation_log"]["log_sha256"] == cases[0]["log_sha256"]
    assert (
        client.post(
            f"/api/v1/tasks/{tid}/retry", headers={"Idempotency-Key": "forbidden-experiment-retry"}
        ).status_code
        == 422
    )
    current = client.get(f"/api/v1/experiments/{detail['id']}").json()["data"]
    assert evaluation.recompute(client.get(f"/api/v1/experiments/{detail['id']}/report").json()) == current


@pytest.mark.parametrize(
    "variant", ["missing_log", "bad_log", "too_many", "zero_attempts", "injected_execution"]
)
def test_four_arm_manifest_cannot_change_boundaries(client, variant):
    body = four_arm_spec().model_dump()
    if variant == "missing_log":
        body["cases"][0].pop("log_sha256")
    elif variant == "bad_log":
        body["cases"][0]["log_sha256"] = "0" * 64
    elif variant == "too_many":
        body["multi_attempts"] = 5
    elif variant == "zero_attempts":
        body["multi_attempts"] = 0
    else:
        body["execution_policy"] = {"shell": "arbitrary"}
    r = client.post("/api/v1/experiments", json=body, headers={"Idempotency-Key": "invalid-four-arm"})
    assert r.status_code == (409 if variant == "bad_log" else 422)
