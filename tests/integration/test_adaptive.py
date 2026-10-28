"""Native Harness² scheduling + scripted HTTP model + real pytest subprocess checks."""

import asyncio
import json

import httpx
import pytest
import uvloop

pytest.importorskip("harness2")

from harness2.core.proposer import Proposer

from buildmedic import evaluation, experiences, store
from buildmedic import runner as runner_module
from buildmedic.adaptive import BASE_DIR, BoundedSubstrate, pack_context, trees, validate_strategy
from buildmedic.cases import metadata
from buildmedic.clients import RunnerClient
from buildmedic.config import HARNESS_SHA, settings
from buildmedic.orchestration import run_claimed
from buildmedic.runner import Engine
from buildmedic.schemas import CreateTask, Source


def run(coro):
    return asyncio.run(coro, loop_factory=uvloop.new_event_loop)


class InProcessRunner(RunnerClient):
    def __init__(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runner_module.app),
            base_url="http://runner",
            headers={"Authorization": "Bearer " + settings().runner_token},
        )


class ScriptedAdaptiveModel:
    def __init__(self, fail_final=False):
        self.metadata = {"response_model": "scripted-harness-test"}
        self.calls = []
        self.solvers = 0
        self.fail_final = fail_final
        self.proposal_contexts = []

    async def complete(self, messages):
        body = json.loads(messages[1]["content"])
        if body.get("harness2_role") == "proposal":
            self.calls.append("proposal")
            self.proposal_contexts.append(
                body["context_files"]
                | {
                    path: body["context_files"][original]
                    for path, original in body["context_file_aliases"].items()
                }
            )
            return json.dumps(
                {
                    "files": {
                        "tool_descriptions/tool_guidance.md": "Cross-check all source call sites and boundary contracts. Stage: "
                        + body["label"]
                    },
                    "notes": "Explicit scripted strategy edit for testing native scheduling.",
                }
            ), {"total_tokens": 100}
        if body.get("harness2_role") == "reflection":
            self.calls.append("reflection")
            return json.dumps(
                {
                    "has_lesson": True,
                    "cue": "Boundary contract differs from slicing convention",
                    "lesson": "Compare the caller's boundary expectations with the container API contract.",
                    "edits": "Made boundary cross-checks explicit.",
                    "reason": "",
                    "evidence_ids": [body["evidence_ids"][0]],
                }
            ), {"total_tokens": 100}
        self.calls.append("diagnosis")
        self.solvers += 1
        source = body["materials"]
        return json.dumps(
            {
                "schema_version": "1.0",
                "summary": "Scripted adapter test",
                "failure_category": "test",
                "root_cause": {
                    "description": "Scripted transport, real test checks",
                    "evidence_ids": [source["evidence"][0]["id"]],
                    "certainty": "supported",
                },
                "limitations": ["This is a scripted model used only in tests"],
                "next_action": "Inspect independent evidence",
                "edits": [
                    {
                        "path": "src/demo/pagination.py",
                        "before": "start + page_size - 1",
                        "after": "start + page_size - 2"
                        if self.fail_final and self.solvers == 4
                        else "start + page_size",
                    }
                ],
            }
        ), {"total_tokens": 100}

    async def close(self):
        pass


@pytest.fixture
def adaptive_environment(tmp_path, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "adaptive_enabled", True)
    monkeypatch.setattr(cfg, "model_base_url", "https://test-provider.invalid/v1")
    monkeypatch.setattr(cfg, "model_api_key", "scripted-test-key")
    monkeypatch.setattr(cfg, "model_id", "scripted-harness-test")
    engine = Engine(backend="trusted-local", data_dir=tmp_path / "runner")
    monkeypatch.setattr(runner_module, "_engine", engine)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("The bounded bridge must never launch a native CLI channel")

    monkeypatch.setattr(Proposer, "_opencode", forbidden)
    monkeypatch.setattr(Proposer, "_claude_cli", forbidden)
    yield engine
    engine.pool.shutdown(wait=True)


def task(limits=None, *, experience=False):
    body = CreateTask(
        source=Source(case_id="pagination"),
        mode="adaptive",
        experience=experience,
        limits=limits or {"max_tokens": 100000, "max_model_calls": 12},
    ).model_dump()
    tid = store.create("owner", store.uid(), body, metadata("pagination"))
    return tid, store.claim(tid)


