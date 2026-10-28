"""Synthetic GitHub transport + real archived source + real isolated Docker pytest.

No remote CI or live-model success claim: those require a user-provided failed Run.
"""

import asyncio
import json
import os

import httpx
import pytest
import uvloop

from buildmedic import runner as runner_module
from buildmedic import store
from buildmedic.clients import RunnerClient
from buildmedic.config import settings
from buildmedic.orchestration import run_claimed
from buildmedic.profiles import Registry
from buildmedic.runner import Engine
from buildmedic.schemas import CreateTask, Source

pytestmark = pytest.mark.skipif(
    not os.environ.get("BUILDMEDIC_TEST_DOCKER_IMAGE"), reason="Explicit Docker image required"
)


def run(coro):
    return asyncio.run(coro, loop_factory=uvloop.new_event_loop)


class InProcessRunner(RunnerClient):
    def __init__(self):
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runner_module.app),
            base_url="http://runner",
            headers={"Authorization": "Bearer " + settings().runner_token},
        )


class ScriptedModel:
    def __init__(self):
        self.metadata = {"response_model": "scripted-remote-integration"}
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        data = json.loads(messages[1]["content"])
        if data.get("harness2_role") == "proposal":
            return json.dumps({"files": {}, "notes": "Keep the base in this explicit scripted test."}), {
                "total_tokens": 100
            }
        materials = data["materials"]
        source = next(e for e in materials["evidence"] if e.get("path") == "src/demo/pagination.py")
        return json.dumps(
            {
                "summary": "Scripted transport; verify the archived boundary source with Docker.",
                "failure_category": "boundary",
                "root_cause": {
                    "description": "Slice contract mismatch",
                    "certainty": "supported",
                    "evidence_ids": [source["id"]],
                },
                "limitations": ["Synthetic GitHub metadata and scripted model; not live acceptance"],
                "next_action": "Inspect independent results",
                "edits": [
                    {
                        "path": "src/demo/pagination.py",
                        "before": "start + page_size - 1",
                        "after": "start + page_size",
                    }
                ],
            }
        ), {"total_tokens": 100}

    async def close(self):
        pass

    async def tool_turn(self, messages, tools):
        self.calls += 1
        initial = json.loads(messages[1]["content"])
        step = 1 + sum(m["role"] == "assistant" for m in messages)
        source = next(s for s in initial["source_index"] if s["path"].endswith("pagination.py"))
        outputs = [json.loads(m["content"])["result"] for m in messages if m["role"] == "tool"]
        if step == 1:
            name, args = "read_repo_file", {"path": source["path"]}
        elif step == 2:
            name, args = (
                "propose_patch",
                {
                    "explanation": "Scripted remote tool candidate",
                    "edits": [
                        {
                            "path": source["path"],
                            "before": "start + page_size - 1",
                            "after": "start + page_size",
                        }
                    ],
                },
            )
        elif step == 3:
            name, args = "run_check", {"candidate_id": outputs[1]["id"], "check_id": "target"}
        else:
            name, args = (
                "submit_diagnosis",
                {
                    "candidate_id": outputs[1]["id"],
                    "diagnosis": {
                        "summary": "Scripted remote function calling",
                        "failure_category": "boundary",
                        "root_cause": {
                            "description": "Slice contract mismatch",
                            "certainty": "supported",
                            "evidence_ids": outputs[0]["evidence_ids"],
                        },
                        "next_action": "Inspect independent results",
                    },
                },
            )
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{initial['rollout_id']}_{step}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        }, {"total_tokens": 100}


@pytest.fixture
def environment(registered_remote, tmp_path, monkeypatch):
    p, archive = registered_remote
    engine = Engine(backend="docker", image=p.runtime_digest, data_dir=tmp_path / "runner")
    monkeypatch.setattr(runner_module, "_engine", engine)
    cfg = settings()
    monkeypatch.setattr(cfg, "model_id", "scripted-remote-integration")
    monkeypatch.setattr(cfg, "model_api_key", "scripted-remote-key")
    monkeypatch.setattr(cfg, "model_base_url", "https://scripted.invalid")
    monkeypatch.setattr(cfg, "adaptive_enabled", True)
    preview = {
        "repo": p.repository,
        "run_id": 42,
        "run_attempt": 2,
        "head_sha": p.head_sha,
        "head_repository": p.repository,
        "workflow_path": p.workflow_path,
        "jobs": [{"id": 17, "name": p.job_name}],
        "profile_version": p.id,
    }

    class GitHub:
        async def preview(self, *_args):
            return preview

        async def logs(self, *_args):
            return "Synthetic CI fixture: FAILED tests/test_pagination.py::test_pages AssertionError"

        async def close(self):
            pass

    monkeypatch.setattr("buildmedic.orchestration.GitHubClient", GitHub)
    run(
        engine.snapshots.prepare(
            p.binding(), httpx.MockTransport(lambda _: httpx.Response(200, content=archive))
        )
    )
    yield engine, p, preview, GitHub
    engine.pool.shutdown(wait=True)


def create(profile, mode="standard", agent_tools=False):
    body = CreateTask(
        source=Source(
            type="github_actions",
            run_url=f"https://github.com/{profile.repository}/actions/runs/42?attempt=2",
            job_id=17,
            run_attempt=2,
        ),
        mode=mode,
        agent_tools=agent_tools,
        limits={
            "max_model_calls": 32 if agent_tools else 12,
            "max_tokens": 250000 if agent_tools else 100000,
        },
    ).model_dump()
    meta = {
        "repo": profile.repository,
        "run_id": 42,
        "run_attempt": 2,
        "job_id": 17,
        "head_sha": profile.head_sha,
        "profile_version": profile.id,
        "runner_profile": profile.binding(),
        "source_type": "github_actions",
        "case_id": None,
        "title": "Explicit synthetic remote test",
        "snapshot_sha256": profile.snapshot_sha256,
        "execution_label": "Synthetic GitHub transport with real Docker verification",
    }
    tid = store.create("owner", store.uid(), body, meta)
    return tid, store.claim(tid)


