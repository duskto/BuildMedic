"""Explicit synthetic probes + actual Docker. Production private bundles are external."""

import hashlib
import os
import time

import pytest

from buildmedic import scoring
from buildmedic.cases import case_dir, metadata
from buildmedic.patching import patch_from_edits
from buildmedic.runner import Engine
from buildmedic.schemas import ExecutionRequest, SourceEdit
from buildmedic.scoring_protocol import ScoreRequest
from buildmedic.store import uid
from scorer_service import engine as engine_module
from scorer_service.bundle import Bundle
from scorer_service.engine import DockerObserver, evaluate

IMAGE = os.environ.get("BUILDMEDIC_TEST_DOCKER_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Explicit Docker image required for independent behavior checks"
)


def test_public_green_overfit_is_rejected_and_expected_values_never_enter_container(tmp_path, monkeypatch):
    snapshot = metadata("pagination")["snapshot_sha256"]
    original = (case_dir("pagination") / "src/demo/pagination.py").read_text()
    overfit = original.replace(
        "end = start + page_size - 1",
        'end = start + page_size\n    if items != list(range(7)):\n        return ["synthetic-regression"]',
    )
    patch = patch_from_edits(
        case_dir("pagination"), [SourceEdit(path="src/demo/pagination.py", before=original, after=overfit)]
    )
    public = Engine(backend="docker", image=IMAGE, data_dir=tmp_path / "public-runner")
    try:
        eid = uid()
        public.submit(
            ExecutionRequest(execution_id=eid, case_id="pagination", check_id="regression", patch=patch)
        )
        until = time.monotonic() + 60
        while public.get(eid)["lifecycle"] in {"queued", "running"} and time.monotonic() < until:
            time.sleep(0.1)
        assert public.get(eid)["passed"] is True
    finally:
        public.pool.shutdown(wait=True)
    private = Bundle.model_validate(
        {
            "schema_version": "1.0",
            "suite_id": "synthetic-container-check",
            "version": 1,
            "dataset": "public-development",
            "development_groups": ["paging"],
            "cases": [
                {
                    "case_id": "pagination",
                    "group": "paging",
                    "snapshot_sha256": snapshot,
                    "probes": [
                        {
                            "entrypoint": "paginate",
                            "args": [["a", "b", "c", "d"], 1, 2],
                            "expected": ["a", "b"],
                        },
                        {"entrypoint": "paginate", "args": [["a", "b", "c", "d"], 99, 2], "expected": []},
                    ],
                }
            ],
        }
    )
    seen = []
    actual = engine_module.bounded_process

    def capture(argv, payload):
        import json

        data = json.loads(payload)
        assert all(set(call) == {"entrypoint", "args", "kwargs"} for call in data["calls"])
        mounts = [argv[i + 1] for i, v in enumerate(argv[:-1]) if v == "-v"]
        assert len(mounts) == 2 and all("private.json" not in m and "tests" not in m for m in mounts)
        assert "--network" in argv and "none" in argv and "--read-only" in argv
        seen.append(data)
        return actual(argv, payload)

    monkeypatch.setattr(engine_module, "bounded_process", capture)
    observer = DockerObserver(IMAGE, tmp_path / "private-observer")
    probes = private.cases[0].probes
    assert observer.observe("pagination", snapshot, probes) == [False, True]
    fixed = patch_from_edits(
        case_dir("pagination"),
        [
            SourceEdit(
                path="src/demo/pagination.py",
                before=original,
                after=original.replace("start + page_size - 1", "start + page_size"),
            )
        ],
    )
    assert observer.observe("pagination", snapshot, probes, fixed) == [True, True]
    request = ScoreRequest(
        id=uid(),
        experiment_id=uid(),
        report_sha256="a" * 64,
        suite_sha256="b" * 64,
        image_digest=IMAGE,
        rows=[
            {
                "index": 0,
                "task_id": uid(),
                "case_id": "pagination",
                "arm": "standard",
                "snapshot_sha256": snapshot,
                "public_verified": True,
                "patch": patch,
                "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            }
        ],
    )
    rows = evaluate(request, private, observer)
    assert (
        rows[0]["status"] == "failed"
        and rows[0]["hidden_verified"] is False
        and rows[0]["new_regression"] is True
    )
    summary = scoring.summarize(request.model_dump(), {"rows": rows})[1]
    assert summary["false_positives"] == summary["new_regressions"] == 1
    assert len(seen) == 4
