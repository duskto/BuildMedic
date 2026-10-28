import asyncio

import httpx
import pytest
import uvloop

from buildmedic.errors import AppError
from buildmedic.github import GitHubClient, parse_run_url


def run(coro):
    return asyncio.run(coro, loop_factory=uvloop.new_event_loop)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b/actions/runs/1",
        "https://github.com.evil.test/a/b/actions/runs/1",
        "https://user@github.com/a/b/actions/runs/1",
        "https://github.com:443/a/b/actions/runs/1",
        "https://github.com/a/b/actions/runs/1/../../secrets",
        "https://127.0.0.1/a/b/actions/runs/1",
    ],
)
def test_run_url_rejects_untrusted_hosts(url):
    with pytest.raises(AppError):
        parse_run_url(url)


def test_fixed_attempt_and_paginated_jobs():
    seen = []
    sha = "a" * 40

    def response(request):
        seen.append(str(request.url))
        path = request.url.path
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"private": False, "full_name": "owner/repo"})
        if path.endswith("/runs/42"):
            return httpx.Response(200, json={"id": 42, "run_attempt": 3, "head_sha": "b" * 40})
        if path.endswith("/attempts/2"):
            return httpx.Response(200, json={"id": 42, "run_attempt": 2, "head_sha": sha})
        if path.endswith("/attempts/2/jobs"):
            jobs = (
                [
                    {
                        "id": i,
                        "run_id": 42,
                        "run_attempt": 2,
                        "head_sha": sha,
                        "conclusion": "failure",
                        "name": "test",
                        "steps": [],
                    }
                    for i in range(100)
                ]
                if request.url.params["page"] == "1"
                else [
                    {
                        "id": 101,
                        "run_id": 42,
                        "run_attempt": 2,
                        "head_sha": sha,
                        "conclusion": "failure",
                        "name": "late job",
                        "steps": [],
                    }
                ]
            )
            return httpx.Response(200, json={"jobs": jobs})
        raise AssertionError(str(request.url))

    async def flow():
        client = GitHubClient(transport=httpx.MockTransport(response))
        try:
            return await client.preview("https://github.com/owner/repo/actions/runs/42?attempt=2")
        finally:
            await client.close()

    result = run(flow())
    assert result["run_attempt"] == 2 and result["head_sha"] == sha
    assert len(result["jobs"]) == 101
    assert not any("/runs/42/jobs" in url for url in seen)


def test_rate_limit_preserves_retry_information():
    async def flow():
        client = GitHubClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "999"}
                )
            )
        )
        try:
            with pytest.raises(AppError) as exc:
                await client.preview("https://github.com/a/b/actions/runs/1")
            assert exc.value.code == "github_rate_limited"
            assert exc.value.details["reset_at"] == "999"
        finally:
            await client.close()

    run(flow())


def test_log_redirect_does_not_send_credentials_to_untrusted_host():
    async def flow():
        client = GitHubClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(302, headers={"Location": "https://attacker.example/logs"})
            )
        )
        try:
            with pytest.raises(AppError) as exc:
                await client.logs("a/b", 123)
            assert exc.value.code == "unsafe_log_redirect"
        finally:
            await client.close()

    run(flow())


@pytest.mark.parametrize(
    "kind,limit,code",
    [("logs", 2_000_000, "log_too_large"), ("metadata", 4_000_000, "github_response_too_large")],
)
def test_stream_limit_stops_before_consuming_the_response(kind, limit, code):
    consumed = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(10):
                consumed.append(i)
                yield b"x" * 1_000_000

    async def flow():
        client = GitHubClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks())))
        try:
            with pytest.raises(AppError) as exc:
                if kind == "logs":
                    await client.logs("a/b", 123)
                else:
                    await client.get("/repos/a/b")
            assert exc.value.code == code
            assert len(consumed) == limit // 1_000_000 + 1
        finally:
            await client.close()

    run(flow())


def test_registered_job_preview_and_creation_pin_server_profile(registered_remote, client, monkeypatch):
    from buildmedic import store

    p, _ = registered_remote
    head_repo = p.repository

    def respond(request):
        path = request.url.path
        if path == f"/repos/{p.repository}":
            return httpx.Response(200, json={"private": False, "full_name": p.repository})
        if path.endswith("/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 17,
                            "run_id": 42,
                            "run_attempt": 2,
                            "head_sha": p.head_sha,
                            "conclusion": "failure",
                            "name": "pytest",
                            "steps": [],
                        },
                        {
                            "id": 18,
                            "run_id": 42,
                            "run_attempt": 2,
                            "head_sha": p.head_sha,
                            "conclusion": "failure",
                            "name": "lint",
                            "steps": [],
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 42,
                "run_attempt": 2,
                "head_sha": p.head_sha,
                "path": p.workflow_path,
                "head_repository": {"full_name": head_repo},
            },
        )

    monkeypatch.setattr(
        "buildmedic.api.GitHubClient", lambda: GitHubClient(transport=httpx.MockTransport(respond))
    )
    monkeypatch.setattr("buildmedic.api.capability", lambda: {"ready": True, "message": "test"})
    url = f"https://github.com/{p.repository}/actions/runs/42?attempt=2"
    preview = client.post("/api/v1/ci/preview", json={"run_url": url}).json()["data"]
    assert preview["execution_supported"] is True
    assert [j["execution_supported"] for j in preview["jobs"]] == [True, False]
    body = {
        "source": {"type": "github_actions", "run_url": url, "run_attempt": 2, "job_id": 17},
        "mode": "adaptive",
        "limits": {"max_model_calls": 12, "max_tokens": 100000},
    }
    response = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "registered-adaptive"})
    assert response.status_code == 202, response.text
    tid = response.json()["data"]["task_id"]
    snap = store.snapshot(tid)
    assert snap["runner_profile"] == p.binding()
    assert snap["snapshot_sha256"] == p.snapshot_sha256
    head_repo = "fork/pytest-demo"
    assert not client.post("/api/v1/ci/preview", json={"run_url": url}).json()["data"]["execution_supported"]
    assert (
        client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "registered-adaptive"}).json()[
            "data"
        ]["task_id"]
        == tid
    )
    rejected = client.post("/api/v1/tasks", json=body, headers={"Idempotency-Key": "unregistered-fork"})
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "adaptive_source_unsupported"
