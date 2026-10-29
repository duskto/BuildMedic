"""Synthetic vectors and explicit QA patches; actual Docker, never a model performance score."""

import hashlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from buildmedic import scoring
from buildmedic.cases import EVALUATION_CASE_IDS, case_dir
from buildmedic.config import ROOT
from buildmedic.patching import patch_from_edits
from buildmedic.runner import Engine
from buildmedic.schemas import ExecutionRequest, SourceEdit
from buildmedic.scoring_protocol import RECEIPT_ADAPTER, verify_receipt
from buildmedic.store import uid
from scorer_service import engine as engine_module
from scorer_service.app import ServiceSettings, make_app
from scorer_service.engine import DockerObserver, evaluate

IMAGE = os.environ.get("BUILDMEDIC_TEST_DOCKER_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Explicit Docker image required for grouped private scoring"
)


def repair(cid):
    data = json.loads((ROOT / "tests/support/grouped_repairs.json").read_text())
    if cid in data:
        return SourceEdit(**data[cid])
    path, before, after = {
        "pagination": ("src/demo/pagination.py", "start + page_size - 1", "start + page_size"),
        "imports": ("src/demo/invoice.py", "from demo.utils import", "from demo.formatting import"),
        "signature": (
            "src/demo/shipping.py",
            "return shipping_cost(weight)",
            "return shipping_cost(weight, region=region)",
        ),
    }[cid]
    source = (case_dir(cid) / path).read_text()
    return SourceEdit(path=path, before=source, after=source.replace(before, after))


@pytest.mark.parametrize("cid", EVALUATION_CASE_IDS)
def test_all_twenty_behavior_gateways_reproduce_and_accept_fixed_sources(
    tmp_path, monkeypatch, grouped_scoring_bundle, cid
):
    private = next(c for c in grouped_scoring_bundle.cases if c.case_id == cid)
    original_transport = engine_module.bounded_process
    seen = []

    def transport(argv, payload):
        data = json.loads(payload)
        assert all(set(call) == {"entrypoint", "args", "kwargs"} for call in data["calls"])
        mounts = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-v"]
        assert len(mounts) == 2 and all("private" not in mount and "/tests" not in mount for mount in mounts)
        seen.append(data)
        return original_transport(argv, payload)

    monkeypatch.setattr(engine_module, "bounded_process", transport)
    observer = DockerObserver(IMAGE, tmp_path / "observations")
    before = observer.observe(cid, private.snapshot_sha256, private.probes)
    patch = patch_from_edits(case_dir(cid), [repair(cid)])
    after = observer.observe(cid, private.snapshot_sha256, private.probes, patch)
    assert not all(before), cid
    assert all(after), cid
    assert len(seen) == 2
    if cid == "mutable-default":
        assert before == [True, False, True]
    if cid == "rollback":
        assert before == [False, True]


def test_public_green_grouped_overfit_fails_independent_behavior_checks(
    tmp_path,
    grouped_score_request,
    grouped_scoring_bundle,
):
    cid = "weighted-mean"
    edit = repair(cid)
    edit.after = edit.after.replace(
        "    return sum(", "    if values in ([3, 9], [3]):\n        return -99\n    return sum("
    )
    patch = patch_from_edits(case_dir(cid), [edit])
    public = Engine(backend="docker", image=IMAGE, data_dir=tmp_path / "public-runner")
    try:
        eid = uid()
        public.submit(ExecutionRequest(execution_id=eid, case_id=cid, check_id="regression", patch=patch))
        until = time.monotonic() + 60
        while public.get(eid)["lifecycle"] in {"queued", "running"} and time.monotonic() < until:
            time.sleep(0.05)
        assert public.get(eid)["passed"] is True
    finally:
        public.pool.shutdown(wait=True)
    request = grouped_score_request(partition="development", patches={cid: patch})
    observer = DockerObserver(IMAGE, tmp_path / "independent-observer")
    rows = evaluate(request, grouped_scoring_bundle, observer)
    scored = [row for row in rows if row["patch_sha256"] == hashlib.sha256(patch.encode()).hexdigest()]
    assert len(scored) == 3
    assert all(row["hidden_verified"] is False and row["new_regression"] is True for row in scored)
    assert '"expected"' not in json.dumps(rows)


def test_grouped_http_scorer_signs_both_complete_partition_results(
    tmp_path,
    grouped_score_request,
    grouped_scoring_bundle,
):
    path = tmp_path / "synthetic-private.json"
    path.write_text(json.dumps(grouped_scoring_bundle.model_dump()))
    token = "synthetic-grouped-http-" + "h" * 40
    app = make_app(ServiceSettings(bundle_path=path, state_dir=tmp_path / "state", token=token, image=IMAGE))
    patches = {cid: patch_from_edits(case_dir(cid), [repair(cid)]) for cid in EVALUATION_CASE_IDS}
    requests = [
        grouped_score_request(partition=split, patches=patches).model_copy(update={"image_digest": IMAGE})
        for split in ("development", "test")
    ]
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + token}
        for request in requests:
            response = client.post("/scorer/v1/jobs", json=request.model_dump(), headers=headers)
            assert response.status_code == 202, response.text
        for request in requests:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                result = client.get("/scorer/v1/jobs/" + request.id, headers=headers).json()
                if result["receipt"]:
                    break
                time.sleep(0.1)
            receipt = verify_receipt(RECEIPT_ADAPTER.validate_python(result["receipt"]), request, token)
            assert receipt["status"] == "completed" and len(receipt["rows"]) == 40
            summary = scoring.summarize(request.model_dump(), receipt)
            assert all(s["planned"] == 10 for s in summary)
            assert all(
                s["behavior_verified"] == s["evaluated"] == 10 for s in summary if s["repair_applicable"]
            )
            assert all(s["false_positives"] == s["new_regressions"] == 0 for s in summary)
            assert '"expected"' not in json.dumps(receipt)


def test_nonfinite_candidate_output_is_an_evaluation_error_not_an_expected_exception(
    tmp_path,
    grouped_score_request,
    grouped_scoring_bundle,
):
    cid = "zip-length"
    edit = repair(cid)
    edit.after = 'def pair_records(keys, values):\n    return float("nan")\n'
    patch = patch_from_edits(case_dir(cid), [edit])
    request = grouped_score_request(patches={cid: patch})
    rows = evaluate(request, grouped_scoring_bundle, DockerObserver(IMAGE, tmp_path / "observer"))
    judged = [row for row in rows if row["patch_sha256"] == hashlib.sha256(patch.encode()).hexdigest()]
    assert len(judged) == 3
    assert all(row["status"] == "evaluation_error" and row["hidden_verified"] is None for row in judged)
