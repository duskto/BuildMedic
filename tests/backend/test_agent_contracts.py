import asyncio
import json

import httpx
import pytest

from buildmedic import store
from buildmedic.agent_tools import TOOLS
from buildmedic.clients import ModelClient
from buildmedic.config import settings
from buildmedic.errors import AppError, Blocked
from buildmedic.schemas import CreateTask, Source


def configure(monkeypatch):
    cfg = settings()
    for key, value in {
        "model_provider": "deepseek",
        "model_base_url": "https://api.deepseek.com",
        "model_api_key": "test-tool-key",
        "model_id": "deepseek-flash",
        "max_model_calls": 32,
    }.items():
        monkeypatch.setattr(cfg, key, value)
    return cfg


def test_native_tool_turn_preserves_protocol_without_private_reasoning(monkeypatch):
    configure(monkeypatch)
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert "tools" in body and "response_format" not in body
        assert body["tool_choice"] == "auto"
        assert "PRIVATE_HIDDEN_TRACE" not in json.dumps(body)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "reasoning_content": "PRIVATE_HIDDEN_TRACE",
                            "content": "UNNEEDED_ASSISTANT_PROSE",
                            "tool_calls": [
                                {
                                    "id": f"call_{len(bodies)}",
                                    "type": "function",
                                    "function": {
                                        "name": "read_repo_file",
                                        "arguments": '{"path":"src/demo.py"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"total_tokens": 31},
            },
        )

    async def flow():
        model = ModelClient(transport=httpx.MockTransport(handler))
        try:
            messages = [{"role": "user", "content": "Inspect fixed source"}]
            first, usage = await model.tool_turn(messages, TOOLS)
            assert usage["total_tokens"] == 31 and first["content"] is None
            assert "PRIVATE_HIDDEN_TRACE" not in json.dumps(first)
            assert "UNNEEDED_ASSISTANT_PROSE" not in json.dumps(first)
            messages.extend([first, {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'}])
            await model.tool_turn(messages, TOOLS)
        finally:
            await model.close()

    asyncio.run(flow())
    assert bodies[1]["messages"][-1]["tool_call_id"] == "call_1"


def test_tool_mode_requires_disabled_thinking_before_sending_request(monkeypatch):
    cfg = configure(monkeypatch)
    monkeypatch.setattr(cfg, "model_thinking", "enabled")

    def handler(_):
        raise AssertionError("Must not send a request requiring hidden reasoning persistence")

    async def flow():
        model = ModelClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(Blocked):
                await model.tool_turn([], TOOLS)
        finally:
            await model.close()

    asyncio.run(flow())


@pytest.mark.parametrize("kind", ["duplicate_ids", "object_arguments"])
def test_invalid_native_tool_envelope_is_rejected_before_dispatch(monkeypatch, kind):
    configure(monkeypatch)
    call = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "read_repo_file",
            "arguments": "{}" if kind == "duplicate_ids" else {"path": "src/demo.py"},
        },
    }

    async def flow():
        model = ModelClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"tool_calls": [call, call] if kind == "duplicate_ids" else [call]}}
                        ]
                    },
                )
            )
        )
        try:
            with pytest.raises(AppError) as error:
                await model.tool_turn([], TOOLS)
            assert error.value.code == "model_format_error"
        finally:
            await model.close()

    asyncio.run(flow())


def test_agent_tools_opt_in_budget_and_idempotency_keep_legacy_limits(client, monkeypatch):
    cfg = configure(monkeypatch)
    body = CreateTask(
        source=Source(case_id="pagination"),
        agent_tools=True,
        limits={"max_model_calls": 16, "max_tokens": 150000},
    ).model_dump()
    headers = {"Idempotency-Key": "agent-tool-task-key"}
    response = client.post("/api/v1/tasks", json=body, headers=headers)
    assert response.status_code == 202, response.text
    tid = response.json()["data"]["task_id"]
    assert store.snapshot(tid)["agent_tools"] is True
    assert (
        client.post(
            "/api/v1/tasks",
            json=dict(body, agent_tools=False),
            headers={"Idempotency-Key": "no-tool-budget-key"},
        ).status_code
        == 422
    )
    monkeypatch.setattr(cfg, "model_id", "")
    assert client.post("/api/v1/tasks", json=body, headers=headers).json()["data"]["task_id"] == tid
    assert (
        client.post(
            "/api/v1/tasks", json=body, headers={"Idempotency-Key": "disabled-agent-tools"}
        ).status_code
        == 422
    )
    legacy = CreateTask(source=Source(case_id="pagination")).model_dump(exclude={"agent_tools"})
    assert store.request_hash(legacy) == store.request_hash(dict(legacy, agent_tools=False))
