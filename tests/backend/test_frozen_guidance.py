"""Synthetic published history; actual native execution is tested in integration."""

import copy

import pytest
from sqlalchemy import select, update
from test_experiences import completed

from buildmedic import db, evaluation, evaluation_analysis, experiences, store
from buildmedic.cases import metadata
from buildmedic.config import settings
from buildmedic.frozen_guidance import FrozenGuidance, available, model_material
from buildmedic.grouped_evaluation import plan
from buildmedic.security import read_owner


def publish(owner="owner", *, case="pagination"):
    tid = completed(owner)
    with db.transaction() as conn:
        snap = copy.deepcopy(store.row(conn, tid, owner)["snapshot"])
        snap.update(case_id=case, snapshot_sha256=metadata(case)["snapshot_sha256"])
        snap["usage"].update(calls=2, tokens=120, usage_known=True)
        conn.execute(update(db.tasks).where(db.tasks.c.id == tid).values(snapshot=snap))
    return experiences.publish(tid, owner, store.uid())


def spec(ids, partition="test"):
    value = plan(partition).model_dump()
    value.update(schema_version="4.0", guidance_version_ids=ids)
    return evaluation.parse_spec(value)


def configure(monkeypatch):
    monkeypatch.setattr(settings(), "runner_backend", "docker")
    monkeypatch.setattr(settings(), "runner_image", evaluation.captured_log("pagination").image_digest)


def test_explicit_development_versions_freeze_for_all_repair_arms_and_survive_revocation(monkeypatch):
    configure(monkeypatch)
    version = publish()
    eid = evaluation.create("owner", "frozen-guidance", spec([version["id"]]))
    original = evaluation.get(eid, "owner")["frozen_guidance"]
    assert original["version_ids"] == [version["id"]]
    assert original["training_usage"] == {
        "tasks": 1,
        "calls": 2,
        "tokens": 120,
        "unknown_usage_tasks": 0,
        "cost_usd": None,
    }
    experiences.revoke(version["id"], "owner")
    newer = publish()
    assert newer["id"] != version["id"]
    assert evaluation.create("owner", "frozen-guidance", spec([version["id"]])) == eid
    projections = []
    for arm in ("logs", "standard", "multi", "adaptive"):
        tid = evaluation.advance(eid, "owner")["task_id"]
        snap = store.snapshot(tid, "owner")
        assert snap["experiment_binding"]["arm"] == arm
        assert snap["experiment_binding"]["guidance_sha256"] == original["sha256"]
        if arm == "logs":
            assert model_material(snap) is None
        else:
            projections.append(model_material(snap))
            assert snap["frozen_guidance"] == original
        assert not snap.get("experience")
        store.cancel(tid, "owner")
    assert projections[0] == projections[1] == projections[2]
    assert '"source_task_id"' not in str(projections[0]) and "evidence_ids" not in str(projections[0])
    evaluation.cancel(eid, "owner")
    report = evaluation.export_report(eid, "owner")
    assert report["frozen_guidance"] == original
    assert newer["id"] not in str(report["frozen_guidance"])
    monkeypatch.setattr(
        "buildmedic.experiences.profile_hash", lambda: (_ for _ in ()).throw(AssertionError("offline"))
    )
    monkeypatch.setattr(
        "buildmedic.cases.source_hash", lambda _: (_ for _ in ()).throw(AssertionError("offline"))
    )
    assert evaluation.recompute(report)["report_sha256"] == report["report_sha256"]
    assert evaluation_analysis.analyze(report)["scope"] == "public-constructed-frozen-guidance-descriptive"


