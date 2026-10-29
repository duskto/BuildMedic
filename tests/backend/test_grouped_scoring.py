"""Synthetic private fixtures only. No real experiment performance is claimed."""

import asyncio
import json
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from buildmedic import db, evaluation, scoring, store
from buildmedic.config import ROOT, settings
from buildmedic.grouped_evaluation import plan
from buildmedic.scoring_protocol import (
    RECEIPT_ADAPTER,
    REQUEST_ADAPTER,
    fingerprint,
    sign_receipt,
    verify_receipt,
)
from scorer_service.app import Runtime, ServiceSettings, make_app
from scorer_service.bundle import BUNDLE_ADAPTER
from scorer_service.engine import evaluate
from scorer_service.probe import run

TOKEN = "synthetic-grouped-service-" + "s" * 40


def signed(request, bundle, variant=None):
    data = {
        k: getattr(request, k)
        for k in (
            "schema_version",
            "id",
            "experiment_id",
            "report_sha256",
            "suite_sha256",
            "image_digest",
            "dataset_sha256",
            "partition",
        )
    } | {
        "request_sha256": fingerprint(request.model_dump()),
        "scorer_sha256": "b" * 64,
        "status": "completed",
        "finished_at": time.time(),
        "error_code": None,
        "rows": evaluate(request, bundle, None),
    }
    if variant == "dataset":
        data["dataset_sha256"] = "0" * 64
    elif variant == "partition":
        data["partition"] = "development" if request.partition == "test" else "test"
    elif variant == "missing":
        data["rows"].pop()
    elif variant == "repeat":
        data["rows"][0]["repeat"] = 2
    elif variant == "order":
        data["rows"].reverse()
    result = sign_receipt(data, TOKEN)
    if variant == "signature":
        result["signature"] = "0" * 64
    return result


@pytest.mark.parametrize("variant", ["subset", "reorder", "source", "dataset", "partition", "duplicate"])
def test_grouped_score_request_requires_full_predeclared_partition(grouped_score_request, variant):
    data = grouped_score_request(repeats=3).model_dump()
    if variant == "subset":
        data["rows"].pop()
    elif variant == "reorder":
        data["rows"].reverse()
    elif variant == "source":
        data["rows"][0]["snapshot_sha256"] = "0" * 64
    elif variant == "dataset":
        data["dataset_sha256"] = "0" * 64
    elif variant == "partition":
        data["partition"] = "development"
    else:
        data["rows"][0]["task_id"] = data["rows"][1]["task_id"]
    with pytest.raises(ValueError):
        REQUEST_ADAPTER.validate_python(data)


@pytest.mark.parametrize("variant", ["signature", "dataset", "partition", "missing", "repeat", "order"])
def test_grouped_receipt_cannot_rebind_signed_results(grouped_score_request, grouped_scoring_bundle, variant):
    request = grouped_score_request()
    body = signed(request, grouped_scoring_bundle, variant)
    with pytest.raises(ValueError):
        verify_receipt(RECEIPT_ADAPTER.validate_python(body), request, TOKEN)


