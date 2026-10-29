"""Public QA repairs, never model-generated candidates or hidden grading labels."""

import json
import os
import time

import pytest

from buildmedic.benchmark_cases import SYNTHETIC_CASES
from buildmedic.cases import case_dir
from buildmedic.config import ROOT
from buildmedic.evaluation import captured_log
from buildmedic.orchestration import verification_ok
from buildmedic.patching import patch_from_edits
from buildmedic.runner import Engine
from buildmedic.schemas import ExecutionRequest, SourceEdit
from buildmedic.store import uid

IMAGE = os.environ.get("BUILDMEDIC_TEST_DOCKER_IMAGE", "")


def execute(engine, cid, check, patch=""):
    eid = uid()
    engine.submit(ExecutionRequest(execution_id=eid, case_id=cid, check_id=check, patch=patch))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = engine.get(eid)
        if result["lifecycle"] not in {"queued", "running"}:
            assert result["termination"]["cleanup_confirmed"] is True
            return result
        time.sleep(0.02)
    engine.cancel(eid)
    pytest.fail("constructed case check timed out")


@pytest.mark.parametrize("cid", list(SYNTHETIC_CASES))
@pytest.mark.parametrize(
    "backend",
    [
        "trusted-local",
        pytest.param(
            "docker",
            marks=pytest.mark.skipif(
                not IMAGE,
                reason="Set BUILDMEDIC_TEST_DOCKER_IMAGE to run constructed cases in Docker",
            ),
        ),
    ],
)
def test_registered_constructed_failure_and_independent_regression(cid, backend, tmp_path):
    engine = Engine(backend=backend, image=IMAGE if backend == "docker" else "", data_dir=tmp_path / "runner")
    repairs = json.loads((ROOT / "tests/support/grouped_repairs.json").read_text())
    repair = repairs[cid]
    try:
        original = execute(engine, cid, "target")
        baseline = execute(engine, cid, "regression")
        assert not original["passed"] and original["counts"]["failed"] > 0
        assert (case_dir(cid) / repair["path"]).read_text() == repair["before"]
        patch = patch_from_edits(case_dir(cid), [SourceEdit(**repair)])
        target = execute(engine, cid, "target", patch)
        regression = execute(engine, cid, "regression", patch)
        assert target["passed"] and regression["passed"]
        assert verification_ok([original, baseline], target, regression)[0]
        assert len({r["id"] for r in (original, baseline, target, regression)}) == 4
        assert original["snapshot_sha256"] == captured_log(cid).snapshot_sha256
        assert all(r["tests_sha256"] == original["tests_sha256"] for r in (baseline, target, regression))
    finally:
        engine.pool.shutdown(wait=True)