def test_http_context_encoding_preserves_every_file_without_repeating_bodies():
    body = "完整公开轨迹\n" * 1000
    files = {
        "rollouts/inv1.md": body,
        "pool/inv1/rollout.md": body,
        "harness/systemprompt.md": "Different strategy",
        "base_harness/systemprompt.md": "Base strategy",
    }
    packed = pack_context(files)
    restored = packed["context_files"] | {
        path: packed["context_files"][original] for path, original in packed["context_file_aliases"].items()
    }
    assert restored == files
    assert len(packed["context_file_aliases"]) == 1
    assert len(json.dumps(packed, ensure_ascii=False).encode()) < len(
        json.dumps(files, ensure_ascii=False).encode()
    )


def test_native_library_reflection_then_publish_and_reuse_frozen_version(adaptive_environment):
    model = ScriptedAdaptiveModel()
    tid, claimed = task(experience=True)
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=model))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "repaired", snap["message"]
    assert snap["experience"]["reflection_status"] == "available"
    assert model.calls.count("reflection") == 1
    assert snap["usage"]["calls"] == 8 and snap["usage"]["tokens"] == 800
    assert experiences.list_versions("owner")["total"] == 0  # No automatic sharing.
    version = experiences.publish(tid, "owner", "publish-native")
    next_id, next_claimed = task(experience=True)
    next_model = ScriptedAdaptiveModel()
    experiences.revoke(version["id"], "owner")  # Already frozen, so native context must still contain it.
    run(run_claimed(next_id, next_claimed, runner=InProcessRunner(), model=next_model))
    next_snap = store.snapshot(next_id)
    assert next_snap["outcome"] == "repaired", next_snap["message"]
    assert next_snap["experience"]["entries"][0]["id"] == version["id"]
    for context in next_model.proposal_contexts:
        assert any("library/" in path and path.endswith("EXPERIENCE.md") for path in context), list(context)
        assert any(version["id"] in path and path.endswith("systemprompt.md") for path in context)
    second = experiences.publish(next_id, "owner", "publish-next-native")
    assert second["version"] == 2
    assert experiences.get_version(second["id"], "owner")["content"]["parent_version_ids"] == [version["id"]]


@pytest.mark.parametrize("mode", ["none", "invalid", "failed_checks", "budget"])
def test_reflection_cannot_publish_missing_invalid_or_unverified_lesson(adaptive_environment, mode):
    class ReflectionModel(ScriptedAdaptiveModel):
        async def complete(self, messages):
            body = json.loads(messages[1]["content"])
            if body.get("harness2_role") == "reflection" and mode in {"none", "invalid"}:
                self.calls.append("reflection")
                return json.dumps(
                    {"has_lesson": False, "reason": "No supported reusable contrast"}
                    if mode == "none"
                    else {
                        "has_lesson": True,
                        "cue": "Unknown",
                        "lesson": "Unknown evidence",
                        "evidence_ids": ["ev-does-not-exist"],
                    }
                ), {"total_tokens": 10}
            return await super().complete(messages)

    limits = {"max_tokens": 100000, "max_model_calls": 7} if mode == "budget" else None
    tid, claimed = task(limits, experience=True)
    model = ReflectionModel(fail_final=mode == "failed_checks")
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=model))
    snap = store.snapshot(tid)
    assert snap["experience"]["draft"] is None
    if mode == "none":
        assert snap["outcome"] == "repaired" and snap["experience"]["reflection_status"] == "no_entry"
    elif mode == "invalid":
        assert snap["outcome"] == "repaired" and snap["experience"]["reflection_status"] == "rejected"
        assert model.calls.count("reflection") == 2
    elif mode == "failed_checks":
        assert snap["outcome"] == "unresolved" and snap["experience"]["reflection_status"] == "unverified"
    else:
        assert snap["outcome"] == "budget_exhausted"
        assert snap["experience"]["reflection_status"] == "interrupted"
        assert model.calls.count("reflection") == 0
    with pytest.raises(store.Conflict):
        experiences.publish(tid, "owner", "must-not-publish")


def test_cancel_during_native_reflection_prevents_sharing(adaptive_environment):
    class WaitingReflection(ScriptedAdaptiveModel):
        async def complete(self, messages):
            if json.loads(messages[1]["content"]).get("harness2_role") == "reflection":
                started.set()
                await asyncio.Event().wait()
            return await super().complete(messages)

    tid, claimed = task(experience=True)

    async def flow():
        nonlocal started
        started = asyncio.Event()
        running = asyncio.create_task(
            run_claimed(tid, claimed, runner=InProcessRunner(), model=WaitingReflection())
        )
        await asyncio.wait_for(started.wait(), 20)
        store.cancel(tid, "owner")
        await asyncio.wait_for(running, 5)

    started = None
    run(flow())
    assert store.snapshot(tid)["lifecycle"] == "cancelled"
    assert store.snapshot(tid)["experience"]["reflection_status"] == "cancelled"
    assert experiences.list_versions("owner")["total"] == 0


