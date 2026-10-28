import shutil
import time

import pytest

from buildmedic import runner
from buildmedic.cases import case_dir
from buildmedic.schemas import ExecutionRequest
from buildmedic.store import uid


def await_done(engine, eid):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = engine.get(eid)
        if value["lifecycle"] not in {"queued", "running"}:
            return value
        time.sleep(0.03)
    pytest.fail("Runner failed to reach a terminal state")


def test_running_pytest_cancellation_and_idempotency(tmp_path, monkeypatch):
    source = tmp_path / "source"
    shutil.copytree(case_dir("pagination"), source)
    (source / "tests/test_pagination.py").write_text("import time\ndef test_slow():\n    time.sleep(60)\n")
    monkeypatch.setattr(runner, "case_dir", lambda _: source)
    engine = runner.Engine(backend="trusted-local", data_dir=tmp_path / "runner")
    req = ExecutionRequest(execution_id=uid(), case_id="pagination", check_id="target")
    try:
        engine.submit(req)
        engine.submit(req)
        assert len(engine.jobs) == 1
        with pytest.raises(ValueError):
            engine.submit(req.model_copy(update={"check_id": "regression"}))
        deadline = time.monotonic() + 5
        while req.execution_id not in engine.processes and time.monotonic() < deadline:
            time.sleep(0.02)
        assert req.execution_id in engine.processes
        engine.cancel(req.execution_id)
        result = await_done(engine, req.execution_id)
        assert result["lifecycle"] == "cancelled"
        assert result["reason"] == "user_cancelled"
        assert result["passed"] is False
        assert req.execution_id not in engine.processes
        assert not list((tmp_path / "runner").glob("bm-run-*"))
    finally:
        engine.pool.shutdown(wait=True)


def test_unconfigured_docker_blocks_without_execution(tmp_path):
    engine = runner.Engine(backend="docker", image="", data_dir=tmp_path)
    try:
        req = ExecutionRequest(execution_id=uid(), case_id="pagination", check_id="target")
        engine.submit(req)
        result = await_done(engine, req.execution_id)
        assert result["reason"] == "environment_blocked"
        assert result["exit_code"] is None
    finally:
        engine.pool.shutdown(wait=True)


def test_junit_rejects_entities_and_empty_collection(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text('<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]><testsuites/>')
    assert runner.parse_junit(report) is None
    report.write_text('<testsuites><testsuite tests="0"/></testsuites>')
    assert runner.parse_junit(report)["collected"] == 0