@pytest.mark.parametrize("mode", ["standard", "adaptive"])
@pytest.mark.parametrize("agent_tools", [False, True])
def test_registered_fixed_sha_repair_and_independent_verification(environment, mode, agent_tools):
    if mode == "adaptive":
        pytest.importorskip("harness2")
    engine, p, _, _ = environment
    tid, claimed = create(p, mode, agent_tools)
    model = ScriptedModel()
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=model))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "repaired", snap["message"]
    expected = (19 if mode == "adaptive" else 4) if agent_tools else (7 if mode == "adaptive" else 1)
    assert model.calls == expected
    assert snap["source_snapshot"]["snapshot_sha256"] == p.snapshot_sha256
    archived_profile = json.loads(
        store.get_artifact(tid, snap["source_snapshot"]["profile_artifact_id"])["content"]
    )
    assert archived_profile == p.model_dump()
    assert any(e["kind"] == "workflow" for e in snap["evidence"])
    checks = (16 if mode == "adaptive" else 7) if agent_tools else (12 if mode == "adaptive" else 6)
    assert len(snap["executions"]) == checks
    for execution in snap["executions"]:
        assert execution["head_sha"] == p.head_sha and execution["profile_hash"] == p.digest
        assert execution["image_digest"] == p.runtime_digest and execution["isolation"] == "docker"
    candidate = next(c for c in snap["candidates"] if c["selected"])
    independent = next(v for v in snap["validations"] if v["kind"] == "independent")
    earlier = next(
        v for v in snap["validations"] if v["kind"] == "candidate" and v["candidate_id"] == candidate["id"]
    )
    assert independent["status"] == "passed" and independent["target"]["id"] != earlier["target"]["id"]
    assert independent["regression"]["id"] != earlier["regression"]["id"]
    assert not list(settings().data_dir.glob("bm-materials-*"))
    vid = store.enqueue_validation(tid, "owner", candidate["id"])
    run(
        run_claimed(
            tid,
            store.claim(tid, vid),
            runner=InProcessRunner(),
            model=ScriptedModel(),
            validation_job={"validation_id": vid, "candidate_id": candidate["id"]},
        )
    )
    snap = store.snapshot(tid)
    assert snap["validations"][-1]["kind"] == "revalidation" and snap["validations"][-1]["status"] == "passed"
    assert snap["outcome"] == "repaired"
    assert all(job["lifecycle"] == "completed" for job in engine.jobs.values())


@pytest.mark.parametrize("change", ["workflow", "profile", "ci_fingerprint", "cache"])
def test_remote_mismatches_stop_before_model_or_code(environment, change, monkeypatch):
    engine, p, preview, github = environment
    tid, claimed = create(p)
    model = ScriptedModel()
    if change == "workflow":
        preview["workflow_path"] = ".github/workflows/other.yml"
    elif change == "profile":
        changed = p.model_copy(update={"target": "tests/test_other.py"})
        settings().runner_profiles.write_text(Registry(profiles=[changed]).model_dump_json())
    elif change == "ci_fingerprint":

        async def logs(*_):
            return "Synthetic CI fixture: ConnectionError, unrelated to the registered failure"

        monkeypatch.setattr(github, "logs", logs)
    else:
        (engine.snapshots.path(p.binding()) / "repo/src/demo/pagination.py").write_text("corrupted")
    run(run_claimed(tid, claimed, runner=InProcessRunner(), model=model))
    snap = store.snapshot(tid)
    assert snap["outcome"] in {"blocked", "not_reproduced"}, snap["message"]
    assert model.calls == 0 and not snap["executions"]
    assert not list(settings().data_dir.glob("bm-materials-*"))


def test_runner_evidence_with_wrong_commit_cannot_reach_model_or_repaired(environment):
    _, p, _, _ = environment
    tid, claimed = create(p)

    class WrongCommit(InProcessRunner):
        async def get(self, eid):
            result = await super().get(eid)
            if result["lifecycle"] == "completed":
                result["head_sha"] = "e" * 40
            return result

    model = ScriptedModel()
    run(run_claimed(tid, claimed, runner=WrongCommit(), model=model))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "blocked", snap["message"]
    assert model.calls == 0
    assert len(snap["executions"]) == 1
    assert not snap["candidates"]


def test_cancel_while_preparing_remote_snapshot_stops_without_execution(environment):
    engine, p, _, _ = environment
    tid, claimed = create(p)

    class WaitingPrepare(InProcessRunner):
        async def prepare(self, binding):
            started.set()
            await asyncio.Event().wait()

    async def flow():
        nonlocal started
        started = asyncio.Event()
        running = asyncio.create_task(
            run_claimed(tid, claimed, runner=WaitingPrepare(), model=ScriptedModel())
        )
        await asyncio.wait_for(started.wait(), 5)
        store.cancel(tid, "owner")
        await asyncio.wait_for(running, 5)

    started = None
    run(flow())
    assert store.snapshot(tid)["lifecycle"] == "cancelled"
    assert not engine.jobs
    assert not list(settings().data_dir.glob("bm-materials-*"))
