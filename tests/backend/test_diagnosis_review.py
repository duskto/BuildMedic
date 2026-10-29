"""Synthetic human labels only: no real model diagnosis is adjudicated by these tests."""

import copy
import json

import pytest
from sqlalchemy import select, update

from buildmedic import db, diagnosis_scoring, evaluation, store
from buildmedic.config import ROOT, settings
from buildmedic.diagnosis_protocol import ReviewRequest, sign
from buildmedic.evaluation_dataset import DatasetManifest
from buildmedic.scoring_protocol import fingerprint
from buildmedic.security import read_owner
from scorer_service.diagnosis_review import (
    Rubric,
    Sheet,
    export_sheet,
    load_rubric,
    read_private,
    seal,
    write_private,
)

KEY = "synthetic-human-review-key-" + "x" * 40


def completed(owner="owner", *, finish=True):
    log = evaluation.captured_log("pagination")
    spec = evaluation.FourArmSpec(
        schema_version="2.0",
        name="diagnosis-test",
        cases=[
            {
                "case_id": "pagination",
                "snapshot_sha256": log.snapshot_sha256,
                "log_sha256": log.log_sha256,
            }
        ],
    )
    eid = evaluation.create(owner, "review-experiment", spec)
    if not finish:
        return eid
    while True:
        item = evaluation.advance(eid, owner)
        if not item["task_id"]:
            break
        store.cancel(item["task_id"], owner)
    with db.transaction() as conn:
        content = copy.deepcopy(evaluation.load(conn, eid, owner)["content"])
        for slot in content["slots"]:
            snap = slot["frozen"]["snapshot"]
            if slot["mode"] == "adaptive":
                continue  # Missing diagnosis must stay in the primary denominator.
            snap["diagnosis"] = {
                "schema_version": "1.0",
                "failure_category": "synthetic",
                "summary": "synthetic diagnosis",
                "root_cause": {
                    "description": "synthetic cause",
                    "evidence_ids": ["ev-synthetic"],
                    "certainty": "hypothesis",
                },
                "limitations": [],
                "next_action": "inspect source",
                "patch": "PRIVATE_PATCH",
                "edits": [],
            }
            snap["evidence"] = [
                {
                    "id": "ev-synthetic",
                    "kind": "ci_log",
                    "path": "ci.log",
                    "line_start": 1,
                    "line_end": 2,
                    "excerpt": "synthetic log",
                    "artifact_id": "PRIVATE_ARTIFACT",
                    "log_origin": {"cache": "PRIVATE_CACHE"},
                }
            ]
            slot["frozen"]["sha256"] = fingerprint(snap)
        conn.execute(update(db.experiments).where(db.experiments.c.id == eid).values(content=content))
    return eid


def rubric():
    log = evaluation.captured_log("pagination")
    return Rubric.model_validate(
        {
            "version": 1,
            "dataset": {
                "purpose": "public-development",
                "cases": [
                    {
                        "case_id": "pagination",
                        "repository_group": "synthetic-repo",
                        "template_group": "synthetic-template",
                        "split": "development",
                        "snapshot_sha256": log.snapshot_sha256,
                        "log_sha256": log.log_sha256,
                    }
                ],
            },
            "references": [
                {
                    "case_id": "pagination",
                    "root_cause": "PRIVATE_REFERENCE_CAUSE",
                    "location": "PRIVATE_REFERENCE_LOCATION",
                    "guidance": "PRIVATE_REFERENCE_GUIDANCE",
                }
            ],
        }
    )


def configure(monkeypatch):
    monkeypatch.setattr(settings(), "diagnosis_review_key", KEY)
    monkeypatch.setattr(settings(), "diagnosis_rubric_sha256", fingerprint(rubric().model_dump()))


def graded(request):
    sheet = export_sheet(request, rubric())
    sheet.reviewer = "synthetic-test-reviewer"
    sheet.human_review_complete = True
    for row in sheet.rows:
        if row.item["diagnosis"]:
            row.root_cause_correct = True
            row.location_correct = True
            row.notes = "PRIVATE_REVIEWER_NOTES"
    return sheet


