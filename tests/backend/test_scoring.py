"""Synthetic private bundles exist only under pytest temp dirs; no production assertions are stored here."""

import asyncio
import copy
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from buildmedic import db, evaluation, scoring, store
from buildmedic.api import app
from buildmedic.config import ROOT, settings
from buildmedic.errors import AppError
from buildmedic.scoring_protocol import ScoreReceipt, ScoreRequest, fingerprint, sign_receipt, verify_receipt
from scorer_service.app import Runtime, ServiceSettings, make_app
from scorer_service.bundle import Bundle, load_bundle
from scorer_service.engine import ObservationError, bounded_process, evaluate

IMAGE = "sha256:" + "1" * 64
TOKEN = "synthetic-scoring-test-" + "a" * 40


def configure(monkeypatch):
    for key, value in {
        "scorer_url": "http://127.0.0.1:8002",
        "scorer_token": TOKEN,
        "scorer_suite_sha256": "2" * 64,
        "runner_backend": "docker",
        "runner_image": IMAGE,
    }.items():
        monkeypatch.setattr(settings(), key, value)


def experiment(owner="owner", completed=True):
    log = evaluation.captured_log("pagination")
    spec = evaluation.FourArmSpec(
        schema_version="2.0",
        name="score-tests",
        cases=[
            evaluation.FourArmCase(
                case_id="pagination", snapshot_sha256=log.snapshot_sha256, log_sha256=log.log_sha256
            )
        ],
    )
    eid = evaluation.create(owner, "scoring-test-experiment", spec)
    if completed:
        while True:
            item = evaluation.advance(eid, owner)
            if not item["task_id"]:
                break
            store.cancel(item["task_id"], owner)
    return eid


def receipt(request):
    rows = []
    for row in request["rows"]:
        rows.append(
            {k: row[k] for k in ("index", "task_id", "case_id", "arm", "patch_sha256")}
            | {
                "status": "not_applicable" if row["arm"] == "logs" else "no_candidate",
                "hidden_verified": None,
                "new_regression": None,
                "probes": 0,
                "baseline_failed": 0,
            }
        )
    return sign_receipt(
        {
            k: request[k]
            for k in (
                "schema_version",
                "id",
                "experiment_id",
                "report_sha256",
                "suite_sha256",
                "image_digest",
            )
        }
        | {
            "request_sha256": fingerprint(request),
            "scorer_sha256": "e" * 64,
            "status": "completed",
            "finished_at": time.time(),
            "rows": rows,
            "error_code": None,
        },
        TOKEN,
    )


class Service:
    def __init__(self, variant="valid"):
        self.variant = variant

    async def submit(self, request):
        if self.variant == "unavailable":
            raise httpx.ConnectError("deliberate synthetic outage")
        result = receipt(request)
        if self.variant == "forged":
            result["signature"] = "0" * 64
        elif self.variant == "wrong_report":
            result["report_sha256"] = "f" * 64
        return {
            "id": request["id"],
            "request_sha256": fingerprint(request),
            "status": "completed",
            "receipt": result,
        }

    async def close(self):
        pass


def test_completed_score_is_owner_scoped_immutable_and_never_changes_public_report(monkeypatch):
    configure(monkeypatch)
    eid = experiment()
    before = evaluation.export_report(eid, "owner")
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: scoring.reserve(eid, "owner"), range(8)))
    assert len(set(ids)) == 1
    result = asyncio.run(scoring.get(eid, "owner", Service()))
    assert result["status"] == "completed" and result["summary"][0]["repair_applicable"] is False
    assert all(s["evaluated"] == s["false_positive_denominator"] == 0 for s in result["summary"])
    assert before == evaluation.export_report(eid, "owner")
    monkeypatch.setattr(settings(), "scorer_token", "rotated-token")
    assert asyncio.run(scoring.get(eid, "owner")) == result
    with pytest.raises(KeyError):
        asyncio.run(scoring.get(eid, "foreign"))


@pytest.mark.parametrize("variant", ["forged", "wrong_report"])
def test_unverifiable_receipts_are_rejected_without_grading(monkeypatch, variant):
    configure(monkeypatch)
    eid = experiment()
    scoring.reserve(eid, "owner")
    result = asyncio.run(scoring.get(eid, "owner", Service(variant)))
    assert result["status"] == "failed" and result["error_code"] == "score_receipt_rejected"
    assert result["receipt"] is None and result["summary"] == []


