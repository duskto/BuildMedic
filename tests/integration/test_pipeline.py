"""The model is scripted ONLY in tests. Every pytest check is a real subprocess."""

import asyncio
import difflib
import hashlib
import json

import httpx
import pytest
import uvloop
from sqlalchemy import update

from buildmedic import db, evaluation, store
from buildmedic import runner as runner_module
from buildmedic.cases import metadata
from buildmedic.clients import ModelClient, RunnerClient
from buildmedic.config import settings
from buildmedic.orchestration import run_claimed, verification_ok
from buildmedic.runner import Engine
from buildmedic.schemas import CreateTask, Source
from buildmedic.worker import reap_expired


def run(coro):
    return asyncio.run(coro, loop_factory=uvloop.new_event_loop)


@pytest.mark.parametrize("invalid_first", [False, True])
def test_log_explanation_never_reads_source_or_calls_runner(monkeypatch, invalid_first):
    configured_model(monkeypatch)
    log = evaluation.captured_log("pagination")
    spec = evaluation.FourArmSpec(
        schema_version="2.0",
        name="log-isolation",
        cases=[
            evaluation.FourArmCase(
                case_id="pagination", snapshot_sha256=log.snapshot_sha256, log_sha256=log.log_sha256
            )
        ],
    )
    eid = evaluation.create("owner", "log-isolation", spec)
    tid = evaluation.advance(eid, "owner")["task_id"]

    class NoRunner:
        async def health(self):
            raise AssertionError("Log arm must not inspect Runner")

        async def start(self, _):
            raise AssertionError("Log arm must not execute code")

        async def close(self):
            pass

    class LogModel:
        calls = 0

        async def complete(self, messages):
            self.calls += 1
            material = json.loads(messages[1]["content"])["materials"]
            assert material["source"] == [] and len(material["evidence"]) == 1
            assert material["evidence"][0]["kind"] == "ci_log" and material["logs"] == log.log
            return json.dumps(
                {
                    "summary": "Test-only log hypothesis",
                    "failure_category": "test",
                    "root_cause": {
                        "description": "Observed in log",
                        "evidence_ids": [material["evidence"][0]["id"]],
                        "certainty": "supported" if invalid_first and self.calls == 1 else "hypothesis",
                    },
                    "next_action": "Review source in a separate task",
                }
            ), {"total_tokens": 100}

        async def close(self):
            pass

    model = LogModel()
    run(run_claimed(tid, store.claim(tid), runner=NoRunner(), model=model))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "diagnosed_only" and snap["reproduction_status"] == "not_run"
    assert model.calls == (2 if invalid_first else 1)
    assert snap["executions"] == snap["candidates"] == snap["validations"] == []
    assert all(e["kind"] == "ci_log" for e in snap["evidence"])
    assert "preflight" not in snap["phase_history"] and "reproduce" not in snap["phase_history"]


class InProcessRunner(RunnerClient):
    def __init__(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runner_module.app),
            base_url="http://runner",
            headers={"Authorization": "Bearer " + settings().runner_token},
        )


class TestProvider:
    __test__ = False

    async def complete(self, messages):
        materials = json.loads(messages[1]["content"])["materials"]
        patch = ""
        for source in materials["source"]:
            old = source["content"]
            new = (
                old.replace("start + page_size - 1", "start + page_size")
                .replace("from demo.utils import", "from demo.formatting import")
                .replace("return shipping_cost(weight)", "return shipping_cost(weight, region=region)")
            )
            if new != old:
                patch += "".join(
                    difflib.unified_diff(
                        old.splitlines(True),
                        new.splitlines(True),
                        "a/" + source["path"],
                        "b/" + source["path"],
                    )
                )
        return json.dumps(
            {
                "schema_version": "1.0",
                "failure_category": "test_fixture",
                "summary": "Scripted test diagnosis",
                "root_cause": {
                    "description": "Test transport exercises the real verification pipeline",
                    "evidence_ids": [materials["evidence"][0]["id"]],
                    "certainty": "supported",
                },
                "limitations": ["This is a scripted test provider, not a live model"],
                "next_action": "Inspect independent results",
                "patch": patch,
            }
        ), {"total_tokens": 1000}

    async def close(self):
        pass


@pytest.fixture
def local_engine(tmp_path, monkeypatch):
    engine = Engine(backend="trusted-local", data_dir=tmp_path / "runner")
    monkeypatch.setattr(runner_module, "_engine", engine)
    yield engine
    engine.pool.shutdown(wait=True)


