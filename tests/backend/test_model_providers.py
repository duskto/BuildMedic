"""Explicit transport fixtures; these tests do not claim real provider acceptance."""

import asyncio
import json

import httpx
import pytest

from buildmedic import evaluation
from buildmedic.agent_tools import TOOLS
from buildmedic.clients import ModelClient
from buildmedic.config import Settings, settings
from buildmedic.errors import AppError, Blocked

PROVIDERS = [
    ("kimi", "https://api.moonshot.cn/v1", "kimi-k2.6", "max_tokens", True),
    ("kimi", "https://api.moonshot.ai/v1", "kimi-k2.6", "max_tokens", True),
    ("glm", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3", "max_tokens", True),
    ("glm", "https://api.z.ai/api/paas/v4", "glm-4.7", "max_tokens", True),
    ("glm", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash", "max_tokens", False),
    ("openai", "https://api.openai.com/v1", "gpt-4.1", "max_completion_tokens", False),
]


def configure(monkeypatch, provider, url, model):
    cfg = settings()
    for key, value in {
        "model_provider": provider,
        "model_base_url": url,
        "model_id": model,
        "model_api_key": "fixture-private-api-key",
    }.items():
        monkeypatch.setattr(cfg, key, value)
    return cfg


@pytest.mark.parametrize("provider,url,model,limit,thinking", PROVIDERS)
def test_provider_json_and_two_turn_tools_preserve_usage_and_private_boundary(
    monkeypatch, provider, url, model, limit, thinking
):
    cfg = configure(monkeypatch, provider, url, model)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert str(request.url) == url + "/chat/completions"
        assert request.headers["authorization"] == "Bearer fixture-private-api-key"
        assert body[limit] == cfg.max_output_tokens
        assert ("max_tokens" in body) != ("max_completion_tokens" in body)
        assert body.get("thinking") == ({"type": "disabled"} if thinking else None)
        assert "reasoning_effort" not in body
        assert "PRIVATE_REASONING" not in json.dumps(body)
        message = {"content": '{"ok":true}', "reasoning_content": "PRIVATE_REASONING"}
        if "tools" in body:
            assert "response_format" not in body and body["tool_choice"] == "auto"
            message["tool_calls"] = [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "list_artifacts", "arguments": "{}"},
                }
            ]
        else:
            assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "id": "fixture-id",
                "model": model,
                "choices": [
                    {"finish_reason": "tool_calls" if "tools" in body else "stop", "message": message}
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50},
            },
        )

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(handler))
        try:
            _, usage = await client.complete([{"role": "user", "content": "Return JSON"}])
            assert usage["total_tokens"] == 50 and client.metadata["response_model"] == model
            frame, _ = await client.tool_turn([], TOOLS)
            assert "PRIVATE_REASONING" not in json.dumps(frame)
            await client.tool_turn(
                [dict(frame, role="assistant"), {"role": "tool", "tool_call_id": "call-1", "content": "{}"}],
                TOOLS,
            )
        finally:
            await client.close()

    asyncio.run(flow())
    assert len(seen) == 3 and cfg.agent_tools_ready


@pytest.mark.parametrize("provider,url,model,limit,thinking", PROVIDERS)
@pytest.mark.parametrize(
    "bad_url",
    [
        "https://wrong.invalid/v1",
        "http://api.openai.com/v1",
        "https://api.openai.com/v1?key=hidden",
        "https://user:pass@api.openai.com/v1",
    ],
)
def test_official_provider_rejects_mismatched_or_credentialled_endpoint_before_network(
    monkeypatch, provider, url, model, limit, thinking, bad_url
):
    configure(monkeypatch, provider, bad_url, model)

    def handler(_):
        raise AssertionError("Credentials must not be sent")

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(AppError) as error:
                await client.probe()
            assert error.value.code == "model_configuration"
        finally:
            await client.close()

    asyncio.run(flow())


