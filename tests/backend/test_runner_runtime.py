import json
import subprocess

import pytest

from buildmedic.runner_runtime import DockerContainer, ExecutionTermination, classify_exit


def state(**changes):
    return {"OOMKilled": False, "ExitCode": 1, "Running": False, "Status": "exited", "Error": "", **changes}


@pytest.mark.parametrize(
    "backend,code,facts,requested,expected,signal,possible",
    [
        ("trusted-local", 1, None, None, "exited", None, None),
        ("trusted-local", -15, None, None, "signal", 15, None),
        ("docker", 137, state(ExitCode=137, OOMKilled=True), None, "oom_killed", 9, None),
        ("docker", 0, state(ExitCode=0, OOMKilled=True), None, "oom_killed", None, None),
        ("docker", 137, state(ExitCode=137), None, "abnormal_exit", None, 9),
        ("docker", 143, state(ExitCode=143), None, "abnormal_exit", None, 15),
        ("docker", 0, state(ExitCode=0), None, "exited", None, None),
        ("docker", -9, state(ExitCode=137), "user_cancelled", "user_cancelled", 9, None),
        ("docker", -9, state(ExitCode=137, OOMKilled=True), "timeout", "timeout", 9, None),
        ("trusted-local", 0, None, "output_limit", "output_limit", None, None),
        ("docker", 0, None, None, "container_state_unavailable", None, None),
        ("docker", 125, state(ExitCode=0, Error="private daemon detail"), None, "runtime_error", None, None),
        ("docker", 125, state(ExitCode=0), None, "runtime_error", None, None),
        ("docker", 0, state(ExitCode=0, Running=True, Status="running"), None, "runtime_error", None, None),
    ],
)
def test_exit_facts_do_not_infer_oom_from_exit_137(
    backend, code, facts, requested, expected, signal, possible
):
    termination = ExecutionTermination()
    assert classify_exit(backend, code, termination, facts, requested) == expected
    assert termination.signal == signal and termination.possible_signal == possible
    assert "private daemon detail" not in termination.model_dump_json()


def result(argv, code=0, stdout=""):
    return subprocess.CompletedProcess(argv, code, stdout, "private daemon diagnostics")


def test_foreign_container_is_never_killed_or_removed(tmp_path, monkeypatch):
    container = DockerContainer(tmp_path, "a" * 32, "b" * 64)
    calls = []

    def run(argv):
        calls.append(argv)
        return result(
            argv, stdout=json.dumps({"labels": {"io.buildmedic.runner": "another-runner"}, "state": state()})
        )

    monkeypatch.setattr(container, "run", run)
    container.kill()
    assert container.cleanup() == (False, "container_identity_mismatch")
    assert all("inspect" in argv for argv in calls)


def test_cleanup_requires_daemon_acknowledgement_and_rechecks_missing_containers(tmp_path, monkeypatch):
    container = DockerContainer(tmp_path, "a" * 32, "b" * 64)
    calls = []

    def run(argv):
        calls.append(argv)
        return result(argv, code=1) if "inspect" in argv else result(argv)

    monkeypatch.setattr(container, "run", run)
    assert container.cleanup() == (True, None)
    assert len(calls) == 2 and "ls" in calls[1]
    monkeypatch.setattr(container, "run", lambda _: None)
    assert container.cleanup() == (False, "docker_unavailable")


def test_failed_remove_does_not_report_success(tmp_path, monkeypatch):
    container = DockerContainer(tmp_path, "a" * 32, "b" * 64)

    def run(argv):
        if "inspect" in argv:
            return result(
                argv, stdout=json.dumps({"id": "c" * 64, "labels": container.labels, "state": state()})
            )
        return result(argv, code=1)

    monkeypatch.setattr(container, "run", run)
    assert container.cleanup() == (False, "container_remove_failed")


def test_timed_out_create_is_not_treated_as_confirmed_absence(tmp_path, monkeypatch):
    container = DockerContainer(tmp_path, "a" * 32, "b" * 64)
    monkeypatch.setattr(container, "run", lambda _: None)
    assert not container.create(["docker", "create"])
    monkeypatch.setattr(container, "inspect", lambda: ("absent", None))
    assert container.cleanup() == (False, "create_unconfirmed")


@pytest.mark.parametrize("bad_state", [{}, state(OOMKilled="false"), state(ExitCode=True), state(Running=0)])
def test_invalid_daemon_facts_do_not_become_test_results(tmp_path, monkeypatch, bad_state):
    container = DockerContainer(tmp_path, "a" * 32, "b" * 64)
    monkeypatch.setattr(
        container,
        "run",
        lambda argv: result(
            argv,
            stdout=json.dumps(
                {
                    "id": "c" * 64,
                    "labels": container.labels,
                    "state": bad_state,
                }
            ),
        ),
    )
    assert container.inspect() == ("unavailable", None)