@pytest.mark.parametrize("variant", ["missing", "source", "group", "entrypoint", "dataset"])
def test_private_grouped_bundle_requires_complete_bound_cases(grouped_scoring_bundle, variant):
    data = grouped_scoring_bundle.model_dump()
    if variant == "missing":
        data["cases"].pop()
    elif variant == "source":
        data["cases"][0]["snapshot_sha256"] = "0" * 64
    elif variant == "group":
        data["cases"][0]["group"] = "unrelated-template"
    elif variant == "entrypoint":
        data["cases"][0]["probes"][0]["entrypoint"] = "arbitrary_python"
    else:
        data["dataset_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        BUNDLE_ADAPTER.validate_python(data)


def test_grouped_scorer_http_pins_full_catalog_and_survives_restart(
    tmp_path, grouped_score_request, grouped_scoring_bundle
):
    path = tmp_path / "private.json"
    path.write_text(json.dumps(grouped_scoring_bundle.model_dump()))
    cfg = ServiceSettings(
        bundle_path=path, state_dir=tmp_path / "state", token=TOKEN, image="sha256:" + "1" * 64
    )
    app = make_app(cfg)
    request = grouped_score_request(repeats=3)
    headers = {"Authorization": "Bearer " + TOKEN}
    with TestClient(app) as client:
        assert client.post("/scorer/v1/jobs", json=request.model_dump()).status_code == 401
        health = client.get("/scorer/v1/health", headers=headers).json()
        assert health["dataset"] == "public-constructed-grouped" and health["protocol_version"] == "2.0"
        response = client.post("/scorer/v1/jobs", json=request.model_dump(), headers=headers)
        assert response.status_code == 202, response.text
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = client.get("/scorer/v1/jobs/" + request.id, headers=headers).json()
            if result["receipt"]:
                break
            time.sleep(0.01)
        assert len(result["receipt"]["rows"]) == 120
        verify_receipt(RECEIPT_ADAPTER.validate_python(result["receipt"]), request, TOKEN)
        assert '"expected"' not in json.dumps(result) and TOKEN not in json.dumps(result)
        changed = request.model_dump()
        changed["id"] = store.uid()
        changed["dataset_manifest"]["cases"][-1]["repository_group"] = "renamed-project"
        changed["dataset_sha256"] = fingerprint(changed["dataset_manifest"])
        assert client.post("/scorer/v1/jobs", json=changed, headers=headers).status_code == 409
    runtime = Runtime(cfg)
    try:
        assert runtime.submit(request) == result
        assert runtime.jobs[request.id]["status"] == "completed"
    finally:
        runtime.pool.shutdown(wait=True)


def test_probe_snapshots_mutable_results_between_calls(monkeypatch):
    bucket = []

    class Candidate:
        def append_value(self, value):
            bucket.append(value)
            return bucket

    monkeypatch.setattr("scorer_service.probe.importlib.import_module", lambda _: Candidate())
    answer = run(
        {
            "case_id": "mutable-default",
            "calls": [
                {"entrypoint": "append_value", "args": [1], "kwargs": {}},
                {"entrypoint": "append_value", "args": [2], "kwargs": {}},
            ],
        }
    )
    assert answer[0]["value"] == [1] and answer[1]["value"] == [1, 2]


@pytest.mark.parametrize("value", [float("nan"), object()])
def test_unserializable_result_cannot_impersonate_an_expected_exception(monkeypatch, value):
    from types import SimpleNamespace

    candidate = SimpleNamespace(clamp=lambda *args, **kwargs: value)
    monkeypatch.setattr("scorer_service.probe.importlib.import_module", lambda _: candidate)
    answer = run({"case_id": "clamp", "calls": [{"entrypoint": "clamp", "args": [], "kwargs": {}}]})
    assert answer == [{"ok": False, "exception": "ObservationError"}]


def completed(monkeypatch, bundle, owner="owner"):
    cfg = settings()
    for key, value in {
        "runner_backend": "docker",
        "runner_image": evaluation.captured_log("pagination").image_digest,
        "scorer_url": "http://127.0.0.1:8002",
        "scorer_token": TOKEN,
        "scorer_suite_sha256": fingerprint(bundle.model_dump()),
        "scorer_dataset": "public-constructed-grouped",
    }.items():
        monkeypatch.setattr(cfg, key, value)
    eid = evaluation.create(owner, "grouped-score", plan("test"))
    while True:
        tid = evaluation.advance(eid, owner)["task_id"]
        if not tid:
            break
        store.cancel(tid, owner)
    return eid


class Client:
    def __init__(self, bundle):
        self.bundle = bundle

    async def submit(self, body):
        request = REQUEST_ADAPTER.validate_python(body)
        return {
            "id": request.id,
            "request_sha256": fingerprint(body),
            "receipt": signed(request, self.bundle),
        }

    async def close(self):
        pass


def test_completed_grouped_experiment_scores_without_rewriting_public_report(
    monkeypatch, grouped_scoring_bundle
):
    eid = completed(monkeypatch, grouped_scoring_bundle)
    before = evaluation.export_report(eid, "owner")
    scoring.reserve(eid, "owner")
    result = asyncio.run(scoring.get(eid, "owner", Client(grouped_scoring_bundle)))
    assert result["status"] == "completed" and len(result["receipt"]["rows"]) == 40
    assert result["receipt"]["partition"] == "test" and "公开构造" in result["scope"]
    assert all(row["planned"] == 10 for row in result["summary"])
    assert sum(row["no_candidate"] for row in result["summary"]) == 30
    assert before == evaluation.export_report(eid, "owner")
    with pytest.raises(KeyError):
        asyncio.run(scoring.get(eid, "foreign"))
    monkeypatch.setattr(settings(), "scorer_dataset", "public-development")
    assert asyncio.run(scoring.get(eid, "owner")) == result


def test_wrong_service_dataset_is_rejected_before_reservation(monkeypatch, grouped_scoring_bundle):
    eid = completed(monkeypatch, grouped_scoring_bundle)
    monkeypatch.setattr(settings(), "scorer_dataset", "public-development")
    with pytest.raises(scoring.AppError) as exc:
        scoring.reserve(eid, "owner")
    assert exc.value.code == "scorer_dataset_mismatch"
    with db.engine.connect() as conn:
        assert not conn.execute(select(db.experiment_scores)).first()


def test_transport_accepts_bounded_large_receipt_without_exposing_credentials(monkeypatch):
    monkeypatch.setattr(settings(), "scorer_url", "http://127.0.0.1:8002")
    monkeypatch.setattr(settings(), "scorer_token", TOKEN)

    def handler(request):
        assert request.headers["authorization"] == "Bearer " + TOKEN
        return httpx.Response(202, json={"receipt": {"rows": ["synthetic-result"] * 9000}})

    async def check():
        client = scoring.ScorerClient(transport=httpx.MockTransport(handler))
        try:
            result = await client.submit({})
            assert len(result["receipt"]["rows"]) == 9000
        finally:
            await client.close()

    asyncio.run(check())


def test_operator_bundle_check_does_not_print_private_inputs_on_validation_error(
    tmp_path, grouped_scoring_bundle
):
    path = tmp_path / "private.json"
    data = grouped_scoring_bundle.model_dump()
    path.write_text(json.dumps(data))
    argv = [sys.executable, str(ROOT / "scripts/check_scoring_bundle.py"), str(path)]
    good = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=10, check=False)
    assert good.returncode == 0 and good.stdout.strip() == "BUILDMEDIC_SCORER_SUITE_SHA256=" + fingerprint(
        data
    )
    data["cases"][0]["probes"][0]["entrypoint"] = "PRIVATE_INPUT_MUST_NOT_PRINT"
    data["cases"][0]["probes"][0]["expected"] = "PRIVATE_EXPECTATION_MUST_NOT_PRINT"
    path.write_text(json.dumps(data))
    bad = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=10, check=False)
    assert bad.returncode == 2 and "PRIVATE_" not in bad.stderr + bad.stdout
    assert "Traceback" not in bad.stderr