@pytest.mark.parametrize(
    "provider,url,model",
    [("kimi", "https://api.moonshot.cn/v1", "kimi-k2.6"), ("openai", "https://api.openai.com/v1", "gpt-4.1")],
)
def test_models_probe_only_reads_model_list(monkeypatch, provider, url, model):
    configure(monkeypatch, provider, url, model)

    def handler(request):
        assert request.method == "GET" and str(request.url) == url + "/models"
        return httpx.Response(200, json={"data": [{"id": model}]})

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(handler))
        try:
            assert (await client.probe())["status"] == "available"
        finally:
            await client.close()

    asyncio.run(flow())


def test_glm_probe_reports_unverified_without_charging_or_claiming_auth(client, monkeypatch):
    configure(monkeypatch, "glm", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("No generation or unsupported model-list request")

    monkeypatch.setattr(ModelClient, "request_json", forbidden)
    response = client.post("/api/v1/model/check")
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["status"] == "unverified" and "尚未验证密钥" in result["message"]
    assert "fixture-private-api-key" not in response.text


@pytest.mark.parametrize(
    "provider,url,model",
    [
        ("kimi", "https://api.moonshot.cn/v1", "kimi-k2.6"),
        ("glm", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3"),
    ],
)
def test_thinking_tool_mode_fails_before_request(monkeypatch, provider, url, model):
    cfg = configure(monkeypatch, provider, url, model)
    monkeypatch.setattr(cfg, "model_thinking", "enabled")
    assert not cfg.agent_tools_ready

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(lambda _: pytest.fail("Must not send")))
        try:
            with pytest.raises(Blocked):
                await client.tool_turn([], TOOLS)
        finally:
            await client.close()

    asyncio.run(flow())


@pytest.mark.parametrize("model", ["kimi-k3.1-pro", "kimi-k2.7-code", "kimi-k2-thinking"])
def test_mandatory_kimi_thinking_is_explicit_and_tools_disabled(monkeypatch, model):
    cfg = configure(monkeypatch, "kimi", "https://api.moonshot.cn/v1", model)

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(lambda _: pytest.fail("No network expected")))
        try:
            with pytest.raises(AppError):
                client.configuration()
            monkeypatch.setattr(cfg, "model_thinking", "enabled")
            assert client.configuration() is cfg and not cfg.agent_tools_ready
            with pytest.raises(Blocked):
                await client.tool_turn([], TOOLS)
        finally:
            await client.close()

    asyncio.run(flow())


def test_reasoning_effort_reaches_openai_and_binds_evaluation_configuration(monkeypatch):
    cfg = configure(monkeypatch, "openai", "https://api.openai.com/v1", "gpt-5.4")
    baseline = evaluation.configuration()
    monkeypatch.setattr(cfg, "model_reasoning_effort", "none")
    assert evaluation.configuration() != baseline
    assert evaluation.configuration()["reasoning_effort"] == "none"

    def handler(request):
        assert json.loads(request.content)["reasoning_effort"] == "none"
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(handler))
        try:
            await client.complete([])
        finally:
            await client.close()

    asyncio.run(flow())


@pytest.mark.parametrize("alias,provider", [("chatgpt", "openai"), ("moonshot", "kimi"), ("zhipu", "glm")])
def test_provider_configuration_aliases(alias, provider):
    cfg = Settings(_env_file=None, model_provider=alias, model_reasoning_effort="")
    assert cfg.model_provider == provider and cfg.model_reasoning_effort is None


