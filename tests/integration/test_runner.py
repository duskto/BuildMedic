import shutil
import threading
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


@pytest.mark.parametrize("cancel_during_cleanup", [False, True])
def test_terminal_result_waits_for_workspace_cleanup(tmp_path, monkeypatch, cancel_during_cleanup):
    engine = runner.Engine(backend="trusted-local", data_dir=tmp_path / "runner")
    entered, release = threading.Event(), threading.Event()
    original = engine.cleanup_workspace

    def gated(value):
        entered.set()
        assert release.wait(5)
        return original(value)

    monkeypatch.setattr(engine, "cleanup_workspace", gated)
    eid = uid()
    try:
        engine.submit(ExecutionRequest(execution_id=eid, case_id="pagination", check_id="target"))
        assert entered.wait(5)
        # The command has exited, but the public snapshot must still say running.
        assert engine.get(eid)["lifecycle"] == "running"
        assert eid in engine.processes
        if cancel_during_cleanup:
            engine.cancel(eid)
        release.set()
        value = await_done(engine, eid)
        assert value["lifecycle"] == ("cancelled" if cancel_during_cleanup else "completed")
        assert value["termination"]["cleanup_confirmed"] is True
        assert value["termination"]["signal"] is None
        assert not list(engine.root.glob("bm-run-*"))
    finally:
        release.set()
        engine.pool.shutdown(wait=True)


def test_cancel_during_container_creation_never_starts_payload(tmp_path, monkeypatch):
    engine = runner.Engine(backend="docker", image="sha256:" + "1" * 64, data_dir=tmp_path / "runner")
    entered, release = threading.Event(), threading.Event()
    started = []

    def create(container, argv):
        assert argv[:2] == ["docker", "create"] and "--rm" not in argv
        entered.set()
        assert release.wait(5)
        return True

    def forbidden_start(*args, **kwargs):
        started.append(args)
        raise AssertionError("Cancelled payload must not start")

    monkeypatch.setattr(engine, "ready", lambda: (True, "test-only"))
    monkeypatch.setattr(runner.DockerContainer, "create", create)
    monkeypatch.setattr(runner.DockerContainer, "cleanup", lambda _: (True, None))
    monkeypatch.setattr(runner.subprocess, "Popen", forbidden_start)
    eid = uid()
    try:
        engine.submit(ExecutionRequest(execution_id=eid, case_id="pagination", check_id="target"))
        assert entered.wait(5)
        engine.cancel(eid)
        assert engine.get(eid)["lifecycle"] == "running"
        release.set()
        value = await_done(engine, eid)
        assert value["lifecycle"] == "cancelled" and value["reason"] == "user_cancelled"
        assert value["termination"]["cleanup_confirmed"] is True and started == []
    finally:
        release.set()
        engine.pool.shutdown(wait=True)


def test_workspace_cleanup_failure_is_interrupted_and_blocks_more_execution(tmp_path, monkeypatch):
    engine = runner.Engine(backend="trusted-local", data_dir=tmp_path / "runner")
    monkeypatch.setattr(engine, "cleanup_workspace", lambda _: False)
    eid = uid()
    try:
        engine.submit(ExecutionRequest(execution_id=eid, case_id="pagination", check_id="target"))
        value = await_done(engine, eid)
        assert value["lifecycle"] == "interrupted" and value["reason"] == "cleanup_unconfirmed"
        assert value["termination"]["cleanup_error"] == "workspace_cleanup_failed"
        assert not value["passed"] and not engine.ready()[0]
    finally:
        engine.pool.shutdown(wait=True)
        for path in engine.root.glob("bm-run-*"):
            shutil.rmtree(path)


def test_trusted_local_reports_actual_waitpid_signal(tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source"
    shutil.copytree(case_dir("pagination"), source)
    (source / "tests/test_pagination.py").write_text(
        "def test_signal():\n    import os, signal\n    os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    monkeypatch.setattr(runner, "case_dir", lambda _: source)
    engine = runner.Engine(backend="trusted-local", data_dir=tmp_path / "runner")
    eid = uid()
    try:
        engine.submit(ExecutionRequest(execution_id=eid, case_id="pagination", check_id="target"))
        value = await_done(engine, eid)
        assert value["exit_code"] == -15 and value["reason"] == "signal"
        assert value["termination"]["signal"] == 15 and value["termination"]["oom_killed"] is None
        assert value["termination"]["cleanup_confirmed"] is True and not value["passed"]
    finally:
        engine.pool.shutdown(wait=True)


def test_binary_stdout_is_bounded_decoded_and_sanitized(tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source"
    shutil.copytree(case_dir("pagination"), source)
    (source / "tests/test_pagination.py").write_text(
        "def test_bytes():\n    import os\n    os.write(1, b'\\xff\\x00hello\\n')\n"
    )
    (source / "pytest.ini").write_text("[pytest]\naddopts = -s\n")
    monkeypatch.setattr(runner, "case_dir", lambda _: source)
    engine = runner.Engine(backend="trusted-local", data_dir=tmp_path / "runner")
    eid = uid()
    try:
        engine.submit(ExecutionRequest(execution_id=eid, case_id="pagination", check_id="target"))
        value = await_done(engine, eid)
        assert value["passed"] and value["reason"] == "exited"
        assert "\ufffdhello" in value["stdout"] and "\x00" not in value["stdout"]
        assert value["termination"]["cleanup_confirmed"] is True
    finally:
        engine.pool.shutdown(wait=True)
