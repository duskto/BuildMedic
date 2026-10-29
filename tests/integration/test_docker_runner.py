"""Opt-in container checks use fixed test edits, never a live model."""

import os
import shutil
import subprocess
import time

import pytest

from buildmedic import runner as runner_module
from buildmedic.cases import case_dir
from buildmedic.patching import patch_from_edits
from buildmedic.runner import Engine
from buildmedic.schemas import ExecutionRequest, SourceEdit
from buildmedic.store import uid

IMAGE = os.environ.get("BUILDMEDIC_TEST_DOCKER_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="Set BUILDMEDIC_TEST_DOCKER_IMAGE to opt in to real Docker checks"
)


def execute(engine, case_id, check, patch="", *, external_kill=False):
    execution = uid()
    engine.submit(ExecutionRequest(execution_id=execution, case_id=case_id, check_id=check, patch=patch))
    until = time.monotonic() + 60
    killed = False
    while time.monotonic() < until:
        result = engine.get(execution)
        if result["lifecycle"] not in {"queued", "running"}:
            return result
        if external_kill and not killed:
            container = engine.containers.get(execution)
            if container:
                ownership, state = container.inspect()
                if ownership == "owned" and state["Running"]:
                    # Simulate an external daemon-side signal, without a Runner cancel intent.
                    killed_process = subprocess.run(
                        ["docker", "kill", "--signal", "KILL", container.container_id],
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    assert killed_process.returncode == 0
                    killed = True
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
        assert fixed["termination"]["oom_killed"] is False
        assert fixed["termination"]["state_confirmed"] is True
        assert fixed["termination"]["cleanup_confirmed"] is True
        assert fixed["resource_limits"]["memory_swap"] == fixed["resource_limits"]["memory"]
        assert_removed(engine, fixed)
    finally:
        engine.pool.shutdown(wait=True)


def assert_removed(engine, result):
    listed = subprocess.run(
        ["docker", "container", "ls", "-aq", "--filter", f"name=^/bm-{result['id']}$"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert listed.returncode == 0 and not listed.stdout.strip()
    assert result["id"] not in engine.processes
    assert not list(engine.root.glob("bm-run-*"))


@pytest.mark.parametrize(
    "kind,code,expected",
    [
        (
            "oom",
            "chunks = []\n    while True:\n        chunks.append(bytearray(8 * 1024 * 1024))",
            "oom_killed",
        ),
        ("explicit_137", "import os\n    os._exit(137)", "abnormal_exit"),
        ("signal", "import time\n    time.sleep(60)", "abnormal_exit"),
        ("timeout", "import time\n    time.sleep(60)", "timeout"),
        ("output", "import os\n    os.write(1, b'x' * 1000000)", "output_limit"),
    ],
)
def test_real_container_resource_and_exit_causes(kind, code, expected, tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source"
    shutil.copytree(case_dir("pagination"), source)
    (source / "tests/test_pagination.py").write_text("def test_fault():\n    " + code + "\n")
    monkeypatch.setattr(runner_module, "case_dir", lambda _: source)
    # Server-owned test settings: neither requests nor model patches can set these limits.
    monkeypatch.setattr(runner_module, "DOCKER_MEMORY", "128m")
    if kind == "timeout":
        monkeypatch.setattr(runner_module, "MAX_EXECUTION_SECONDS", 2)
    if kind == "output":
        monkeypatch.setattr(runner_module, "MAX_OUTPUT_BYTES", 100000)
        # pytest capture is disabled by this fixture's configuration, not a candidate patch.
        (source / "pytest.ini").write_text("[pytest]\naddopts = -s\n")
    engine = Engine(backend="docker", image=IMAGE, data_dir=tmp_path / "runner")
    try:
        value = execute(engine, "pagination", "target", external_kill=kind == "signal")
        assert value["reason"] == expected, value
        assert value["lifecycle"] == "completed" and not value["passed"]
        facts = value["termination"]
        assert facts["cleanup_confirmed"] is True and facts["cause"] == expected
        assert facts["oom_killed"] is (kind == "oom")
        if kind == "explicit_137":
            assert facts["signal"] is None and facts["possible_signal"] == 9
        if kind == "signal":
            assert facts["signal"] is None and facts["possible_signal"] == 9
        assert_removed(engine, value)
        # Facts remain available after removal and a Runner restart.
        restarted = Engine(backend="docker", image=IMAGE, data_dir=engine.root)
        try:
            assert restarted.get(value["id"])["termination"] == facts
        finally:
            restarted.pool.shutdown(wait=True)
    finally:
        engine.pool.shutdown(wait=True)


def test_real_container_cancel_confirms_removal_before_terminal_result(tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source"
    shutil.copytree(case_dir("pagination"), source)
    (source / "tests/test_pagination.py").write_text("import time\ndef test_wait():\n    time.sleep(60)\n")
    monkeypatch.setattr(runner_module, "case_dir", lambda _: source)
    engine = Engine(backend="docker", image=IMAGE, data_dir=tmp_path / "runner")
    eid = uid()
    try:
        engine.submit(ExecutionRequest(execution_id=eid, case_id="pagination", check_id="target"))
        until = time.monotonic() + 10
        while eid not in engine.processes and time.monotonic() < until:
            time.sleep(0.02)
        assert eid in engine.processes
        engine.cancel(eid)
        until = time.monotonic() + 15
        while time.monotonic() < until:
            value = engine.get(eid)
            if value["lifecycle"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert value["lifecycle"] == "cancelled" and value["reason"] == "user_cancelled", value
        assert value["termination"]["cleanup_confirmed"] is True and not value["passed"]
        assert_removed(engine, value)
    finally:
        engine.cancel(eid)
        engine.pool.shutdown(wait=True)