def test_blinded_review_freezes_inputs_and_missing_denominator_and_is_immutable(monkeypatch):
    configure(monkeypatch)
    eid = completed()
    before = evaluation.export_report(eid, "owner")
    request = ReviewRequest.model_validate(diagnosis_scoring.prepare(eid, "owner"))
    assert diagnosis_scoring.prepare(eid, "owner") == request.model_dump()
    serialized = json.dumps(request.model_dump())
    for hidden in (
        "PRIVATE_PATCH",
        "PRIVATE_ARTIFACT",
        "PRIVATE_CACHE",
        "PRIVATE_REFERENCE",
        '"mode"',
        '"arm"',
        '"task_id"',
        '"models"',
        '"public_verified"',
        '"calls"',
    ):
        assert hidden not in serialized
    pending = diagnosis_scoring.get(eid, "owner")
    assert pending["status"] == "awaiting_review"
    assert all(r["diagnosis_accuracy"] is None for r in pending["summary"])
    assert sum(r["unreviewed"] for r in pending["summary"]) == 3
    sheet = graded(request)
    body = seal(request, rubric(), sheet, KEY)
    result = diagnosis_scoring.ingest(eid, "owner", body)
    summaries = {r["mode"]: r for r in result["summary"]}
    assert summaries["logs"]["diagnosis_accuracy"] == 1
    assert summaries["adaptive"]["diagnosis_accuracy"] == 0
    assert summaries["adaptive"]["missing"] == summaries["adaptive"]["planned"] == 1
    assert "PRIVATE_REFERENCE" not in json.dumps(result) and "PRIVATE_REVIEWER_NOTES" not in json.dumps(
        result
    )
    assert before == evaluation.export_report(eid, "owner")
    assert diagnosis_scoring.ingest(eid, "owner", body) == result
    monkeypatch.setattr(settings(), "diagnosis_review_key", "rotated")
    assert diagnosis_scoring.get(eid, "owner") == result
    changed = copy.deepcopy(body)
    changed["sealed_at"] += 1
    with pytest.raises(store.Conflict):
        diagnosis_scoring.ingest(eid, "owner", changed)
    with pytest.raises(KeyError):
        diagnosis_scoring.get(eid, "foreign")


@pytest.mark.parametrize(
    "mutation", ["signature", "report", "rubric", "request", "row", "missing", "duplicate", "fabricated"]
)
def test_tampered_signed_receipts_cannot_score_or_change_experiment(monkeypatch, mutation):
    configure(monkeypatch)
    eid = completed()
    before = evaluation.export_report(eid, "owner")
    request = ReviewRequest.model_validate(diagnosis_scoring.prepare(eid, "owner"))
    body = seal(request, rubric(), graded(request), KEY)
    if mutation == "signature":
        body["signature"] = "0" * 64
    else:
        if mutation == "report":
            body["report_sha256"] = "0" * 64
        elif mutation == "rubric":
            body["rubric_sha256"] = "0" * 64
        elif mutation == "request":
            body["request_sha256"] = "0" * 64
        elif mutation == "row":
            body["rows"][0]["assessment_sha256"] = "0" * 64
        elif mutation == "missing":
            body["rows"].pop()
        elif mutation == "duplicate":
            body["rows"][0] = body["rows"][1]
        else:
            rid = next(r.review_id for r in request.rows if r.diagnosis is None)
            row = next(r for r in body["rows"] if r["review_id"] == rid)
            row["root_cause_correct"] = row["location_correct"] = True
        body = sign(body, KEY)
    with pytest.raises(ValueError):
        diagnosis_scoring.ingest(eid, "owner", body)
    assert diagnosis_scoring.get(eid, "owner")["status"] == "awaiting_review"
    assert before == evaluation.export_report(eid, "owner")


