import copy

import pytest
from sqlalchemy import select, update

from buildmedic import db, evaluation, evaluation_analysis, store
from buildmedic.cases import EVALUATION_CASE_IDS
from buildmedic.config import settings
from buildmedic.grouped_evaluation import plan, registered_dataset
from buildmedic.scoring_protocol import fingerprint


def configure(monkeypatch):
    monkeypatch.setattr(settings(), "runner_backend", "docker")
    monkeypatch.setattr(settings(), "runner_image", evaluation.captured_log("pagination").image_digest)


def test_registered_twenty_case_catalog_and_complete_partition_plans():
    manifest = registered_dataset()
    assert len(manifest.cases) == len(EVALUATION_CASE_IDS) == 20
    for split in ("development", "test"):
        spec = plan(split)
        assert len(spec.cases) == 10 and spec.dataset_sha256 == fingerprint(manifest.model_dump())
        assert evaluation.parse_spec(spec.model_dump()) == spec


@pytest.mark.parametrize("variant", ["subset", "order", "mixed_split", "hash", "source", "log", "command"])
def test_grouped_protocol_rejects_cherry_picking_and_unbound_material(variant):
    body = plan("test").model_dump()
    if variant == "subset":
        body["cases"].pop()
    elif variant == "order":
        body["cases"].reverse()
    elif variant == "mixed_split":
        body["cases"][0] = plan("development").cases[0].model_dump()
    elif variant == "hash":
        body["dataset_sha256"] = "0" * 64
    elif variant in {"source", "log"}:
        body["cases"][0][variant.replace("source", "snapshot") + "_sha256"] = "0" * 64
    else:
        body["command"] = "arbitrary shell"
    with pytest.raises(ValueError):
        evaluation.parse_spec(body)


def test_preregistration_api_freezes_group_binding_and_has_no_model_work(client, monkeypatch):
    configure(monkeypatch)
    presets = client.get("/api/v1/evaluation/grouped-plans").json()["data"]
    response = client.post(
        "/api/v1/experiments", json=presets["test"], headers={"Idempotency-Key": "grouped-api"}
    )
    assert response.status_code == 201, response.text
    report = response.json()["data"]
    assert report["planned"] == 40 and report["finished"] == 0
    assert report["manifest"]["partition"] == "test" and "公开构造" in report["scope"]
    assert client.get("/api/v1/tasks").json()["data"]["total"] == 0
    tid = client.post(f"/api/v1/experiments/{report['id']}/advance").json()["data"]["task_id"]
    snap = client.get(f"/api/v1/tasks/{tid}").json()["data"]
    assert snap["case_id"] == "graph-cycle" and snap["experiment_binding"]["partition"] == "test"
    assert snap["experiment_binding"]["dataset_sha256"] == report["manifest"]["dataset_sha256"]
    assert snap["usage"]["calls"] == 0 and snap["experiment_binding"]["arm"] == "logs"
    assert len(client.get("/api/v1/demo-cases").json()["data"]) == 5


def test_frozen_grouped_report_recomputes_without_current_catalog_or_sources(monkeypatch):
    configure(monkeypatch)
    eid = evaluation.create("owner", "grouped-freeze", plan("test"))
    while True:
        tid = evaluation.advance(eid, "owner")["task_id"]
        if not tid:
            break
        store.cancel(tid, "owner")
    report = evaluation.export_report(eid, "owner")
    assert report["finished"] == report["planned"] == 40
    assert all(s["planned"] == 10 and s["public_verified"] == 0 for s in report["summary"])

    def forbidden(*args, **kwargs):
        raise AssertionError("offline recomputation cannot read current source/catalog")

    monkeypatch.setattr("buildmedic.cases.source_hash", forbidden)
    monkeypatch.setattr("buildmedic.grouped_evaluation.registered_dataset", forbidden)
    assert evaluation.recompute(report) == {k: v for k, v in report.items() if k != "inputs"}
    analysis = evaluation_analysis.analyze(report)
    assert analysis["scope"] == "public-constructed-grouped-descriptive"
    changed = copy.deepcopy(report)
    changed["rows"].pop()
    changed["inputs"].pop()
    with pytest.raises(store.Conflict):
        evaluation.recompute(changed)


@pytest.mark.parametrize("variant", ["image", "backend", "unregistered"])
def test_grouped_registration_requires_pinned_docker_catalog(monkeypatch, variant):
    configure(monkeypatch)
    spec = plan("development")
    if variant == "image":
        monkeypatch.setattr(settings(), "runner_image", "sha256:" + "0" * 64)
    elif variant == "backend":
        monkeypatch.setattr(settings(), "runner_backend", "trusted-local")
    else:
        body = spec.model_dump()
        body["dataset_manifest"]["cases"][-1]["repository_group"] = "renamed"
        body["dataset_sha256"] = fingerprint(body["dataset_manifest"])
        spec = evaluation.parse_spec(body)
    with pytest.raises(store.Conflict):
        evaluation.create("owner", "grouped-invalid", spec)
    with db.engine.connect() as conn:
        assert not conn.execute(select(db.experiments)).first()


def test_frozen_grouped_task_cannot_claim_another_partition(monkeypatch):
    configure(monkeypatch)
    eid = evaluation.create("owner", "grouped-binding", plan("test"))
    tid = evaluation.advance(eid, "owner")["task_id"]
    store.cancel(tid, "owner")
    evaluation.advance(eid, "owner")
    with db.transaction() as conn:
        content = copy.deepcopy(evaluation.load(conn, eid, "owner")["content"])
        frozen = content["slots"][0]["frozen"]
        frozen["snapshot"]["experiment_binding"]["partition"] = "development"
        frozen["sha256"] = evaluation.digest(frozen["snapshot"])
        conn.execute(update(db.experiments).where(db.experiments.c.id == eid).values(content=content))
    with pytest.raises(store.Conflict):
        evaluation.get(eid, "owner")