def test_native_schedule_edits_strategies_runs_four_rollouts_and_independently_accepts(adaptive_environment):
    model = ScriptedAdaptiveModel()
    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=model))
    snap = store.snapshot(tid)
    assert (snap["lifecycle"], snap["outcome"]) == ("completed", "repaired"), snap["message"]
    assert snap["harness2_sha"] == HARNESS_SHA
    result = store.get_artifact(tid, snap["harness"]["result_artifact_id"])
    framework_report = json.loads(result["content"])
    assert framework_report["harness2_sha"] == HARNESS_SHA
    ledger = framework_report["modes"]["seq"]["ledger"]
    assert ledger["R"] == 4 and ledger["P"] == 3
    assert ledger["usd"] is ledger["rollout_usd"] is ledger["session_usd"] is None
    assert snap["rollouts"][0]["label"] == "base1" and snap["rollouts"][-1]["label"] == "c1"
    assert sorted(r["label"] for r in snap["rollouts"]) == ["base1", "c1", "dom1", "inv1"]
    assert len(snap["executions"]) == 12
    assert len(snap["candidates"]) == 4
    assert model.calls.count("diagnosis") == 4 and model.calls.count("proposal") == 3
    assert snap["usage"]["calls"] == 7 and snap["usage"]["tokens"] == 700
    assert snap["harness"]["selected_label"] == "c1"
    assert len(snap["harness"]["stages"]) == 3
    assert len({r["harness_hash"] for r in snap["rollouts"]}) > 1
    assert all(p["changed"] for p in snap["harness"]["proposals"])
    chosen = next(c for c in snap["candidates"] if c["selected"])
    assert chosen["rollout_id"] == snap["rollouts"][-1]["id"]
    independent = next(v for v in snap["validations"] if v["kind"] == "independent")
    assert independent["candidate_id"] == chosen["id"] and independent["status"] == "passed"
    report = next(a for a in snap["artifacts"] if a["kind"] == "report")
    assert json.loads(store.get_artifact(tid, report["id"])["content"])["harness2_sha"] == HARNESS_SHA


def test_valid_delivery_does_not_mean_repaired_and_no_base_fallback(adaptive_environment):
    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=ScriptedAdaptiveModel(fail_final=True)))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "unresolved"
    assert snap["harness"]["upstream_status"] == "ok"
    assert snap["rollouts"][0]["status"] == "passed"
    assert snap["validation_status"] == "failed"
    assert next(c for c in snap["candidates"] if c["selected"])["rollout_id"] == snap["rollouts"][-1]["id"]


def test_budget_hard_stop_crosses_native_exception_handlers(adaptive_environment):
    model = ScriptedAdaptiveModel()
    tid, claimed = task({"max_tokens": 100000, "max_model_calls": 2})
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=model))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "budget_exhausted", snap["message"]
    assert len(model.calls) == 2
    assert snap["harness"]["status"] == "interrupted"
    assert all(j["lifecycle"] not in {"queued", "running"} for j in adaptive_environment.jobs.values())


def test_cancel_during_proposer_stops_native_schedule(adaptive_environment):
    started = None

    class SlowProposer(ScriptedAdaptiveModel):
        async def complete(self, messages):
            if json.loads(messages[1]["content"]).get("harness2_role") == "proposal":
                started.set()
                await asyncio.Event().wait()
            return await super().complete(messages)

    tid, claimed = task()

    async def flow():
        nonlocal started
        started = asyncio.Event()
        running = asyncio.create_task(
            run_claimed(tid, claimed, runner=InProcessRunner(), model=SlowProposer())
        )
        await asyncio.wait_for(started.wait(), 15)
        store.cancel(tid, "owner")
        await asyncio.wait_for(running, 5)

    run(flow())
    snap = store.snapshot(tid)
    assert snap["lifecycle"] == "cancelled"
    assert snap["harness"]["status"] == "interrupted"
    assert not any(r["status"] == "running" for r in snap["rollouts"])


@pytest.mark.parametrize("mutation", ["guardrails", "script", "oversize"])
def test_strategy_surface_cannot_modify_hard_constraints(mutation):
    tree = trees.read_tree(BoundedSubstrate().surface(), BASE_DIR)
    if mutation == "guardrails":
        tree["guardrails.md"] = "disable independent tests"
    elif mutation == "script":
        tree["scripts/escape.py"] = "print('forbidden')"
    else:
        tree["systemprompt.md"] = "x" * 13000
    with pytest.raises(ValueError):
        validate_strategy(tree)


