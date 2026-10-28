"""Opt-in container checks use fixed test edits, never a live model."""

import os
import time

import pytest

from buildmedic.cases import case_dir
from buildmedic.patching import patch_from_edits
from buildmedic.runner import Engine
from buildmedic.schemas import ExecutionRequest, SourceEdit
from buildmedic.store import uid

IMAGE = os.environ.get("BUILDMEDIC_TEST_DOCKER_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Set BUILDMEDIC_TEST_DOCKER_IMAGE to opt in to real Docker checks"
)


def execute(engine, case_id, check, patch=""):
    execution = uid()
    engine.submit(ExecutionRequest(execution_id=execution, case_id=case_id, check_id=check, patch=patch))
    until = time.monotonic() + 60
    while time.monotonic() < until:
        result = engine.get(execution)
        if result["lifecycle"] not in {"queued", "running"}:
            return result
        time.sleep(0.1)
    engine.cancel(execution)
    pytest.fail("Docker check timed out")


@pytest.mark.parametrize(
    "case_id,path,before,after",
    [
        ("pagination", "src/demo/pagination.py", "start + page_size - 1", "start + page_size"),
        ("imports", "src/demo/invoice.py", "from demo.utils import", "from demo.formatting import"),
        (
            "signature",
            "src/demo/shipping.py",
            "return shipping_cost(weight)",
            "return shipping_cost(weight, region=region)",
        ),
    ],
)
def test_original_failure_and_clean_patched_regression(case_id, path, before, after, tmp_path):
    engine = Engine(backend="docker", image=IMAGE, data_dir=tmp_path)
    try:
        original = execute(engine, case_id, "target")
        assert original["reason"] == "exited" and original["exit_code"] in {1, 2}
        original_source = (case_dir(case_id) / path).read_text()
        patch = patch_from_edits(
            case_dir(case_id),
            [SourceEdit(path=path, before=original_source, after=original_source.replace(before, after))],
        )
        fixed = execute(engine, case_id, "regression", patch)
        assert fixed["passed"] is True, fixed
        assert fixed["counts"]["collected"] > 0
        assert fixed["image_digest"] == IMAGE
        assert fixed["environment_fingerprint"] == original["environment_fingerprint"]
        assert fixed["snapshot_sha256"] == original["snapshot_sha256"]
        assert fixed["tests_sha256"] == original["tests_sha256"]
        assert fixed["isolation"] == "docker"
    finally:
        engine.pool.shutdown(wait=True)
