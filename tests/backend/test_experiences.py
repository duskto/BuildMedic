"""Publication cannot turn model text or an unsuccessful task into trusted history."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from buildmedic import db, experiences, store
from buildmedic.api import app
from buildmedic.cases import metadata
from buildmedic.config import HARNESS_SHA, ROOT, settings
from buildmedic.experiences import digest
from buildmedic.schemas import CreateTask, Source
from buildmedic.security import read_owner


def task(owner="owner", key=None, parent=None, case="pagination"):
    body = CreateTask(
        source=Source(case_id=case),
        mode="adaptive",
        experience=True,
        limits={"max_tokens": 100000, "max_model_calls": 12},
    ).model_dump()
    return store.create(owner, key or store.uid(), body, metadata(case), parent)


def completed(owner="owner", *, outcome="repaired"):
    """Server-owned evidence fixture. Native scheduling is exercised separately."""
    tid = task(owner)
    token, *_ = store.claim(tid)
    base = ROOT / "harnesses" / "base"
    tree = {p.relative_to(base).as_posix(): p.read_text() for p in base.rglob("*") if p.is_file()}
    harness = store.artifact(tid, "harness_snapshot", json.dumps(tree), token, "application/json")
    patch = store.artifact(tid, "patch", "synthetic patch fixture", token)
    ev = store.artifact(tid, "baseline_log", "synthetic baseline failure", token)
    candidate = {
        "id": store.uid(),
        "selected": True,
        "rollout_id": store.uid(),
        "patch_artifact_id": patch["id"],
        "patch_hash": patch["sha256"],
        "explanation": "fixture",
        "files": [],
    }
    call = {
        "id": store.uid(),
        "role": "experience_reflection",
        "status": "completed",
        "model": "fixture",
        "started_at": db.now(),
        "total_tokens": 1,
        "usage_known": True,
        "reserved_tokens": 10,
        "provider": "fixture",
        "harness_hash": digest(tree),
    }
    validation = {
        "id": store.uid(),
        "kind": "independent",
        "status": "passed",
        "candidate_id": candidate["id"],
        "target": {"patch_hash": patch["sha256"]},
        "regression": {"patch_hash": patch["sha256"]},
    }
    draft = {
        "has_lesson": True,
        "cue": "Slice boundary mismatch",
        "lesson": "Check inclusive and exclusive boundary contracts.",
        "edits": "Cross-check call sites.",
        "reason": "",
        "evidence_ids": ["ev-fixture"],
        "tree": tree,
        "harness_hash": digest(tree),
        "profile_hash": experiences.profile_hash(),
        "candidate_id": candidate["id"],
        "validation_id": validation["id"],
        "model_call_id": call["id"],
    }
    art = store.artifact(tid, "experience_draft", json.dumps(draft), token, "application/json")

    def fill(s):
        s.update(
            reproduction_status="reproduced",
            validation_status="passed",
            harness2_sha=HARNESS_SHA,
            evidence=[
                {
                    "id": "ev-fixture",
                    "kind": "baseline_log",
                    "artifact_id": ev["id"],
                    "path": None,
                    "line_start": 1,
                    "line_end": 1,
                    "excerpt": "fixture",
                }
            ],
            candidates=[candidate],
            validations=[validation],
            model_calls=[call],
            rollouts=[
                {
                    "id": candidate["rollout_id"],
                    "label": "c1",
                    "status": "passed",
                    "harness_hash": digest(tree),
                    "harness_artifact_id": harness["id"],
                }
            ],
        )
        s["experience"].update(
            reflection_status="available",
            draft={
                "artifact_id": art["id"],
                "cue": draft["cue"],
                "lesson": draft["lesson"],
                "evidence_ids": draft["evidence_ids"],
                "model_call_id": call["id"],
            },
        )

    store.mutate(tid, "fixture.evidence", transform=fill, token=token)
    store.finish(tid, token, "completed", outcome)
    return tid


def test_publication_freezes_inputs_retries_and_revocation_preserves_history():
    first = completed()
    empty = store.snapshot(first)["experience"]
    assert not empty["entries"]
    version = experiences.publish(first, "owner", "publish-first")
    assert experiences.publish(first, "owner", "publish-first") == version
    assert experiences.publish(first, "owner", "publish-second-key") == version
    next_task = task()
    bound = store.snapshot(next_task)["experience"]
    assert [e["id"] for e in bound["entries"]] == [version["id"]]
    assert experiences.get_version(version["id"], "owner")["content"]["parent_version_ids"] == []
    experiences.revoke(version["id"], "owner")
    experiences.revoke(version["id"], "owner")
    later = task()
    assert store.snapshot(later)["experience"]["entries"] == []
    store.cancel(next_task, "owner")
    retry = task(parent=next_task)
    retried = store.snapshot(retry)["experience"]
    assert retried["snapshot_id"] != bound["snapshot_id"]
    assert retried["snapshot_hash"] == bound["snapshot_hash"]
    assert retried["entries"] == bound["entries"]
    assert (
        experiences.get_snapshot(bound["snapshot_id"], "owner")["content"]["entries"][0]["id"]
        == version["id"]
    )
    assert experiences.get_snapshot(empty["snapshot_id"], "owner")["content"]["entries"] == []
    store.cancel(later, "owner")


def test_serialized_publish_handles_duplicate_and_concurrent_tasks(monkeypatch):
    monkeypatch.setattr(settings(), "max_active_tasks", 10)
    first, second = completed(), completed()
    with ThreadPoolExecutor(max_workers=4) as pool:
        published = list(
            pool.map(lambda t: experiences.publish(t, "owner", "key-" + t), [first, second, first, second])
        )
    assert published[0]["id"] == published[2]["id"]
    assert published[1]["id"] == published[3]["id"]
    assert {v["version"] for v in published} == {1, 2}
    with pytest.raises(store.Conflict, match="其他经验"):
        experiences.publish(second, "owner", "key-" + first)
    assert experiences.list_versions("owner")["total"] == 2
    bound = store.snapshot(task())["experience"]
    assert bound["entries"][0]["version"] == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "outcome",
        "validation",
        "patch_hash",
        "rollout",
        "evidence",
        "call_role",
        "artifact",
        "evidence_artifact",
    ],
)
def test_publication_rejects_unverified_or_mismatched_evidence(mutation):
    tid = completed(outcome="unresolved" if mutation == "outcome" else "repaired")
    with db.transaction() as conn:
        row = store.row(conn, tid)
        s = copy.deepcopy(row["snapshot"])
        if mutation == "validation":
            s["validations"][0]["kind"] = "candidate"
        elif mutation == "patch_hash":
            s["validations"][0]["target"]["patch_hash"] = "another-patch"
        elif mutation == "rollout":
            s["rollouts"][0]["harness_hash"] = "another-tree"
        elif mutation == "evidence":
            s["evidence"] = []
        elif mutation == "call_role":
            s["model_calls"][0]["role"] = "diagnosis"
        elif mutation == "artifact":
            conn.execute(
                update(db.artifacts)
                .where(db.artifacts.c.id == s["experience"]["draft"]["artifact_id"])
                .values(content="{}")
            )
        elif mutation == "evidence_artifact":
            conn.execute(
                update(db.artifacts)
                .where(db.artifacts.c.id == s["evidence"][0]["artifact_id"])
                .values(content="corrupted evidence")
            )
        conn.execute(update(db.tasks).where(db.tasks.c.id == tid).values(snapshot=s))
    with pytest.raises(store.Conflict):
        experiences.publish(tid, "owner", "invalid-evidence")
    assert experiences.list_versions("owner")["total"] == 0


def test_owner_and_domain_isolation_and_incompatible_profile(monkeypatch):
    version = experiences.publish(completed(), "owner", "publish-first")
    assert experiences.list_versions("someone-else")["total"] == 0
    for lookup in (
        lambda: experiences.get_version(version["id"], "someone-else"),
        lambda: experiences.revoke(version["id"], "someone-else"),
        lambda: experiences.publish(version["source_task_id"], "someone-else", "publish-first"),
    ):
        with pytest.raises(KeyError):
            lookup()
    assert not store.snapshot(task("someone-else"))["experience"]["entries"]
    assert not store.snapshot(task(case="imports"))["experience"]["entries"]
    monkeypatch.setattr(experiences, "profile_hash", lambda: "new-platform-profile")
    assert not store.snapshot(task())["experience"]["entries"]


def test_http_publication_privacy_and_frozen_snapshot_access(client):
    owner = read_owner(client.cookies.get("buildmedic_session"))
    tid = completed(owner)
    sid = store.snapshot(tid)["experience"]["snapshot_id"]
    assert client.post(f"/api/v1/tasks/{tid}/experience").status_code == 422
    response = client.post(f"/api/v1/tasks/{tid}/experience", headers={"Idempotency-Key": "publish-http"})
    assert response.status_code == 200, response.text
    vid = response.json()["data"]["id"]
    assert client.get("/api/v1/experience").json()["data"]["total"] == 1
    assert client.get(f"/api/v1/experience/{vid}").json()["data"]["content"]["source_task_id"] == tid
    assert client.get(f"/api/v1/experience/snapshots/{sid}").status_code == 200
    with TestClient(app) as other:
        assert other.get("/api/v1/experience").json()["data"]["total"] == 0
        for path in (f"/api/v1/experience/{vid}", f"/api/v1/experience/snapshots/{sid}"):
            assert other.get(path).status_code == 404
        assert other.post(f"/api/v1/experience/{vid}/revoke").status_code == 404
        assert (
            other.post(
                f"/api/v1/tasks/{tid}/experience", headers={"Idempotency-Key": "publish-http"}
            ).status_code
            == 404
        )
    assert client.post(f"/api/v1/experience/{vid}/revoke").json()["data"]["status"] == "revoked"


def test_request_idempotency_compatibility_and_frozen_binding():
    legacy = CreateTask(source=Source(case_id="pagination")).model_dump(exclude={"experience"})
    current = dict(legacy, experience=False)
    assert store.request_hash(legacy) == store.request_hash(current)
    tid = task(key="same-request")
    experiences.publish(completed(), "owner", "publish-first")
    assert task(key="same-request") == tid
    assert store.snapshot(tid)["experience"]["entries"] == []
    with db.transaction() as conn:
        assert conn.scalar(select(func.count()).select_from(db.experience_snapshots)) == 2
