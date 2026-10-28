import asyncio
from email.utils import formatdate

import httpx
import pytest

from buildmedic.config import settings
from buildmedic.errors import AppError
from buildmedic.github import GitHubClient, retry_information


@pytest.fixture
def waits(monkeypatch):
    values = []

    async def sleep(delay):
        values.append(delay)

    monkeypatch.setattr("buildmedic.github.asyncio.sleep", sleep)
    return values


@pytest.mark.parametrize("failure", [500, 502, 503, 504, "connection", "timeout"])
def test_transient_reads_retry_whole_request_with_bounded_backoff(failure, waits):
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) < 3:
            if failure == "connection":
                raise httpx.ConnectError("sensitive upstream URL", request=request)
            if failure == "timeout":
                raise httpx.ReadTimeout("sensitive upstream URL", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json={"ok": True})

    async def flow():
        github = GitHubClient(transport=httpx.MockTransport(respond))
        try:
            assert await github.get("/repos/a/b", params={"page": 2}) == {"ok": True}
        finally:
            await github.close()

    asyncio.run(flow())
    assert len(calls) == 3 and waits == [0.25, 0.5]
    assert len({str(r.url) for r in calls}) == 1


def test_exhausted_transport_failures_are_safe_and_retryable(waits):
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ConnectError("SECRET upstream detail", request=request)

    async def flow():
        github = GitHubClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(AppError) as exc:
                await github.get("/repos/a/b")
            assert exc.value.code == "github_connection" and exc.value.retryable
            assert "SECRET" not in str(exc.value)
        finally:
            await github.close()

    asyncio.run(flow())
    assert len(calls) == 3 and len(waits) == 2


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 410, 422])
def test_permanent_errors_are_not_retried(status, waits):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status)

    async def flow():
        github = GitHubClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(AppError):
                await github.get("/repos/a/b")
        finally:
            await github.close()

    asyncio.run(flow())
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"retry-after": "30"},
        {"retry-after": "9" * 100},
        {"retry-after": "0", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1060"},
    ],
)
def test_long_or_unspecified_rate_limits_do_not_sleep_or_retry(headers, waits, monkeypatch):
    monkeypatch.setattr("buildmedic.github.time.time", lambda: 1000)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers=headers)

    async def flow():
        github = GitHubClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(AppError) as exc:
                await github.get("/repos/a/b")
            assert exc.value.code == "github_rate_limited"
            assert exc.value.details["retry_at"] >= 1030
        finally:
            await github.close()

    asyncio.run(flow())
    assert len(calls) == 1 and waits == []


def test_short_explicit_rate_limit_is_honored(waits, monkeypatch):
    monkeypatch.setattr("buildmedic.github.time.time", lambda: 1000)
    calls = []

    def respond(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"retry-after": "1"})
            if len(calls) == 1
            else httpx.Response(200, json={"ok": True})
        )

    async def flow():
        github = GitHubClient(transport=httpx.MockTransport(respond))
        try:
            assert await github.get("/repos/a/b") == {"ok": True}
        finally:
            await github.close()

    asyncio.run(flow())
    assert len(calls) == 2 and waits == [1]


def test_retry_dates_and_untrusted_headers_are_parsed_without_echo(monkeypatch):
    monkeypatch.setattr("buildmedic.github.time.time", lambda: 1000)
    assert retry_information(httpx.Headers({"retry-after": formatdate(1010, usegmt=True)})) == {
        "retry_at": 1010
    }
    assert retry_information(httpx.Headers({"retry-after": "SECRET", "x-ratelimit-reset": "SECRET"})) == {}


@pytest.mark.parametrize("failure", ["expired", "partial", "server"])
def test_download_retry_gets_fresh_signed_link_and_discards_partial_body(failure, waits, monkeypatch):
    monkeypatch.setattr(settings(), "github_token", "synthetic-api-credential")
    api_calls, downloads, closed = [], [], []

    def api(request):
        api_calls.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-api-credential"
        return httpx.Response(
            302,
            headers={
                "location": f"https://logs.blob.core.windows.net/log?sig=link-{len(api_calls)}",
                "set-cookie": "api_cookie=private; Path=/",
            },
        )

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"discarded partial output\n"
            raise httpx.ReadError("private signed URL")

        async def aclose(self):
            closed.append(True)

    def download(request):
        downloads.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if len(downloads) == 1:
            if failure == "partial":
                return httpx.Response(200, stream=Broken())
            return httpx.Response(403 if failure == "expired" else 503)
        return httpx.Response(200, text="complete log\napi_key=synthetic-private-value")

    async def flow():
        github = GitHubClient(
            transport=httpx.MockTransport(api), download_transport=httpx.MockTransport(download)
        )
        try:
            assert await github.logs("a/b", 7) == "complete log\napi_key=[REDACTED]"
        finally:
            await github.close()

    asyncio.run(flow())
    assert len(api_calls) == len(downloads) == 2 and waits == [0.25]
    assert [r.url.params["sig"] for r in downloads] == ["link-1", "link-2"]
    if failure == "partial":
        assert closed == [True]


def test_retry_wait_is_cancellable_without_another_request(monkeypatch):
    calls = []

    async def flow():
        waiting = asyncio.Event()

        async def sleep(_):
            waiting.set()
            await asyncio.Future()

        monkeypatch.setattr("buildmedic.github.asyncio.sleep", sleep)

        def respond(request):
            calls.append(request)
            return httpx.Response(503)

        github = GitHubClient(transport=httpx.MockTransport(respond))
        task = asyncio.create_task(github.logs("a/b", 7))
        try:
            await waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await github.close()

    asyncio.run(flow())
    assert len(calls) == 1