@pytest.mark.parametrize(
    "variant",
    ["test_source", "test_binding", "revoked", "foreign", "profile", "source_hash", "cycle", "test_ancestor"],
)
def test_test_cases_incompatible_or_foreign_history_cannot_enter_frozen_guidance(monkeypatch, variant):
    configure(monkeypatch)
    version = publish(
        "other" if variant == "foreign" else "owner",
        case="graph-cycle" if variant == "test_source" else "pagination",
    )
    if variant == "revoked":
        experiences.revoke(version["id"], "owner")
    if variant in {"profile", "cycle", "test_ancestor"}:
        ancestor = publish(case="graph-cycle") if variant == "test_ancestor" else None
        with db.transaction() as conn:
            row = (
                conn.execute(
                    select(db.experience_versions).where(db.experience_versions.c.id == version["id"])
                )
                .mappings()
                .one()
            )
            content = copy.deepcopy(row["content"])
            if variant == "profile":
                content["profile_hash"] = "0" * 64
            else:
                content["parent_version_ids"] = [ancestor["id"] if ancestor else version["id"]]
            conn.execute(
                update(db.experience_versions)
                .where(db.experience_versions.c.id == version["id"])
                .values(content=content, content_hash=experiences.digest(content))
            )
    if variant in {"source_hash", "test_binding"}:
        with db.transaction() as conn:
            snap = copy.deepcopy(store.row(conn, version["source_task_id"], "owner")["snapshot"])
            if variant == "source_hash":
                snap["snapshot_sha256"] = "0" * 64
            else:
                snap["experiment_binding"] = {"partition": "test"}
            conn.execute(
                update(db.tasks).where(db.tasks.c.id == version["source_task_id"]).values(snapshot=snap)
            )
    with pytest.raises((store.Conflict, KeyError)):
        evaluation.create("owner", "invalid-guidance", spec([version["id"]]))
    with db.engine.connect() as conn:
        assert not conn.execute(select(db.experiments)).first()


def test_parent_training_usage_is_deduplicated_and_empty_control_is_explicit(monkeypatch):
    configure(monkeypatch)
    first, second = publish(), publish()
    eid = evaluation.create("owner", "guidance-lineage", spec([second["id"], first["id"]]))
    pack = evaluation.get(eid, "owner")["frozen_guidance"]
    assert len(pack["ancestry"]) == pack["training_usage"]["tasks"] == 2
    assert pack["training_usage"]["calls"] == 4 and pack["training_usage"]["tokens"] == 240
    assert pack["version_ids"] == [second["id"], first["id"]]
    empty = evaluation.create("owner", "empty-control", spec([]))
    control = evaluation.get(empty, "owner")["frozen_guidance"]
    assert control["entries"] == control["ancestry"] == [] and control["training_usage"]["cost_usd"] == 0
    assert FrozenGuidance.model_validate(control).sha256 == control["sha256"]


def test_candidates_are_owner_scoped_and_only_include_eligible_development_sources():
    eligible = publish()
    publish(case="graph-cycle")
    publish("other")
    assert [e["id"] for e in available("owner")["items"]] == [eligible["id"]]


@pytest.mark.parametrize("variant", ["hash", "rehashed_task", "training", "ancestry", "selection"])
def test_frozen_input_tampering_is_rejected(monkeypatch, variant):
    configure(monkeypatch)
    version = publish()
    eid = evaluation.create("owner", "guidance-tamper", spec([version["id"]]))
    tid = evaluation.advance(eid, "owner")["task_id"]
    store.cancel(tid, "owner")
    tid = evaluation.advance(eid, "owner")["task_id"]
    store.cancel(tid, "owner")
    evaluation.advance(eid, "owner")
    report = evaluation.export_report(eid, "owner")
    if variant == "rehashed_task":
        frozen = report["inputs"][1]
        frozen["snapshot"]["frozen_guidance"]["entries"][0]["lesson"] = "changed"
        frozen["sha256"] = evaluation.digest(frozen["snapshot"])
        report["rows"][1]["snapshot_sha256"] = frozen["sha256"]
    else:
        pack = report["frozen_guidance"]
        if variant == "hash":
            pack["sha256"] = "0" * 64
        elif variant == "training":
            pack["training_usage"]["calls"] += 1
        elif variant == "selection":
            pack["version_ids"] = []
        else:
            pack["ancestry"] = []
    with pytest.raises((store.Conflict, ValueError)):
        evaluation.recompute(report)


@pytest.mark.parametrize("ids", [["not-a-version"], ["a" * 32] * 2, [f"{i:032x}" for i in range(6)]])
def test_selection_is_bounded_and_explicit(ids):
    with pytest.raises(ValueError):
        spec(ids)