@pytest.mark.parametrize(
    "mutation", ["unreviewed", "attestation", "reviewer", "reference", "diagnosis", "source", "integer"]
)
def test_review_sheet_requires_explicit_human_labels_and_exact_frozen_material(monkeypatch, mutation):
    configure(monkeypatch)
    request = ReviewRequest.model_validate(diagnosis_scoring.prepare(completed(), "owner"))
    sheet = graded(request).model_dump()
    row = next(r for r in sheet["rows"] if r["item"]["diagnosis"])
    if mutation == "unreviewed":
        row["location_correct"] = None
    elif mutation == "attestation":
        sheet["human_review_complete"] = False
    elif mutation == "reviewer":
        sheet["reviewer"] = "   "
    elif mutation == "reference":
        row["reference"]["location"] = "changed"
    elif mutation == "diagnosis":
        row["item"]["diagnosis"]["summary"] = "changed"
    elif mutation == "source":
        row["item"]["snapshot_sha256"] = "0" * 64
    else:
        row["root_cause_correct"] = 1
    with pytest.raises(ValueError):
        seal(request, rubric(), Sheet.model_validate(sheet), KEY)


def test_review_artifacts_are_external_private_and_do_not_overwrite(tmp_path):
    path = tmp_path / "private" / "rubric.json"
    write_private(path, rubric().model_dump())
    assert path.stat().st_mode & 0o777 == 0o600
    loaded, digest = load_rubric(path)
    assert digest == fingerprint(loaded.model_dump())
    assert read_private(path) == rubric().model_dump()
    write_private(path, rubric().model_dump())
    with pytest.raises(ValueError):
        write_private(path, {"different": True})
    with pytest.raises(ValueError):
        write_private(ROOT / "var" / "forbidden-review.json", {})
    link = tmp_path / "link"
    link.symlink_to(path.parent)
    with pytest.raises(ValueError):
        read_private(link / "rubric.json")


def test_public_api_reads_only_owned_receipts_and_rejects_client_labels(client, monkeypatch):
    configure(monkeypatch)
    owner = read_owner(client.cookies.get("buildmedic_session"))
    eid = completed(owner)
    base = f"/api/v1/experiments/{eid}/diagnosis-score"
    assert client.get(base).json()["data"] is None
    assert client.get(base + "/report").status_code == 404
    assert client.post(base, json={"root_cause_correct": True}).status_code == 405
    diagnosis_scoring.prepare(eid, owner)
    assert client.get(base).json()["data"]["status"] == "awaiting_review"
    download = client.get(base + "/report")
    assert download.status_code == 200 and download.headers["cache-control"] == "private, no-store"
    assert "PRIVATE_REFERENCE" not in download.text and '"diagnosis"' not in download.text
    client.cookies.clear()
    assert client.get(base).status_code == client.get(base + "/report").status_code == 404


def test_prepare_requires_completed_batch_and_does_not_create_model_or_runner_work(monkeypatch):
    configure(monkeypatch)
    eid = completed(finish=False)
    with pytest.raises(store.Conflict):
        diagnosis_scoring.prepare(eid, "owner")
    with db.engine.connect() as conn:
        assert not conn.execute(select(db.diagnosis_scores)).first()