def test_transient_service_failure_preserves_exact_request_and_resumes(monkeypatch):
    configure(monkeypatch)
    eid = experiment()
    sid = scoring.reserve(eid, "owner")
    failed = asyncio.run(scoring.get(eid, "owner", Service("unavailable")))
    assert failed["status"] == "pending" and failed["summary"] == []
    assert scoring.reserve(eid, "owner") == sid
    assert asyncio.run(scoring.get(eid, "owner", Service()))["status"] == "completed"


def test_scoring_requires_complete_frozen_batch_and_private_paths_cannot_be_submitted(client, monkeypatch):
    configure(monkeypatch)
    eid = experiment(completed=False)
    with pytest.raises(store.Conflict):
        scoring.reserve(eid, "owner")
    assert (
        client.post(
            f"/api/v1/experiments/{eid}/score", json={"metrics": 100, "private_path": "/tmp/answers"}
        ).status_code
        == 422
    )
    with TestClient(app) as other:
        assert other.get(f"/api/v1/experiments/{eid}/score").status_code == 404
        assert other.post(f"/api/v1/experiments/{eid}/score").status_code == 404


def bundle():
    return {
        "schema_version": "1.0",
        "suite_id": "synthetic-only",
        "version": 1,
        "dataset": "public-development",
        "development_groups": ["pagination-template"],
        "test_groups": ["unrelated-reserved-template"],
        "cases": [
            {
                "case_id": "pagination",
                "group": "pagination-template",
                "snapshot_sha256": evaluation.captured_log("pagination").snapshot_sha256,
                "probes": [
                    {
                        "entrypoint": "paginate",
                        "args": [["SYNTHETIC_INPUT"], 1, 1],
                        "expected": ["PRIVATE_SYNTHETIC_EXPECTATION"],
                    }
                ],
            }
        ],
    }


def test_private_bundle_must_be_external_and_cannot_relabel_public_cases_as_held_out(tmp_path):
    with pytest.raises(ValueError, match="工作区"):
        load_bundle(ROOT / "evaluation/development.json")
    data = bundle()
    path = tmp_path / "assertions.json"
    path.write_text(json.dumps(data))
    parsed, sha = load_bundle(path)
    assert parsed.suite_id == "synthetic-only" and sha == fingerprint(parsed.model_dump())
    bad = copy.deepcopy(data)
    bad["test_groups"].append("pagination-template")
    with pytest.raises(ValueError, match="重叠"):
        Bundle.model_validate(bad)
    bad = copy.deepcopy(data)
    bad["cases"][0]["group"] = "unrelated-reserved-template"
    with pytest.raises(ValueError, match="公开开发"):
        Bundle.model_validate(bad)
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_bundle(link)


@pytest.mark.parametrize(
    "before,after,verified,regression,fp,reg_den",
    [
        ([False, True], [False, False], False, True, 1, 1),
        ([False, False], [True, True], True, None, 0, 0),
    ],
)
def test_private_grader_detects_false_repair_and_new_regression_without_returning_assertions(
    before, after, verified, regression, fp, reg_den
):
    b = Bundle.model_validate(bundle())
    b.cases[0].probes *= 2
    request = ScoreRequest(
        id="1" * 32,
        experiment_id="2" * 32,
        report_sha256="3" * 64,
        suite_sha256="4" * 64,
        image_digest=IMAGE,
        rows=[
            {
                "index": 0,
                "task_id": "5" * 32,
                "case_id": "pagination",
                "arm": "standard",
                "snapshot_sha256": b.cases[0].snapshot_sha256,
                "public_verified": True,
                "patch": "synthetic-patch",
                "patch_sha256": hashlib.sha256(b"synthetic-patch").hexdigest(),
            }
        ],
    )

    class Observer:
        def observe(self, *args):
            return before if len(args) == 3 else after

    rows = evaluate(request, b, Observer())
    assert rows[0]["hidden_verified"] is verified and rows[0]["new_regression"] is regression
    assert "PRIVATE_SYNTHETIC_EXPECTATION" not in json.dumps(rows)
    summary = scoring.summarize(request.model_dump(), {"rows": rows})[1]
    assert summary["false_positives"] == fp and summary["false_positive_denominator"] == 1
    assert summary["regression_denominator"] == reg_den


def test_signed_receipt_cannot_change_candidate_or_missing_rows(monkeypatch):
    configure(monkeypatch)
    eid = experiment()
    scoring.reserve(eid, "owner")
    with db.transaction() as conn:
        request = ScoreRequest.model_validate(scoring.existing(conn, eid, "owner")["content"]["request"])
    original = receipt(request.model_dump())
    assert verify_receipt(ScoreReceipt.model_validate(original), request, TOKEN)
    modified = copy.deepcopy(original)
    modified["rows"].pop()
    modified.pop("signature")
    with pytest.raises(ValueError):
        verify_receipt(ScoreReceipt.model_validate(sign_receipt(modified, TOKEN)), request, TOKEN)