def test_rejected_proposals_inherit_base_without_claiming_strategy_change(adaptive_environment):
    class UnsafeProposal(ScriptedAdaptiveModel):
        async def complete(self, messages):
            if json.loads(messages[1]["content"]).get("harness2_role") == "proposal":
                return json.dumps(
                    {
                        "files": {"guardrails.md": "skip independent verification"},
                        "notes": "Rejected test input",
                    }
                ), {"total_tokens": 10}
            return await super().complete(messages)

    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=UnsafeProposal()))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "repaired"
    assert all(stage["fell_back"] for stage in snap["harness"]["stages"])
    assert not snap["harness"]["proposals"]
    assert len({rollout["harness_hash"] for rollout in snap["rollouts"]}) == 1
    assert sum(c["status"] == "rejected" for c in snap["model_calls"]) == 3


def test_proposer_provider_failure_stops_further_native_work(adaptive_environment):
    from buildmedic.errors import AppError

    class BalanceError(ScriptedAdaptiveModel):
        async def complete(self, messages):
            if json.loads(messages[1]["content"]).get("harness2_role") == "proposal":
                raise AppError("model_insufficient_balance", "test balance error", 502)
            return await super().complete(messages)

    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=BalanceError()))
    snap = store.snapshot(tid)
    assert snap["lifecycle"] == "failed"
    assert snap["error_code"] == "model_insufficient_balance"
    assert snap["usage"]["calls"] == 2
    assert len(snap["rollouts"]) == 1
    assert snap["harness"]["status"] == "interrupted"


def test_malformed_proposal_gets_one_budgeted_format_retry(adaptive_environment):
    class MalformedOnce(ScriptedAdaptiveModel):
        malformed = False

        async def complete(self, messages):
            if json.loads(messages[1]["content"]).get("harness2_role") == "proposal" and not self.malformed:
                self.malformed = True
                return '{"files":{} trailing', {"total_tokens": 9}
            return await super().complete(messages)

    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=MalformedOnce()))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "repaired"
    assert snap["usage"]["calls"] == 8
    assert sum(c["status"] == "rejected" for c in snap["model_calls"]) == 1
    assert any(c["label"].endswith(".format-retry") for c in snap["model_calls"])
    assert any(stage["attempts"] == 2 for stage in snap["harness"]["stages"])


def test_adapter_satisfies_the_pinned_runtime_protocol(adaptive_environment, tmp_path):
    from types import SimpleNamespace

    from harness2.core.spec import BenchmarkAdapter, ChannelSpec

    from buildmedic.adaptive import CIRepairAdapter

    adapter = CIRepairAdapter(
        SimpleNamespace(id="compatibility-task"), None, tmp_path / "compat", "pagination", {}, []
    )
    assert isinstance(adapter, BenchmarkAdapter)
    assert isinstance(adapter.spec.channels["proposer"], ChannelSpec)
    assert adapter.spec.channels["proposer"].kind == "buildmedic_http"
    assert adapter.substrate.name == "bounded-http"
    assert adapter.substrate.surface().name == "opencode"  # layout, not an executable channel


def test_preregistered_comparison_freezes_real_execution_and_recomputes(adaptive_environment):
    from sqlalchemy import update

    from buildmedic import db

    spec = evaluation.ExperimentSpec(
        name="scripted-real-pytest-comparison",
        cases=[
            evaluation.EvaluationCase(
                case_id="pagination", snapshot_sha256=metadata("pagination")["snapshot_sha256"]
            )
        ],
    )
    eid = evaluation.create("owner", "development-comparison", spec)
    ids = []
    while True:
        item = evaluation.advance(eid, "owner")
        if not item["task_id"]:
            break
        tid = item["task_id"]
        ids.append(tid)
        run(run_claimed(tid, store.claim(tid), runner=InProcessRunner(), model=ScriptedAdaptiveModel()))
    report = evaluation.get(eid, "owner")
    assert report["status"] == "completed" and report["finished"] == report["planned"] == 2
    assert [s["public_verified"] for s in report["summary"]] == [1, 1]
    assert [s["calls"] for s in report["summary"]] == [1, 7]
    assert [s["checks"] for s in report["summary"]] == [6, 12]
    assert all(s["cost_usd"] is None and s["cost_per_public_verified"] is None for s in report["summary"])
    assert report["environment_consistent"] is True
    assert report == evaluation.get(eid, "owner")
    exported = evaluation.export_report(eid, "owner")
    assert evaluation.recompute(exported) == report
    exported["summary"][0]["public_verified"] = 999
    with pytest.raises(store.Conflict, match="复算"):
        evaluation.recompute(exported)
    # Later task mutations/revalidation cannot rewrite this comparison's frozen end report.
    snap = store.snapshot(ids[0])
    snap["usage"]["tokens"] += 99999
    with db.transaction() as conn:
        conn.execute(update(db.tasks).where(db.tasks.c.id == ids[0]).values(snapshot=snap))
    assert report == evaluation.get(eid, "owner")
    assert all(
        row["report"]["artifact_id"] and row["metrics"]["hidden_verified"] is None for row in report["rows"]
    )