def test_guidance_api_selection_is_owner_scoped_and_registration_makes_no_model_tasks(client, monkeypatch):
    from fastapi.testclient import TestClient

    from buildmedic.api import app

    configure(monkeypatch)
    owner = read_owner(client.cookies.get("buildmedic_session"))
    version = publish(owner)
    response = client.get("/api/v1/evaluation/guidance-versions")
    assert response.status_code == 200 and [e["id"] for e in response.json()["data"]["items"]] == [
        version["id"]
    ]
    body = spec([version["id"]]).model_dump()
    result = client.post("/api/v1/experiments", json=body, headers={"Idempotency-Key": "guidance-api"})
    assert result.status_code == 201, result.text
    data = result.json()["data"]
    assert data["frozen_guidance"]["version_ids"] == [version["id"]] and data["finished"] == 0
    assert client.get("/api/v1/tasks").json()["data"]["total"] == 1  # Only the synthetic teaching task.
    with TestClient(app) as other:
        assert other.get("/api/v1/evaluation/guidance-versions").json()["data"]["items"] == []
        assert other.get(f"/api/v1/experiments/{data['id']}").status_code == 404
        assert (
            other.post(
                "/api/v1/experiments", json=body, headers={"Idempotency-Key": "foreign-guidance"}
            ).status_code
            == 404
        )


def test_frozen_guidance_experiments_keep_detached_scoring_and_diagnosis_review_compatible(
    monkeypatch,
    grouped_scoring_bundle,
):
    import asyncio

    from test_grouped_scoring import TOKEN, Client

    from buildmedic import diagnosis_scoring, scoring
    from buildmedic.fingerprints import fingerprint

    configure(monkeypatch)
    version = publish()
    eid = evaluation.create("owner", "guidance-compatible-score", spec([version["id"]]))
    while True:
        tid = evaluation.advance(eid, "owner")["task_id"]
        if not tid:
            break
        store.cancel(tid, "owner")
    before = evaluation.export_report(eid, "owner")
    for key, value in {
        "scorer_url": "http://127.0.0.1:8002",
        "scorer_token": TOKEN,
        "scorer_suite_sha256": fingerprint(grouped_scoring_bundle.model_dump()),
        "scorer_dataset": "public-constructed-grouped",
        "diagnosis_review_key": "synthetic-review-key-" + "k" * 40,
        "diagnosis_rubric_sha256": "f" * 64,
    }.items():
        monkeypatch.setattr(settings(), key, value)
    scoring.reserve(eid, "owner")
    result = asyncio.run(scoring.get(eid, "owner", Client(grouped_scoring_bundle)))
    assert result["status"] == "completed" and result["report_sha256"] == before["report_sha256"]
    review = diagnosis_scoring.prepare(eid, "owner")
    assert review["report_sha256"] == before["report_sha256"] and len(review["rows"]) == 40
    assert "guidance_version_ids" not in str(review) and version["id"] not in str(review)
    assert before == evaluation.export_report(eid, "owner")


def test_tool_loop_receives_guidance_without_treating_it_as_source_or_evidence():
    from types import SimpleNamespace

    from buildmedic.agent_tools import ToolSession
    from buildmedic.schemas import CreateTask, Source

    body = CreateTask(source=Source(case_id="pagination"), agent_tools=True).model_dump()
    tid = store.create("owner", "tool-guidance", body, metadata("pagination"))
    token, *_ = store.claim(tid)
    artifact = store.artifact(tid, "repo_code", "def example():\n    return 1\n", token)
    evidence = {
        "id": "ev-current",
        "kind": "repo_code",
        "path": "src/demo/example.py",
        "artifact_id": artifact["id"],
    }
    store.mutate(tid, "test.evidence", {"evidence": [evidence]}, token=token)
    guidance = {
        "sha256": "a" * 64,
        "lessons": [{"cue": "published cue", "lesson": "published guidance", "edits": ""}],
    }
    materials = {
        "goal": "synthetic task",
        "source": [{"path": evidence["path"], "evidence_id": evidence["id"]}],
        "frozen_guidance": guidance,
    }
    session = ToolSession(SimpleNamespace(id=tid, body=body), materials, "synthetic-rollout", "pagination")
    assert session.initial["frozen_guidance"] == guidance
    assert session.initial["source_index"] == [
        {"path": evidence["path"], "evidence_id": "ev-current", "total_lines": 2}
    ]
    assert session.logs == {} and session.read_paths == set()
    store.finish(tid, token, "cancelled")