@pytest.mark.parametrize(
    "url", ["http://remote.example", "https://user:secret@example.org", "https://example.org/?token=secret"]
)
def test_scorer_credentials_cannot_be_sent_to_untrusted_address(monkeypatch, url):
    configure(monkeypatch)
    monkeypatch.setattr(settings(), "scorer_url", url)
    with pytest.raises(AppError):
        scoring.ScorerClient()


def test_private_service_auth_idempotency_and_receipts_do_not_expose_assertions(tmp_path):
    path = tmp_path / "private.json"
    path.write_text(json.dumps(bundle()))
    cfg = ServiceSettings(bundle_path=path, state_dir=tmp_path / "scorer-state", token=TOKEN, image=IMAGE)
    service = make_app(cfg)
    runtime = service.state.runtime
    request = ScoreRequest(
        id="a" * 32,
        experiment_id="b" * 32,
        report_sha256="c" * 64,
        suite_sha256=runtime.suite_hash,
        image_digest=IMAGE,
        rows=[
            {
                "index": 0,
                "task_id": "d" * 32,
                "case_id": "pagination",
                "arm": "standard",
                "snapshot_sha256": evaluation.captured_log("pagination").snapshot_sha256,
                "public_verified": False,
            }
        ],
    )
    with TestClient(service) as client:
        assert client.get("/scorer/v1/health").status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        assert client.post("/scorer/v1/jobs", json=request.model_dump(), headers=headers).status_code == 202
        until = time.monotonic() + 3
        while time.monotonic() < until:
            result = client.get("/scorer/v1/jobs/" + request.id, headers=headers).json()
            if result["receipt"]:
                break
            time.sleep(0.01)
        assert result["status"] == "completed"
        verify_receipt(ScoreReceipt.model_validate(result["receipt"]), request, TOKEN)
        assert "PRIVATE_SYNTHETIC_EXPECTATION" not in json.dumps(result)
        assert TOKEN not in json.dumps(result)
        assert client.post("/scorer/v1/jobs", json=request.model_dump(), headers=headers).json() == result
        changed = request.model_copy(update={"report_sha256": "e" * 64})
        assert client.post("/scorer/v1/jobs", json=changed.model_dump(), headers=headers).status_code == 409
        path.write_text(path.read_text().replace("PRIVATE_SYNTHETIC_EXPECTATION", "changed-expectation"))
        new = request.model_copy(update={"id": "f" * 32})
        assert client.post("/scorer/v1/jobs", json=new.model_dump(), headers=headers).status_code == 409


def test_scorer_restart_marks_incomplete_job_without_reexecuting(tmp_path):
    path = tmp_path / "private.json"
    path.write_text(json.dumps(bundle()))
    cfg = ServiceSettings(bundle_path=path, state_dir=tmp_path / "state", token=TOKEN, image=IMAGE)
    _, suite = load_bundle(path)
    request = ScoreRequest(
        id="a" * 32,
        experiment_id="b" * 32,
        report_sha256="c" * 64,
        suite_sha256=suite,
        image_digest=IMAGE,
        rows=[
            {
                "index": 0,
                "task_id": "d" * 32,
                "case_id": "pagination",
                "arm": "logs",
                "snapshot_sha256": evaluation.captured_log("pagination").snapshot_sha256,
                "public_verified": False,
            }
        ],
    )
    cfg.state_dir.mkdir()
    (cfg.state_dir / (request.id + ".json")).write_text(
        json.dumps(
            {
                "request": request.model_dump(),
                "request_hash": fingerprint(request.model_dump()),
                "status": "running",
            }
        )
    )
    runtime = Runtime(cfg)
    try:
        result = runtime.submit(request)
        assert result["status"] == "failed" and result["receipt"]["error_code"] == "service_restarted"
        verify_receipt(ScoreReceipt.model_validate(result["receipt"]), request, TOKEN)
    finally:
        runtime.pool.shutdown(wait=True)


@pytest.mark.parametrize("code", ['print("x"*70000)', "import time; time.sleep(2)"])
def test_observation_transport_bounds_output_and_deadline(code):
    with pytest.raises(ObservationError):
        bounded_process([sys.executable, "-c", code], b"{}", timeout=0.1)