@pytest.mark.parametrize("failure", [OSError("private detail"), subprocess.TimeoutExpired("docker", 10)])
def test_daemon_failures_are_bounded_and_not_exposed(tmp_path, monkeypatch, failure):
    container = DockerContainer(tmp_path, "a" * 32, "b" * 64)

    def run(*_, **kwargs):
        assert kwargs["timeout"] == 10
        raise failure

    monkeypatch.setattr("buildmedic.runner_runtime.subprocess.run", run)
    assert container.cleanup() == (False, "docker_unavailable")


def test_restart_retries_owned_cleanup_and_preserves_unconfirmed_state(tmp_path, monkeypatch):
    from buildmedic.runner import Engine

    eid = "a" * 32
    workspace = tmp_path / "bm-run-synthetic"
    workspace.mkdir()
    record = {
        "id": eid,
        "request_hash": "b" * 64,
        "lifecycle": "running",
        "container_labels_version": 1,
        "workspace_name": workspace.name,
    }
    (tmp_path / (eid + ".json")).write_text(json.dumps(record))
    attempts = []
    cleaned = False

    def cleanup(container):
        attempts.append(container)
        return (True, None) if cleaned else (False, "docker_unavailable")

    monkeypatch.setattr(DockerContainer, "cleanup", cleanup)
    engine = Engine(backend="docker", image="sha256:" + "1" * 64, data_dir=tmp_path)
    try:
        value = engine.get(eid)
        assert value["lifecycle"] == "interrupted" and not value["passed"]
        assert value["termination"]["cleanup_confirmed"] is False
        assert not engine.ready()[0]
        assert not workspace.exists()
        assert len(attempts) == 1 and not attempts[0].legacy
        cleaned = True
        engine.cancel(eid)
        assert engine.get(eid)["termination"]["cleanup_confirmed"] is True
        assert engine.get(eid)["lifecycle"] == "interrupted"
        saved = json.loads((tmp_path / (eid + ".json")).read_text())
        assert saved["termination"]["cleanup_confirmed"] is True
    finally:
        engine.pool.shutdown(wait=True)


def test_verification_rejects_optimistic_results_with_unconfirmed_facts():
    from buildmedic.orchestration import verification_ok

    before = {
        "command": ["pytest"],
        "counts": {"collected": 1, "skipped": 0, "tests": [{"id": "test_a", "status": "passed"}]},
    }
    after = {**before, "reason": "exited", "passed": True}
    for changes in [
        {"cleanup_confirmed": False, "state_confirmed": True, "oom_killed": False},
        {"cleanup_confirmed": True, "state_confirmed": False, "oom_killed": False},
        {"cleanup_confirmed": True, "state_confirmed": True, "oom_killed": True},
    ]:
        invalid = {**after, "termination": changes}
        assert verification_ok([before, before], invalid, after)[0] is False


def test_runner_client_does_not_confirm_cancel_while_cleanup_is_pending():
    import asyncio

    import httpx

    from buildmedic.clients import RunnerClient

    async def flow():
        client = RunnerClient()
        await client.close()
        client.client = httpx.AsyncClient(
            base_url="http://runner",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"lifecycle": "interrupted", "termination": {"cleanup_confirmed": False}}
                )
            ),
        )
        try:
            assert await client.cancel("a" * 32) is False
        finally:
            await client.close()

    asyncio.run(flow())


def test_restart_retains_oom_facts_when_retrying_cleanup(tmp_path, monkeypatch):
    from buildmedic.runner import Engine

    eid = "a" * 32
    facts = ExecutionTermination(
        cause="oom_killed",
        cli_exit_code=137,
        container_exit_code=137,
        oom_killed=True,
        signal=9,
        container_status="exited",
        state_confirmed=True,
        cleanup_error="docker_unavailable",
    ).model_dump()
    record = {
        "id": eid,
        "request_hash": "b" * 64,
        "lifecycle": "interrupted",
        "reason": "cleanup_unconfirmed",
        "container_labels_version": 1,
        "termination": facts,
    }
    (tmp_path / (eid + ".json")).write_text(json.dumps(record))
    monkeypatch.setattr(DockerContainer, "cleanup", lambda _: (True, None))
    engine = Engine(backend="docker", image="sha256:" + "1" * 64, data_dir=tmp_path)
    try:
        value = engine.get(eid)
        assert value["lifecycle"] == "interrupted" and not value["passed"]
        expected = {**facts, "cleanup_confirmed": True, "cleanup_error": None}
        assert value["termination"] == expected
    finally:
        engine.pool.shutdown(wait=True)
