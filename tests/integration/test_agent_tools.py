"""Scripted native function-calling HTTP, real pytest, and task/rollout isolation."""

import asyncio
import json

import httpx
import pytest
import uvloop

from buildmedic import runner as runner_module
from buildmedic import store
from buildmedic.cases import metadata
from buildmedic.clients import ModelClient, RunnerClient
from buildmedic.config import settings
from buildmedic.orchestration import run_claimed
from buildmedic.runner import Engine
from buildmedic.schemas import CreateTask, Source


def run(coro):
    return asyncio.run(coro, loop_factory=uvloop.new_event_loop)


@pytest.mark.parametrize("max_calls, expected_calls, repaired", [(24, 20, True), (11, 11, False)])
def test_independent_fixed_tools_share_total_budget_and_cannot_see_other_rollouts(
    environment, max_calls, expected_calls, repaired
):
    from buildmedic import evaluation

    log = evaluation.captured_log("pagination")
    spec = evaluation.FourArmSpec(
        schema_version="2.0",
        name="tool-independent-test",
        agent_tools=True,
        limits={"max_model_calls": max_calls, "max_tokens": 250000},
        cases=[
            evaluation.FourArmCase(
                case_id="pagination", snapshot_sha256=log.snapshot_sha256, log_sha256=log.log_sha256
            )
        ],
    )
    eid = evaluation.create("tools-owner", "tool-independent-test", spec)
    while True:
        tid = evaluation.advance(eid, "tools-owner")["task_id"]
        if store.snapshot(tid)["experiment_binding"]["arm"] == "multi":
            break
        store.cancel(tid, "tools-owner")
    provider = FunctionAPI()
    snap = execute(tid, store.claim(tid), provider)
    assert snap["outcome"] == ("repaired" if repaired else "budget_exhausted"), snap["message"]
    assert snap["usage"]["calls"] == expected_calls
    initial = [
        json.loads(body["messages"][1]["content"]) for body in provider.calls if len(body["messages"]) == 2
    ]
    assert len(initial) == (4 if repaired else 3)
    assert len({i["rollout_id"] for i in initial}) == len(initial)
    assert all(
        i["previous_feedback"] == {}
        and set(i["visible_execution_ids"]) == {e["id"] for e in snap["baseline"]}
        for i in initial
    )
    if repaired:
        assert len(snap["candidates"]) == 4 and len(snap["executions"]) == 20
        assert sum(c["selected"] for c in snap["candidates"]) == 1
    else:
        assert not any(c["selected"] for c in snap["candidates"])
        assert not any(v["kind"] == "independent" for v in snap["validations"])


class InProcessRunner(RunnerClient):
    def __init__(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runner_module.app),
            base_url="http://runner",
            headers={"Authorization": "Bearer " + settings().runner_token},
        )


