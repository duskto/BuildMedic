import asyncio
import json

import httpx
import pytest
import uvloop

from buildmedic.clients import ModelClient
from buildmedic.config import settings
from buildmedic.errors import AppError


def test_real_transport_payload_and_missing_usage(monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "model_base_url", "https://provider.invalid/v1")
    monkeypatch.setattr(cfg, "model_api_key", "test-api-key")
    monkeypatch.setattr(cfg, "model_id", "explicit-model-id")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"summary":"transport fixture"}'}}]}
        )

    async def flow():
        model = ModelClient()
        await model.client.aclose()
        model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await model.complete([{"role": "user", "content": "diagnose"}])
        finally:
            await model.close()

    content, usage = asyncio.run(flow(), loop_factory=uvloop.new_event_loop)
    assert json.loads(content)["summary"] == "transport fixture"
    assert usage is None
    assert str(seen[0].url) == "https://provider.invalid/v1/chat/completions"
    body = json.loads(seen[0].content)
    assert body["model"] == "explicit-model-id"
    assert body["max_tokens"] == cfg.max_output_tokens
    assert "thinking" not in body
    assert seen[0].headers["authorization"] == "Bearer test-api-key"


@pytest.mark.parametrize("status,expected", [(429, "model_http_error"), (500, "model_http_error")])
def test_provider_http_failure_is_typed(status, expected, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "model_base_url", "https://provider.invalid/v1")
    monkeypatch.setattr(cfg, "model_api_key", "test-api-key")
    monkeypatch.setattr(cfg, "model_id", "explicit-model")

    async def flow():
        model = ModelClient()
        await model.client.aclose()
        model.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status)))
        try:
            with pytest.raises(AppError) as error:
                await model.complete([])
            assert error.value.code == expected
        finally:
            await model.close()

    asyncio.run(flow(), loop_factory=uvloop.new_event_loop)


def setup_deepseek(monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "model_provider", "deepseek")
    monkeypatch.setattr(cfg, "model_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(cfg, "model_api_key", "test-deepseek-key")
    monkeypatch.setattr(cfg, "model_id", "deepseek-flash")
    return cfg


def test_deepseek_thinking_and_response_provenance(monkeypatch):
    setup_deepseek(monkeypatch)

    def handler(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200,
            json={
                "id": "response-fixture",
                "model": "reported-model-version",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"ok":true}',
                            "reasoning_content": "PRIVATE REASONING MUST NOT BE RETAINED",
                        },
                    }
                ],
                "usage": {"total_tokens": 27},
            },
        )

    async def flow():
        model = ModelClient(transport=httpx.MockTransport(handler))
        try:
            content, usage = await model.complete([])
            assert content == '{"ok":true}' and usage == {"total_tokens": 27}
            assert model.metadata == {
                "response_id": "response-fixture",
                "response_model": "reported-model-version",
                "finish_reason": "stop",
            }
            assert "PRIVATE" not in json.dumps(model.metadata)
        finally:
            await model.close()

    asyncio.run(flow(), loop_factory=uvloop.new_event_loop)


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "model_authentication", False),
        (402, "model_insufficient_balance", False),
        (403, "model_forbidden", False),
        (404, "model_not_found", False),
        (429, "model_http_error", True),
        (503, "model_http_error", True),
    ],
)
def test_deepseek_errors_do_not_expose_provider_body(status, code, retryable, monkeypatch):
    setup_deepseek(monkeypatch)

    async def flow():
        model = ModelClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    status, text="sensitive-provider-body", headers={"Retry-After": "15"}
                )
            )
        )
        try:
            with pytest.raises(AppError) as error:
                await model.probe()
            assert error.value.code == code and error.value.retryable is retryable
            assert "sensitive" not in str(error.value)
            assert error.value.details["retry_after_seconds"] == 15
        finally:
            await model.close()

    asyncio.run(flow(), loop_factory=uvloop.new_event_loop)


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example",
        "http://api.deepseek.com",
        "https://api.deepseek.com/anthropic",
        "https://api.deepseek.com?secret=hidden",
        "https://user:password@api.deepseek.com",
    ],
)
def test_official_mode_never_sends_key_to_a_different_endpoint(url, monkeypatch):
    cfg = setup_deepseek(monkeypatch)
    monkeypatch.setattr(cfg, "model_base_url", url)

    def handler(_request):
        raise AssertionError("Invalid configuration must be rejected before sending the key")

    async def flow():
        model = ModelClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(AppError) as error:
                await model.probe()
            assert error.value.code == "model_configuration"
        finally:
            await model.close()

    asyncio.run(flow(), loop_factory=uvloop.new_event_loop)


def test_probe_checks_selected_model_without_calling_completions(monkeypatch):
    cfg = setup_deepseek(monkeypatch)
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})

    async def flow():
        model = ModelClient(transport=httpx.MockTransport(handler))
        try:
            result = await model.probe()
            assert result["status"] == "available"
            monkeypatch.setattr(cfg, "model_id", "missing-model")
            with pytest.raises(AppError) as error:
                await model.probe()
            assert error.value.code == "model_not_found"
        finally:
            await model.close()

    asyncio.run(flow(), loop_factory=uvloop.new_event_loop)
    assert seen == [("GET", "/models"), ("GET", "/models")]


@pytest.mark.parametrize(
    "finish,content,code",
    [
        ("length", '{"unfinished":', "model_output_truncated"),
        ("stop", " ", "model_empty_response"),
        ("content_filter", "", "model_generation_stopped"),
    ],
)
def test_incomplete_generation_is_an_actionable_error(finish, content, code, monkeypatch):
    setup_deepseek(monkeypatch)

    async def flow():
        model = ModelClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"choices": [{"finish_reason": finish, "message": {"content": content}}]}
                )
            )
        )
        try:
            with pytest.raises(AppError) as error:
                await model.complete([])
            assert error.value.code == code
        finally:
            await model.close()

    asyncio.run(flow(), loop_factory=uvloop.new_event_loop)
