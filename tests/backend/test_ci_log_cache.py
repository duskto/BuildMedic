import asyncio
import copy

import pytest
from sqlalchemy import select, update

from buildmedic import ci_logs, db, store
from buildmedic.config import settings
from buildmedic.errors import AppError


def preview(**changes):
    return {
        "repo": "Owner/Repo",
        "run_id": 42,
        "run_attempt": 2,
        "head_sha": "a" * 40,
        "jobs": [{"id": 7, "status": "completed"}],
        **changes,
    }


class GitHub:
    def __init__(self, content="TypeError: synthetic failure\napi_key=synthetic-private-value"):
        self.content = content
        self.calls = 0

    async def logs(self, repo, job_id):
        self.calls += 1
        return self.content


def read(github, owner="owner", source=None, job=7):
    return asyncio.run(ci_logs.read_job_log(github, owner, source or preview(), job))


def entries():
    with db.transaction() as conn:
        return list(conn.execute(select(db.ci_log_cache)).mappings())


def test_cache_is_sanitized_bound_and_does_not_extend_expiry(monkeypatch):
    monkeypatch.setattr(db, "now", lambda: 1000)
    github = GitHub()
    text, first = read(github)
    monkeypatch.setattr(db, "now", lambda: 1100)
    again, cached = read(github, source=preview(repo="owner/repo", head_sha="A" * 40))
    assert github.calls == 1 and text == again
    assert "synthetic-private-value" not in text
    assert first["source"] == "network" and cached["source"] == "cache"
    assert cached["sha256"] == ci_logs.digest(text)
    assert cached["cached_at"] == 1000 and cached["expires_at"] == 1000 + ci_logs.CACHE_TTL
    assert cached["binding"] == {
        "repo": "owner/repo",
        "head_sha": "a" * 40,
        "run_id": 42,
        "run_attempt": 2,
        "job_id": 7,
    }
    assert len(entries()) == 1
    assert "owner_id" not in cached and "key" not in cached


@pytest.mark.parametrize("change", ["owner", "repo", "run_id", "run_attempt", "head_sha", "job_id"])
def test_cache_does_not_cross_session_or_frozen_source_identity(change):
    github = GitHub()
    read(github)
    owner, job, source = "owner", 7, preview()
    if change == "owner":
        owner = "another-owner"
    elif change == "job_id":
        job = 8
        source["jobs"] = [{"id": 8, "status": "completed"}]
    else:
        source[change] = {"repo": "other/repo", "run_id": 43, "run_attempt": 3, "head_sha": "b" * 40}[change]
    _, origin = read(github, owner, source, job)
    assert github.calls == 2 and origin["source"] == "network" and len(entries()) == 2


@pytest.mark.parametrize("reason", ["disabled", "running", "unknown", "blank", "too_large"])
def test_unfinished_disabled_empty_and_oversized_logs_are_not_cached(reason, monkeypatch):
    github, source = GitHub(), preview()
    if reason == "disabled":
        monkeypatch.setattr(settings(), "github_log_cache_enabled", False)
    if reason in {"running", "unknown"}:
        source["jobs"][0]["status"] = "in_progress" if reason == "running" else None
    if reason == "blank":
        github.content = " \n"
    if reason == "too_large":
        monkeypatch.setattr(ci_logs, "MAX_LOG_BYTES", 10)
    read(github, source=source)
    read(github, source=source)
    assert github.calls == 2 and entries() == []


@pytest.mark.parametrize("damage", ["content", "sha256", "size_bytes", "binding", "expired", "new_secret"])
def test_invalid_or_stale_cache_is_discarded_and_refetched(damage, monkeypatch):
    monkeypatch.setattr(db, "now", lambda: 1000)
    github = GitHub("TypeError: synthetic failure\nnewly-configured-token")
    read(github)
    values = {
        "content": {"content": "corrupted"},
        "sha256": {"sha256": "0" * 64},
        "size_bytes": {"size_bytes": 1},
        "binding": {"binding": {"repo": "other/repo"}},
        "expired": {"expires_at": 999},
        "new_secret": {},
    }[damage]
    if damage == "new_secret":
        monkeypatch.setattr(settings(), "github_token", "newly-configured-token")
    else:
        with db.transaction() as conn:
            conn.execute(update(db.ci_log_cache).values(**values))
    text, origin = read(github)
    assert github.calls == 2 and origin["source"] == "network" and len(entries()) == 1
    assert entries()[0]["content"] == text
    if damage == "new_secret":
        assert "newly-configured-token" not in text