class FunctionAPI:
    """A deliberately scripted model. Its tool results always come from the real platform."""

    def __init__(self, variant="normal", foreign=None):
        self.variant, self.foreign = variant, foreign
        self.rounds, self.calls = {}, []

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(body)
        if "tools" not in body:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"files": {}, "notes": "Explicit scripted keep-base proposal."}
                                )
                            }
                        }
                    ],
                    "usage": {"total_tokens": 100},
                },
            )
        assert "response_format" not in body
        offered = {t["function"]["name"] for t in body["tools"]}
        assert offered == {"submit_diagnosis"} or offered == {
            "read_ci_log",
            "read_repo_file",
            "search_code",
            "propose_patch",
            "run_check",
            "read_check_result",
            "submit_diagnosis",
        }
        messages = body["messages"]
        assert all("reasoning_content" not in message for message in messages)
        initial = json.loads(messages[1]["content"])
        rid = initial["rollout_id"]
        number = self.rounds.get(rid, 0) + 1
        self.rounds[rid] = number
        if self.variant == "plain_text":
            calls = []
        elif self.variant == "forever":
            calls = [("read_repo_file", {"path": initial["source_index"][0]["path"]})]
        elif number == 1 and self.variant == "unsafe_batch":
            calls = [
                ("read_repo_file", {"path": initial["source_index"][0]["path"]}),
                ("run_shell", {"command": "must-not-execute"}),
            ]
        elif number == 1 and self.variant == "foreign":
            calls = [
                ("read_repo_file", {"path": "../../.env"}),
                ("read_ci_log", {"artifact_id": self.foreign}),
                ("run_check", {"candidate_id": "other-rollout", "check_id": "target"}),
                ("read_check_result", {"execution_id": "other-task"}),
            ]
        elif number == 1 and self.variant in {
            "unread_patch",
            "duplicate_arguments",
            "self_certification",
            "readonly_patch",
            "unknown_evidence",
        }:
            source = next(s for s in initial["source_index"] if s["path"].endswith("pagination.py"))
            if self.variant == "duplicate_arguments":
                calls = [("read_repo_file", '{"path":"src/demo/pagination.py","path":"../../.env"}')]
            elif self.variant in {"unread_patch", "readonly_patch"}:
                calls = [
                    (
                        "propose_patch",
                        {
                            "explanation": "Rejected safety fixture",
                            "edits": [
                                {
                                    "path": source["path"]
                                    if self.variant == "unread_patch"
                                    else "tests/test_pagination.py",
                                    "before": "start + page_size - 1",
                                    "after": "start + page_size",
                                }
                            ],
                        },
                    )
                ]
            else:
                args = {
                    "diagnosis": {
                        "summary": "Rejected safety fixture",
                        "failure_category": "test",
                        "root_cause": {
                            "description": "No trusted proof",
                            "certainty": "supported",
                            "evidence_ids": [
                                "ev-not-here" if self.variant == "unknown_evidence" else source["evidence_id"]
                            ],
                        },
                        "next_action": "Inspect evidence",
                    }
                }
                if self.variant == "self_certification":
                    args.update(validation_status="passed", reasoning_content="UNVALIDATED_PRIVATE_TRACE")
                calls = [("submit_diagnosis", args)]
        else:
            step = number - (
                1
                if self.variant
                in {
                    "unsafe_batch",
                    "foreign",
                    "unread_patch",
                    "duplicate_arguments",
                    "self_certification",
                    "readonly_patch",
                    "unknown_evidence",
                }
                else 0
            )
            calls = self.script(step, initial, messages)
        native = [
            {
                "id": f"call_{rid}_{number}_{index}",
                "type": "function",
                "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)},
            }
            for index, (name, args) in enumerate(calls)
        ]
        return httpx.Response(
            200,
            json={
                "model": "scripted-native-tools",
                "choices": [
                    {
                        "finish_reason": "tool_calls" if native else "stop",
                        "message": {
                            "content": "VISIBLE_PROSE_MUST_NOT_BE_RETAINED",
                            "reasoning_content": "PRIVATE_REASONING_MUST_NOT_BE_RETAINED",
                            "tool_calls": native,
                        },
                    }
                ],
                "usage": {"total_tokens": 100},
            },
        )

    @staticmethod
    def results(messages):
        calls = {
            c["id"]: c["function"]["name"]
            for m in messages
            if m["role"] == "assistant"
            for c in m.get("tool_calls", [])
        }
        return [(calls[m["tool_call_id"]], json.loads(m["content"])) for m in messages if m["role"] == "tool"]

    def script(self, step, initial, messages):
        source = next(s for s in initial["source_index"] if s["path"].endswith("pagination.py"))
        results = self.results(messages)
        if step == 1:
            return [
                ("read_repo_file", {"path": source["path"]}),
                ("read_ci_log", {"artifact_id": initial["log_index"][0]["artifact_id"]}),
                ("search_code", {"query": "page_size"}),
            ]
        if step == 2:
            return [
                (
                    "propose_patch",
                    {
                        "explanation": "Scripted native tool candidate",
                        "edits": [
                            {
                                "path": source["path"],
                                "before": "start + page_size - 1",
                                "after": "start + page_size",
                            }
                        ],
                    },
                )
            ]
        candidate = next(
            result["result"]["id"] for name, result in results if name == "propose_patch" and result["ok"]
        )
        if step == 3:
            return [
                ("run_check", {"candidate_id": candidate, "check_id": "target"}),
                ("run_check", {"candidate_id": candidate, "check_id": "regression"}),
            ]
        if step == 4:
            if self.variant == "cache":
                return [
                    ("run_check", {"candidate_id": candidate, "check_id": "target"}),
                    ("run_check", {"candidate_id": candidate, "check_id": "regression"}),
                ]
            eid = next(
                result["result"]["id"] for name, result in results if name == "run_check" and result["ok"]
            )
            return [("read_check_result", {"execution_id": eid})]
        evidence = next(
            result["result"]["evidence_ids"][0]
            for name, result in results
            if name == "read_repo_file" and result["ok"]
        )
        return [
            (
                "submit_diagnosis",
                {
                    "candidate_id": candidate,
                    "diagnosis": {
                        "failure_category": "boundary",
                        "summary": "Explicit scripted tool diagnosis",
                        "root_cause": {
                            "description": "Boundary contract mismatch",
                            "certainty": "supported",
                            "evidence_ids": [evidence],
                        },
                        "limitations": ["Scripted model used only for tests"],
                        "next_action": "Inspect independent verification",
                    },
                },
            )
        ]