@pytest.mark.parametrize(
    "provider,model",
    [
        ("deepseek", "deepseek-flash"),
        ("kimi", "moonshot/kimi-k2.6"),
        ("glm", "zhipu/glm-5.3"),
        ("openai", "openai/gpt-4.1"),
    ],
)
def test_relay_keeps_provider_adaptation_and_accepts_custom_https_root(monkeypatch, provider, model):
    cfg = configure(monkeypatch, provider, "https://relay.invalid/custom/v1/", model)
    monkeypatch.setattr(cfg, "model_endpoint_mode", "relay")

    def handler(request):
        assert str(request.url) == "https://relay.invalid/custom/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == model
        if provider == "openai":
            assert "max_completion_tokens" in body and "thinking" not in body
        else:
            assert "max_tokens" in body and body["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(handler))
        try:
            assert (await client.complete([]))[0] == '{"ok":true}'
        finally:
            await client.close()

    asyncio.run(flow())


@pytest.mark.parametrize("status", [404, 405, 501, 401, 403, 429, 302])
def test_relay_probe_does_not_mask_auth_or_redirect_or_trigger_paid_fallback(monkeypatch, status):
    cfg = configure(monkeypatch, "glm", "https://relay.invalid/v1", "glm-5.3")
    monkeypatch.setattr(cfg, "model_endpoint_mode", "relay")
    seen = []

    def handler(request):
        seen.append(request)
        assert request.method == "GET" and request.url.path == "/v1/models"
        return httpx.Response(status, headers={"Location": "https://other.invalid/v1"})

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(handler))
        try:
            if status in {404, 405, 501}:
                assert (await client.probe())["status"] == "unverified"
            else:
                with pytest.raises(AppError) as error:
                    await client.probe()
                assert error.value.code != "model_not_found"
        finally:
            await client.close()

    asyncio.run(flow())
    assert len(seen) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://relay.invalid/v1",
        "https://key:password@relay.invalid/v1",
        "https://relay.invalid/v1?key=secret",
    ],
)
def test_relay_invalid_roots_never_receive_credentials(monkeypatch, url):
    cfg = configure(monkeypatch, "kimi", url, "kimi-k2.6")
    monkeypatch.setattr(cfg, "model_endpoint_mode", "relay")

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(lambda _: pytest.fail("Must not send")))
        try:
            with pytest.raises(AppError):
                await client.complete([])
        finally:
            await client.close()

    asyncio.run(flow())


def test_relay_token_field_override_is_bounded_and_bound_to_experiment(monkeypatch):
    cfg = configure(monkeypatch, "openai", "http://127.0.0.1:9999/v1", "gpt-relay-alias")
    monkeypatch.setattr(cfg, "model_endpoint_mode", "relay")
    baseline = evaluation.configuration()
    assert baseline["endpoint_mode"] == "relay"
    monkeypatch.setattr(cfg, "model_token_parameter", "max_tokens")
    assert (
        evaluation.configuration() != baseline
        and evaluation.configuration()["token_parameter"] == "max_tokens"
    )

    def handler(request):
        body = json.loads(request.content)
        assert body["max_tokens"] == cfg.max_output_tokens and "max_completion_tokens" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    async def flow():
        client = ModelClient(transport=httpx.MockTransport(handler))
        try:
            await client.complete([])
        finally:
            await client.close()

    asyncio.run(flow())


def test_glm_relay_can_verify_its_model_list(monkeypatch):
    cfg = configure(monkeypatch, "glm", "https://relay.invalid/v1", "glm-5.3")
    monkeypatch.setattr(cfg, "model_endpoint_mode", "relay")

    async def flow():
        client = ModelClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"id": "glm-5.3"}]}))
        )
        try:
            assert (await client.probe())["status"] == "available"
        finally:
            await client.close()

    asyncio.run(flow())


@pytest.mark.parametrize("generate", [False, True])
def test_cli_unverified_probe_requires_explicit_generation(monkeypatch, generate, capsys):
    from scripts import check_model

    seen = []
    cfg = settings()
    original = cfg.max_output_tokens

    class FixtureModel:
        def __init__(self):
            self.metadata = {}

        async def probe(self):
            return {"status": "unverified"}

        async def complete(self, messages):
            seen.append(cfg.max_output_tokens)
            return '{"ok":true}', {"total_tokens": 10}

        async def close(self):
            seen.append("closed")

    monkeypatch.setattr(check_model, "ModelClient", FixtureModel)
    assert asyncio.run(check_model.check(generate)) == (0 if generate else 2)
    assert seen == ([512, "closed"] if generate else ["closed"])
    assert cfg.max_output_tokens == original