def configured_model(monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "model_base_url", "https://test-provider.invalid/v1")
    monkeypatch.setattr(cfg, "model_api_key", "test-provider-only-not-a-real-secret")
    monkeypatch.setattr(cfg, "model_id", "scripted-test-provider")


def task(case_id="pagination", limits=None):
    body = CreateTask(source=Source(case_id=case_id), **({"limits": limits} if limits else {})).model_dump()
    tid = store.create("test-owner", store.uid(), body, metadata(case_id))
    return tid, store.claim(tid)


@pytest.mark.parametrize("case_id", ["pagination", "imports", "signature"])
def test_real_baseline_patch_and_independent_regression(case_id, local_engine, monkeypatch):
    configured_model(monkeypatch)
    tid, claimed = task(case_id)

    async def flow():
        await run_claimed(tid, claimed, runner=InProcessRunner(), model=TestProvider())

    run(flow())
    snap = store.snapshot(tid)
    assert (snap["lifecycle"], snap["outcome"], snap["reproduction_status"], snap["validation_status"]) == (
        "completed",
        "repaired",
        "reproduced",
        "passed",
    ), snap["message"]
    assert snap["baseline"][0]["exit_code"] != 0
    assert len(snap["model_calls"]) == 1
    assert snap["model_calls"][0]["status"] == "completed"
    assert snap["model_calls"][0]["total_tokens"] == 1000
    assert snap["model_calls"][0]["finished_at"] >= snap["model_calls"][0]["started_at"]
    assert len(snap["executions"]) == 6
    candidate = snap["candidates"][0]
    patch = store.get_artifact(tid, candidate["patch_artifact_id"])["content"]
    assert hashlib.sha256(patch.encode()).hexdigest() == candidate["patch_hash"]
    independent = next(v for v in snap["validations"] if v["kind"] == "independent")
    assert independent["regression"]["counts"]["passed"] > 0
    assert all(
        e["patch_hash"] == candidate["patch_hash"] for e in [independent["target"], independent["regression"]]
    )
    assert (
        next(v for v in snap["validations"] if v["kind"] == "candidate")["target"]["id"]
        != independent["target"]["id"]
    )
    report = next(a for a in snap["artifacts"] if a["kind"] == "report")
    archived = json.loads(store.get_artifact(tid, report["id"])["content"])
    assert (archived["lifecycle"], archived["outcome"]) == ("completed", "repaired")
    assert archived["phase_history"] == ["ingest", "preflight", "reproduce", "diagnose", "validate", "report"]
    assert store.claim(tid) is None


def test_revalidation_appends_history(local_engine, monkeypatch):
    configured_model(monkeypatch)
    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=TestProvider()))
    snap = store.snapshot(tid)
    candidate = snap["candidates"][0]
    first_ids = [v["id"] for v in snap["validations"]]
    vid = store.enqueue_validation(tid, "test-owner", candidate["id"])
    with pytest.raises(store.Conflict):
        store.enqueue_validation(tid, "test-owner", candidate["id"])
    job = {"candidate_id": candidate["id"], "validation_id": vid}
    run(
        run_claimed(
            tid, store.claim(tid, vid), runner=InProcessRunner(), model=TestProvider(), validation_job=job
        )
    )
    final = store.snapshot(tid)
    assert all(v in [r["id"] for r in final["validations"]] for v in first_ids)
    assert final["validations"][-1]["status"] == "passed"
    assert final["lifecycle"] == "completed"


def test_missing_model_keeps_real_reproduction(local_engine):
    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=TestProvider()))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "blocked"
    assert snap["reproduction_status"] == "reproduced"
    assert not snap["candidates"]
    assert snap["usage"]["calls"] == 0


@pytest.mark.parametrize(
    "case_id,outcome", [("missing-dependency", "blocked"), ("not-reproduced", "not_reproduced")]
)
def test_failure_paths(case_id, outcome, local_engine):
    tid, claimed = task(case_id)
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=TestProvider()))
    assert store.snapshot(tid)["outcome"] == outcome


def test_independent_failure_cannot_reuse_candidate_success(local_engine, monkeypatch):
    configured_model(monkeypatch)

    class ChangedRunner(InProcessRunner):
        patched_calls = 0

        async def submit(self, eid, case, check, patch):
            if patch:
                self.patched_calls += 1
                if self.patched_calls >= 3:
                    patch = ""  # Real unpatched failure during final independent checks.
            return await super().submit(eid, case, check, patch)

    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=ChangedRunner(), model=TestProvider()))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "unresolved"
    assert snap["validation_status"] == "failed"
    assert snap["validations"][0]["status"] == "passed"
    assert snap["validations"][-1]["status"] == "failed"