@pytest.fixture
def environment(tmp_path, monkeypatch):
    engine = Engine(backend="trusted-local", data_dir=tmp_path / "runner")
    monkeypatch.setattr(runner_module, "_engine", engine)
    cfg = settings()
    for key, value in {
        "model_base_url": "https://tools.invalid",
        "model_api_key": "scripted-tool-key",
        "model_id": "scripted-tools",
        "max_model_calls": 32,
        "adaptive_enabled": True,
    }.items():
        monkeypatch.setattr(cfg, key, value)
    yield engine
    engine.pool.shutdown(wait=True)


def task(mode="standard", limits=None):
    body = CreateTask(
        source=Source(case_id="pagination"),
        mode=mode,
        agent_tools=True,
        limits=limits or {"max_model_calls": 32, "max_tokens": 250000},
    ).model_dump()
    tid = store.create("tools-owner", store.uid(), body, metadata("pagination"))
    return tid, store.claim(tid)


def execute(tid, claimed, provider, runner=None):
    run(
        run_claimed(
            tid,
            claimed,
            runner=runner or InProcessRunner(),
            model=ModelClient(transport=httpx.MockTransport(provider)),
        )
    )
    return store.snapshot(tid)


@pytest.mark.parametrize("mode", ["standard", "adaptive"])
@pytest.mark.parametrize("provider", ["compatible", "kimi", "glm", "openai"])
def test_native_tools_read_search_propose_check_and_submit_then_independent_validation(
    environment, mode, provider, monkeypatch
):
    from buildmedic.model_providers import OFFICIAL_ENDPOINTS

    if provider != "compatible":
        cfg = settings()
        host, path = min(OFFICIAL_ENDPOINTS[provider])
        monkeypatch.setattr(cfg, "model_provider", provider)
        monkeypatch.setattr(cfg, "model_base_url", "https://" + host + path)
        monkeypatch.setattr(
            cfg, "model_id", {"kimi": "kimi-k2.6", "glm": "glm-5.3", "openai": "gpt-4.1"}[provider]
        )
    if mode == "adaptive":
        pytest.importorskip("harness2")
    tid, claimed = task(mode)
    provider = FunctionAPI()
    snap = execute(tid, claimed, provider)
    assert snap["outcome"] == "repaired", snap["message"]
    assert snap["usage"]["calls"] == (23 if mode == "adaptive" else 5)
    assert snap["usage"]["tokens"] == snap["usage"]["calls"] * 100
    assert len(snap["agent_actions"]) == (32 if mode == "adaptive" else 8)
    assert all(a["status"] == "completed" for a in snap["agent_actions"])
    assert len(snap["candidates"]) == (4 if mode == "adaptive" else 1)
    assert len(snap["executions"]) == (20 if mode == "adaptive" else 8)
    candidate = next(c for c in snap["candidates"] if c["selected"])
    independent = next(v for v in snap["validations"] if v["kind"] == "independent")
    assert independent["candidate_id"] == candidate["id"] and independent["status"] == "passed"
    tool_executions = {
        json.loads(store.get_artifact(tid, a["result_artifact_id"])["content"])["result"]["id"]
        for a in snap["agent_actions"]
        if a["name"] == "run_check"
    }
    assert (
        independent["target"]["id"] not in tool_executions
        and independent["regression"]["id"] not in tool_executions
    )
    assert len([a for a in snap["artifacts"] if a["kind"] == "patch"]) == len(snap["candidates"])
    ids = {e["id"] for e in snap["evidence"]}
    assert set(snap["diagnosis"]["root_cause"]["evidence_ids"]) <= ids
    for art in snap["artifacts"]:
        text = store.get_artifact(tid, art["id"])["content"]
        assert "PRIVATE_REASONING_MUST_NOT_BE_RETAINED" not in text
        assert "VISIBLE_PROSE_MUST_NOT_BE_RETAINED" not in text