def test_write_prunes_expired_owner_global_and_byte_limits(monkeypatch):
    monkeypatch.setattr(ci_logs, "MAX_OWNER_ENTRIES", 2)
    monkeypatch.setattr(ci_logs, "MAX_ENTRIES", 3)
    monkeypatch.setattr(ci_logs, "MAX_BYTES", 30)
    github = GitHub("1234567890")
    for i, owner in enumerate(["a", "a", "a", "b", "c"]):
        monkeypatch.setattr(db, "now", lambda i=i: 1000 + i)
        read(github, owner, preview(run_id=i + 1))
        saved = entries()
        assert sum(e["size_bytes"] for e in saved) <= 30
        assert len(saved) <= 3
        assert sum(e["owner_id"] == owner for e in saved) <= 2
    assert sorted(e["binding"]["run_id"] for e in entries()) == [3, 4, 5]
    monkeypatch.setattr(db, "now", lambda: 1000 + ci_logs.CACHE_TTL + 10)
    read(github, "d")
    assert len(entries()) == 1 and entries()[0]["owner_id"] == "d"


def test_concurrent_different_downloads_do_not_overwrite_cached_identity():
    async def flow():
        both_started = asyncio.Event()

        class Racing(GitHub):
            async def logs(self, *_):
                self.calls += 1
                n = self.calls
                if n == 2:
                    both_started.set()
                await both_started.wait()
                return f"different log {n}"

        github = Racing()
        results = await asyncio.gather(
            ci_logs.read_job_log(github, "owner", preview(), 7),
            ci_logs.read_job_log(github, "owner", preview(), 7),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, AppError)]
        assert len(errors) == 1 and errors[0].code == "github_log_changed"
        cached = await ci_logs.read_job_log(github, "owner", preview(), 7)
        assert cached[1]["source"] == "cache" and github.calls == 2

    asyncio.run(flow())
    assert len(entries()) == 1


def test_worker_revalidates_before_cache_and_archives_origin(monkeypatch):
    from buildmedic.orchestration import run_claimed
    from buildmedic.schemas import CreateTask, Source

    source = preview()
    source["profile_version"] = None
    source["execution_label"] = "Synthetic completed GitHub Job"
    source["title"] = "Synthetic CI cache"
    counts = {"preview": 0, "logs": 0}
    drift = False

    class WorkerGitHub(GitHub):
        async def preview(self, *_):
            counts["preview"] += 1
            result = copy.deepcopy(source)
            if drift:
                result["head_sha"] = "b" * 40
            return result

        async def logs(self, *_):
            counts["logs"] += 1
            return "TypeError: synthetic failure"

        async def close(self):
            pass

    monkeypatch.setattr("buildmedic.orchestration.GitHubClient", WorkerGitHub)
    body = CreateTask(
        source=Source(
            type="github_actions",
            run_url="https://github.com/Owner/Repo/actions/runs/42",
            run_attempt=2,
            job_id=7,
        )
    ).model_dump()
    for i in range(3):
        drift = i == 2
        tid = store.create("owner", f"cache-{i}", body, source)
        asyncio.run(run_claimed(tid, store.claim(tid)))
        snap = store.snapshot(tid)
        # No model/Runner configured: stop after real ingestion and evidence archiving.
        assert snap["outcome"] == "blocked" and snap["executions"] == []
        if i == 2:
            assert snap["evidence"] == []
        else:
            origin = snap["evidence"][0]["log_origin"]
            assert origin["source"] == ("cache" if i else "network")
            archive = store.get_artifact(tid, origin["artifact_id"])
            assert archive["kind"] == "ci_log_origin"
            assert origin["sha256"] == snap["evidence"][0]["log_index"]["sha256"]
    assert counts == {"preview": 3, "logs": 1}


def test_0004_upgrade_preserves_tasks_and_artifacts():
    from alembic import command

    from buildmedic.cases import metadata
    from buildmedic.database import migration_config, require_schema, upgrade_database
    from buildmedic.schemas import CreateTask, Source

    tid = store.create(
        "owner",
        "pre-cache",
        CreateTask(source=Source(case_id="pagination")).model_dump(),
        metadata("pagination"),
    )
    token = store.claim(tid)[0]
    artifact = store.artifact(tid, "ci_log", "historic log", token)
    before = store.snapshot(tid)
    with db.engine.begin() as conn:
        command.downgrade(migration_config(conn), "0004")
    upgrade_database()
    require_schema()
    assert store.snapshot(tid) == before
    assert store.get_artifact(tid, artifact["id"])["content"] == "historic log"
    assert entries() == []