def test_preregistered_task_stops_before_paid_call_after_config_change(adaptive_environment, monkeypatch):
    spec = evaluation.ExperimentSpec(
        name="configuration-drift",
        cases=[
            evaluation.EvaluationCase(
                case_id="pagination", snapshot_sha256=metadata("pagination")["snapshot_sha256"]
            )
        ],
    )
    eid = evaluation.create("owner", "development-drift", spec)
    tid = evaluation.advance(eid, "owner")["task_id"]
    monkeypatch.setattr(settings(), "max_output_tokens", 100)
    model = ScriptedAdaptiveModel()
    run(run_claimed(tid, store.claim(tid), runner=InProcessRunner(), model=model))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "blocked" and model.calls == [] and snap["executions"] == []


def test_four_arm_independent_attempts_do_not_share_feedback_or_stop_at_first_success(adaptive_environment):
    log = evaluation.captured_log("pagination")
    spec = evaluation.FourArmSpec(
        schema_version="2.0",
        name="four-arm-scripted",
        cases=[
            evaluation.FourArmCase(
                case_id="pagination", snapshot_sha256=log.snapshot_sha256, log_sha256=log.log_sha256
            )
        ],
    )
    eid = evaluation.create("owner", "four-arm-scripted", spec)
    material_sets = {}

    class Provider(ScriptedAdaptiveModel):
        def __init__(self, arm):
            super().__init__()
            self.arm = arm

        async def complete(self, messages):
            body = json.loads(messages[1]["content"])
            if "materials" in body:
                materials = body["materials"]
                material_sets.setdefault(self.arm, []).append(materials)
                if self.arm == "logs":
                    assert materials["source"] == []
                    return json.dumps(
                        {
                            "summary": "Explicit log-only test",
                            "failure_category": "test",
                            "root_cause": {
                                "description": "Log hypothesis",
                                "evidence_ids": [materials["evidence"][0]["id"]],
                                "certainty": "hypothesis",
                            },
                            "next_action": "Inspect repository separately",
                            "patch": "",
                        }
                    ), {"total_tokens": 100}
            raw, usage = await super().complete(messages)
            if self.arm in {"standard", "multi"} and self.solvers == 1:
                data = json.loads(raw)
                data["edits"][0]["after"] = "start + page_size - 2"
                return json.dumps(data), usage
            return raw, usage

    while True:
        item = evaluation.advance(eid, "owner")
        if not item["task_id"]:
            break
        tid = item["task_id"]
        arm = store.snapshot(tid)["experiment_binding"]["arm"]
        run(run_claimed(tid, store.claim(tid), runner=InProcessRunner(), model=Provider(arm)))
    report = evaluation.get(eid, "owner")
    assert report["status"] == "completed" and report["finished"] == 4
    assert [s["calls"] for s in report["summary"]] == [1, 1, 4, 7]
    assert [s["checks"] for s in report["summary"]] == [0, 4, 12, 12]
    assert [s["public_verified"] for s in report["summary"]] == [0, 0, 1, 1]
    assert report["summary"][0]["diagnoses"] == 1 and report["summary"][0]["diagnosis_accuracy"] is None
    assert all(m == material_sets["multi"][0] for m in material_sets["multi"])
    assert all("previous_patch" not in m and "validation_feedback" not in m for m in material_sets["multi"])
    multi = store.snapshot(report["rows"][2]["task_id"])
    assert len(multi["rollouts"]) == len(multi["candidates"]) == 4
    assert len({r["input_sha256"] for r in multi["rollouts"]}) == 1
    assert next(c for c in multi["candidates"] if c["selected"])["id"] == multi["candidates"][1]["id"]
    assert evaluation.recompute(evaluation.export_report(eid, "owner")) == report