def test_concurrent_prepare_is_identical_and_partial_correctness_is_not_accuracy(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    configure(monkeypatch)
    eid = completed()
    with ThreadPoolExecutor(max_workers=4) as pool:
        requests = list(pool.map(lambda _: diagnosis_scoring.prepare(eid, "owner"), range(8)))
    assert all(request == requests[0] for request in requests)
    request = ReviewRequest.model_validate(requests[0])
    sheet = graded(request)
    for row in sheet.rows:
        if row.item["diagnosis"]:
            row.location_correct = False
    result = diagnosis_scoring.ingest(eid, "owner", seal(request, rubric(), sheet, KEY))
    assert all(row["diagnosis_accuracy"] == 0 for row in result["summary"])
    assert sum(row["root_cause_correct"] for row in result["summary"]) == 3


def test_operator_cli_full_offline_round_trip_uses_only_synthetic_human_labels(tmp_path, monkeypatch):
    from buildmedic.diagnosis_cli import main as database_cli
    from scorer_service.diagnosis_review import main as review_cli

    configure(monkeypatch)
    eid = completed()
    frozen = evaluation.export_report(eid, "owner")
    request_path, rubric_path = tmp_path / "request.json", tmp_path / "rubric.json"
    sheet_path, receipt_path = tmp_path / "sheet.json", tmp_path / "receipt.json"
    write_private(rubric_path, rubric().model_dump())
    monkeypatch.setattr(
        "sys.argv", ["diagnosis_cli", "prepare", "--experiment", eid, "--file", str(request_path)]
    )
    database_cli()
    monkeypatch.setattr(
        "sys.argv",
        [
            "diagnosis_review",
            "export",
            "--request",
            str(request_path),
            "--rubric",
            str(rubric_path),
            "--output",
            str(sheet_path),
        ],
    )
    review_cli()
    request = ReviewRequest.model_validate(read_private(request_path))
    assert read_private(sheet_path) == export_sheet(request, rubric()).model_dump()
    # Simulated reviewer action, explicitly synthetic. Production labels must come from a human.
    sheet_path.write_text(json.dumps(graded(request).model_dump()))
    monkeypatch.setenv("BUILDMEDIC_DIAGNOSIS_REVIEW_KEY", KEY)
    monkeypatch.setattr(
        "sys.argv",
        [
            "diagnosis_review",
            "seal",
            "--request",
            str(request_path),
            "--rubric",
            str(rubric_path),
            "--sheet",
            str(sheet_path),
            "--output",
            str(receipt_path),
        ],
    )
    review_cli()
    monkeypatch.setattr(
        "sys.argv", ["diagnosis_cli", "import", "--experiment", eid, "--file", str(receipt_path)]
    )
    database_cli()
    assert diagnosis_scoring.get(eid, "owner")["status"] == "completed"
    assert frozen == evaluation.export_report(eid, "owner")
    with db.engine.connect() as conn:
        tasks = conn.execute(select(db.tasks)).mappings().all()
        assert len(tasks) == 4 and all(t["snapshot"]["usage"]["calls"] == 0 for t in tasks)


def grouped_manifest():
    return {
        "purpose": "grouped-benchmark",
        "cases": [
            {
                "case_id": f"synthetic-{i}",
                "repository_group": f"repo-{i}",
                "template_group": f"template-{i}",
                "split": "development" if i < 10 else "test",
                "snapshot_sha256": fingerprint(["source", i]),
                "log_sha256": fingerprint(["log", i]),
            }
            for i in range(20)
        ],
    }


def test_grouped_manifest_validates_repository_and_template_isolation():
    manifest = DatasetManifest.model_validate(grouped_manifest())
    assert len(manifest.cases) == 20
    assert fingerprint(manifest.model_dump()) == fingerprint(
        DatasetManifest.model_validate(manifest.model_dump()).model_dump()
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "repository_group",
        "template_group",
        "snapshot_sha256",
        "log_sha256",
        "case_id",
        "public_alias",
        "too_small",
        "one_split",
        "public_purpose",
    ],
)
def test_grouped_manifest_rejects_leakage_and_false_held_out_claims(mutation):
    data = grouped_manifest()
    if mutation in {"repository_group", "template_group", "snapshot_sha256", "log_sha256", "case_id"}:
        data["cases"][10][mutation] = data["cases"][0][mutation]
    elif mutation == "public_alias":
        data["cases"][10]["snapshot_sha256"] = evaluation.captured_log("pagination").snapshot_sha256
    elif mutation == "too_small":
        data["cases"].pop()
    elif mutation == "one_split":
        for case in data["cases"]:
            case["split"] = "development"
    else:
        data["purpose"] = "public-development"
    with pytest.raises(ValueError):
        DatasetManifest.model_validate(data)