def test_cancel_during_model_call_stops_and_preserves_evidence(local_engine, monkeypatch):
    configured_model(monkeypatch)
    started = None

    class SlowProvider(TestProvider):
        async def complete(self, messages):
            started.set()
            await asyncio.Event().wait()

    tid, claimed = task()

    async def flow():
        nonlocal started
        started = asyncio.Event()
        job = asyncio.create_task(run_claimed(tid, claimed, runner=InProcessRunner(), model=SlowProvider()))
        await asyncio.wait_for(started.wait(), timeout=15)
        store.cancel(tid, "test-owner")
        await asyncio.wait_for(job, timeout=5)

    run(flow())
    snap = store.snapshot(tid)
    assert snap["lifecycle"] == "cancelled"
    assert snap["evidence"]
    assert snap["usage"]["usage_known"] is False
    assert snap["model_calls"][0]["status"] == "cancelled"
    assert all(r["status"] != "running" for r in snap["rollouts"])
    assert not any(j["lifecycle"] in {"queued", "running"} for j in local_engine.jobs.values())


def test_token_budget_stops_before_model_call(local_engine, monkeypatch):
    configured_model(monkeypatch)

    class NeverCalled(TestProvider):
        async def complete(self, messages):
            raise AssertionError("budget must be checked before model transport")

    tid, claimed = task(limits={"max_tokens": 8192})
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=NeverCalled()))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "budget_exhausted"
    assert snap["usage"]["calls"] == 0
    assert snap["model_calls"] == []


def test_format_retry_keeps_both_model_calls(local_engine, monkeypatch):
    configured_model(monkeypatch)

    class FirstMalformed(TestProvider):
        calls = 0

        async def complete(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '{"bad_format":true}', {"total_tokens": 12}
            return await super().complete(messages)

    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=FirstMalformed()))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "repaired"
    assert [c["status"] for c in snap["model_calls"]] == ["rejected", "completed"]
    assert snap["model_calls"][0]["error_code"] == "model_format_error"
    assert snap["usage"]["tokens"] == 1012


def test_exact_model_edits_pass_same_independent_checks(local_engine, monkeypatch):
    configured_model(monkeypatch)

    class ExactEdits(TestProvider):
        async def complete(self, messages):
            raw, usage = await super().complete(messages)
            data = json.loads(raw)
            data["patch"] = ""
            data["edits"] = [
                {
                    "path": "src/demo/pagination.py",
                    "before": "start + page_size - 1",
                    "after": "start + page_size",
                }
            ]
            return json.dumps(data), usage

    tid, claimed = task()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=ExactEdits()))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "repaired"
    assert snap["candidates"][0]["delivery_format"] == "exact_edits"
    assert len(snap["executions"]) == 6
    assert next(v for v in snap["validations"] if v["kind"] == "independent")["status"] == "passed"


def test_provider_balance_error_preserves_real_reproduction_and_ledger(local_engine, monkeypatch):
    configured_model(monkeypatch)
    tid, claimed = task()
    model = ModelClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(402, text="provider-private-body"))
    )
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=model))
    snap = store.snapshot(tid)
    assert snap["reproduction_status"] == "reproduced"
    assert (snap["lifecycle"], snap["error_code"]) == ("failed", "model_insufficient_balance")
    assert snap["model_calls"][0]["error_code"] == "model_insufficient_balance"
    assert not snap["candidates"]
    assert "provider-private-body" not in json.dumps(snap)


def test_expired_worker_is_fenced_and_marked_interrupted(monkeypatch):
    tid, claimed = task()
    old_token = claimed[0]
    with db.transaction() as conn:
        conn.execute(update(db.tasks).where(db.tasks.c.id == tid).values(heartbeat_at=0))
    run(reap_expired())
    assert store.snapshot(tid)["lifecycle"] == "interrupted"
    with pytest.raises(store.LostLease):
        store.finish(tid, old_token, "completed", "repaired")


def test_zero_tests_or_extra_skips_fail_verification():
    old = {
        "passed": False,
        "reason": "exited",
        "command": ["pytest"],
        "counts": {"collected": 1, "skipped": 0, "tests": [{"id": "test_original", "status": "failed"}]},
    }
    new = {**old, "passed": True, "counts": {"collected": 0, "skipped": 0, "tests": []}}
    assert verification_ok([old, old], new, new)[0] is False
    new["counts"] = {"collected": 1, "skipped": 1, "tests": [{"id": "test_original", "status": "skipped"}]}
    assert verification_ok([old, old], new, new)[0] is False