@pytest.mark.parametrize(
    "variant",
    [
        "unsafe_batch",
        "foreign",
        "unread_patch",
        "duplicate_arguments",
        "self_certification",
        "readonly_patch",
        "unknown_evidence",
    ],
)
def test_invalid_batch_has_no_prior_read_effects_and_cannot_cross_task_or_rollout(environment, variant):
    foreign = store.create(
        "another-owner",
        "foreign-source",
        CreateTask(source=Source(case_id="pagination")).model_dump(),
        metadata("pagination"),
    )
    token, *_ = store.claim(foreign)
    artifact = store.artifact(foreign, "ci_log", "FOREIGN_PRIVATE_MATERIAL", token)
    store.finish(foreign, token, "completed", "blocked")
    provider = FunctionAPI(variant, artifact["id"])
    tid, claimed = task()
    snap = execute(tid, claimed, provider)
    assert snap["outcome"] == "repaired", snap["message"]
    first = [a for a in snap["agent_actions"] if a["round"] == 1]
    assert first and all(a["status"] == "rejected" for a in first)
    assert len(snap["candidates"]) == 1 and len(snap["executions"]) == 8
    for action in first:
        result = json.loads(store.get_artifact(tid, action["result_artifact_id"])["content"])
        assert result["ok"] is False
        assert "FOREIGN_PRIVATE_MATERIAL" not in json.dumps(result)
    assert "UNVALIDATED_PRIVATE_TRACE" not in "\n".join(
        store.get_artifact(tid, a["id"])["content"] for a in snap["artifacts"]
    )


def test_duplicate_tool_checks_reuse_observations_but_final_checks_remain_independent(environment):
    tid, claimed = task()
    snap = execute(tid, claimed, FunctionAPI("cache"))
    assert snap["outcome"] == "repaired", snap["message"]
    assert len(snap["executions"]) == 8
    results = [
        json.loads(store.get_artifact(tid, a["result_artifact_id"])["content"])["result"]
        for a in snap["agent_actions"]
        if a["name"] == "run_check"
    ]
    assert len(results) == 4 and sum(r["cached"] for r in results) == 2
    assert len({r["id"] for r in results}) == 2


@pytest.mark.parametrize(
    "variant,limits,calls",
    [
        ("forever", None, 6),
        ("normal", {"max_model_calls": 2, "max_tokens": 250000}, 2),
        ("normal", {"max_model_calls": 32, "max_tokens": 8192}, 0),
    ],
)
def test_tool_loop_and_all_schemas_share_hard_model_budget(environment, variant, limits, calls):
    tid, claimed = task(limits=limits)
    provider = FunctionAPI(variant)
    snap = execute(tid, claimed, provider)
    assert snap["outcome"] == "budget_exhausted", snap["message"]
    assert snap["usage"]["calls"] == calls
    assert len(provider.calls) == calls
    assert not any(c["selected"] for c in snap["candidates"])
    assert all(a["status"] != "running" for a in snap["agent_actions"])
    assert len(snap["executions"]) == 2


def test_non_tool_answers_are_bounded_format_failures_and_never_archived_as_prose(environment):
    tid, claimed = task()
    snap = execute(tid, claimed, FunctionAPI("plain_text"))
    assert snap["lifecycle"] == "failed" and snap["error_code"] == "model_format_error"
    assert snap["usage"]["calls"] == 4
    assert not snap["agent_actions"] and not snap["candidates"]


def test_cancellation_inside_tool_check_closes_action_and_cleans_runner(environment):
    class WaitingRunner(InProcessRunner):
        def __init__(self):
            super().__init__()
            self.seen, self.waited = set(), False

        async def get(self, eid):
            self.seen.add(eid)
            if len(self.seen) == 3 and not self.waited:
                self.waited = True
                started.set()
                await asyncio.Event().wait()
            return await super().get(eid)

    tid, claimed = task()
    provider = FunctionAPI()

    async def flow():
        nonlocal started
        started = asyncio.Event()
        running = asyncio.create_task(
            run_claimed(
                tid,
                claimed,
                runner=WaitingRunner(),
                model=ModelClient(transport=httpx.MockTransport(provider)),
            )
        )
        await asyncio.wait_for(started.wait(), 15)
        store.cancel(tid, "tools-owner")
        await asyncio.wait_for(running, 5)

    started = None
    run(flow())
    snap = store.snapshot(tid)
    assert snap["lifecycle"] == "cancelled"
    assert snap["agent_actions"][-1]["status"] == "cancelled"
    assert all(j["lifecycle"] not in {"queued", "running"} for j in environment.jobs.values())
    assert len(provider.calls) == 3
