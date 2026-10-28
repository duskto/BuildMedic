import asyncio
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from buildmedic import db, store
from buildmedic.api import app
from buildmedic.cases import metadata
from buildmedic.config import settings
from buildmedic.errors import AppError
from buildmedic.logs import context_log, index_log, log_window
from buildmedic.orchestration import Context, run_claimed
from buildmedic.schemas import CreateTask, Source
from buildmedic.security import sanitize


def artifact(text):
    return {
        "id": "test-artifact",
        "kind": "ci_log",
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "content": text,
    }


@pytest.mark.parametrize("text", ["", "short log", "first\r\n第二行\nlast\n"])
def test_short_logs_remain_verbatim_with_original_line_identity(text):
    selected = context_log(text)
    assert selected["text"] == text
    assert selected["selection"]["truncated"] is False
    assert selected["selection"]["omitted_ranges"] == []
    assert selected["selection"]["sha256"] == artifact(text)["sha256"]
    assert selected["selection"]["total_lines"] == len(text.splitlines())


def test_index_keeps_step_and_timestamp_and_does_not_merge_different_steps():
    text = sanitize(
        "2026-10-28T01:00:00.000Z ##[group]Run pytest\n2026-10-28T01:00:01Z \x1b[31mAssertionError: boundary\x1b[0m\n2026-10-28T01:00:02Z ##[group]Run build\n2026-10-28T01:00:03Z ##[error]build failed\n"
    )
    index = index_log(text)
    assert index["total_lines"] == 4
    assert [(s["step"], s["marker_line"], s["timestamp"]) for s in index["sections"]] == [
        ("Run pytest", 2, "2026-10-28T01:00:01Z"),
        ("Run build", 4, "2026-10-28T01:00:03Z"),
    ]
    assert index["sections"][0]["line_start"] == 1 and index["sections"][0]["line_end"] == 4


def test_long_context_retains_early_traceback_and_records_exact_omitted_lines():
    lines = (
        ["setup\n"] * 35
        + [
            "Traceback (most recent call last):\n",
            '  File "src/module.py", line 9\n',
            "TypeError: required argument missing\n",
        ]
        + [f"irrelevant output {i}\n" for i in range(4000)]
        + ["Job failed\n"]
    )
    text = "".join(lines)
    result = context_log(text)
    assert "TypeError: required argument missing" not in text[-10000:]
    assert "TypeError: required argument missing" in result["text"]
    assert "Job failed" in result["text"] and "原日志 L" in result["text"]
    selection = result["selection"]
    assert selection["truncated"] is True
    assert len(result["text"].encode()) <= 10000
    covered = []
    for group in (selection["selected_ranges"], selection["omitted_ranges"]):
        for span in group:
            covered.extend(range(span["line_start"], span["line_end"] + 1))
    assert sorted(covered) == list(range(1, len(lines) + 1))
    assert 36 in {
        n for span in selection["selected_ranges"] for n in range(span["line_start"], span["line_end"] + 1)
    }


def test_long_line_clipping_is_explicit_and_utf8_bounded():
    text = "AssertionError: " + "中文" * 20000
    result = context_log(text, budget=2048)
    assert result["selection"]["truncated_lines"] == [1]
    assert result["selection"]["truncated"] is True
    assert "AssertionError" in result["text"]
    assert len(result["text"].encode()) <= 2048
    assert "\ufffd" not in result["text"]


def test_huge_setup_line_cannot_crowd_out_failure_marker():
    text = (
        "setup "
        + "x" * 20000
        + "\nTraceback (most recent call last):\nTypeError: broken call\n"
        + "normal output\n" * 5000
    )
    selected = context_log(text)
    assert "Traceback" in selected["text"] and "TypeError: broken call" in selected["text"]
    assert len(selected["text"].encode()) <= 10000


def test_index_limit_retains_first_and_last_failures_and_marks_omission():
    text = "".join(f"FAILED tests/test_{i}.py::test_it\n" + "normal line\n" * 100 for i in range(40))
    index = index_log(text)
    assert len(index["sections"]) == 24 and index["sections_truncated"] is True
    assert index["sections"][0]["marker_line"] == 1
    assert index["sections"][-1]["marker_line"] == 39 * 101 + 1


def test_window_roundtrip_keeps_every_byte_across_unicode_long_lines_and_crlf():
    text = "first\r\n" + "汉字🙂" * 12000 + "\n" + "last\n" * 205
    source = artifact(text)
    cursor, seen, parts = (1, 0), set(), []
    while cursor is not None:
        assert cursor not in seen
        seen.add(cursor)
        page = log_window(source, *cursor, max_lines=100)
        assert len(page.lines) <= 100
        assert sum(len(row.content.encode()) for row in page.lines) <= 16000
        parts.extend(row.content for row in page.lines)
        cursor = (page.next_line, page.next_column) if page.next_line is not None else None
    assert "".join(parts) == text
    assert len(seen) > 5


@pytest.mark.parametrize("line,column", [(0, 0), (3, 0), (1, -1), (1, 6)])
def test_window_rejects_out_of_range_cursors(line, column):
    with pytest.raises(AppError, match="请求位置"):
        log_window(artifact("first\nsecond"), line, column)


def test_empty_log_has_no_fake_line_or_next_cursor():
    page = log_window(artifact(""))
    assert page.total_lines == 0 and page.lines == [] and page.next_line is None
    with pytest.raises(AppError):
        log_window(artifact(""), 2)


def test_evidence_index_hashes_sanitized_content_and_limits_excerpt(monkeypatch):
    monkeypatch.setattr(settings(), "model_api_key", "test-only-provider-credential")
    body = CreateTask(source=Source(case_id="pagination")).model_dump()
    tid = store.create("owner", "log-evidence-test", body, metadata("pagination"))
    token, _, snap = store.claim(tid)
    ctx = Context(tid, token, body, snap)
    evidence = ctx.evidence("ci_log", "api_key=test-only-provider-credential\nAssertionError: " + "x" * 20000)
    stored = store.get_artifact(tid, evidence["artifact_id"])
    assert "test-only-provider-credential" not in stored["content"]
    assert evidence["log_index"]["sha256"] == stored["sha256"]
    assert evidence["excerpt_truncated"] is True and len(evidence["excerpt"]) <= 4000
    selected = ctx.select_log(stored["id"])
    archived = store.get_artifact(tid, selected["selection"]["context_artifact_id"])
    identity, _, text = archived["content"].partition("\n\n")
    assert json.loads(identity)["sha256"] == stored["sha256"]
    assert json.loads(identity)["truncated_lines"] == selected["selection"]["truncated_lines"]
    assert text == selected["text"]
    assert store.snapshot(tid)["evidence"][0]["log_context_artifact_ids"] == [archived["id"]]
    asyncio.run(ctx.close())


def test_log_window_api_is_bounded_private_and_checks_integrity(client):
    response = client.post(
        "/api/v1/tasks",
        json=CreateTask(source=Source(case_id="pagination")).model_dump(),
        headers={"Idempotency-Key": "log-window-test"},
    )
    tid = response.json()["data"]["task_id"]
    token, *_ = store.claim(tid)
    art = store.artifact(tid, "ci_log", "\x1b[31msecret=fake-value\x1b[0m\n" + "中文\n" * 1000, token)
    path = f"/api/v1/tasks/{tid}/artifacts/{art['id']}/lines"
    first = client.get(path)
    assert first.status_code == 200 and first.headers["cache-control"] == "private, no-store"
    data = first.json()["data"]
    assert len(data["lines"]) == 100 and data["next_line"] == 101
    assert "fake-value" not in json.dumps(data) and data["sha256"] == art["sha256"]
    with TestClient(app) as other:
        assert other.get(path).status_code == 404
    assert client.get(path.replace(tid, "0" * 32)).status_code == 404
    for query in (
        "max_lines=201",
        "max_lines=0",
        "start_line=0",
        "start_line=10000",
        "start_column=-1",
        "start_column=2000001",
    ):
        assert client.get(path + "?" + query).status_code == 422
    unsupported = store.artifact(tid, "report", "{}", token, content_type="application/json")
    assert client.get(path.replace(art["id"], unsupported["id"])).status_code == 422
    with db.transaction() as conn:
        conn.execute(update(db.artifacts).where(db.artifacts.c.id == art["id"]).values(content="tampered"))
    assert client.get(path).status_code == 409


def test_github_log_diagnosis_gets_early_failure_and_sanitized_original_ranges(monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "model_base_url", "https://explicit-test-provider.invalid")
    monkeypatch.setattr(cfg, "model_id", "test-only")
    monkeypatch.setattr(cfg, "model_api_key", "test-only-model-key")
    body = CreateTask(
        source=Source(
            type="github_actions",
            run_url="https://github.com/owner/repo/actions/runs/42",
            job_id=7,
            run_attempt=2,
        )
    ).model_dump()
    meta = {
        "repo": "owner/repo",
        "run_id": 42,
        "run_attempt": 2,
        "head_sha": "a" * 40,
        "profile_version": None,
        "title": "Synthetic long CI log",
        "execution_label": "Test-only log fixture",
    }
    tid = store.create("owner", "long-ci-log", body, meta)
    log = (
        "2026-10-28T01:00:00Z ##[group]Run pytest\nTraceback (most recent call last):\nTypeError: early missing argument\napi_key=synthetic-private-value\n"
        + "normal output\n" * 5000
        + "Job failed\n"
    )

    class GitHub:
        async def preview(self, *args):
            return {**meta, "jobs": [{"id": 7}]}

        async def logs(self, *args):
            return log

        async def close(self):
            pass

    class NoRunner:
        async def health(self):
            raise AssertionError("Unregistered log diagnosis must not inspect Runner")

        async def close(self):
            pass

    class Model:
        async def complete(self, messages):
            material = json.loads(messages[1]["content"])["materials"]
            assert "early missing argument" in material["log"]
            assert "synthetic-private-value" not in json.dumps(material)
            selection = material["log_selection"]
            assert selection["truncated"] and selection["omitted_ranges"]
            saved = store.get_artifact(tid, selection["artifact_id"])
            assert saved["sha256"] == selection["sha256"]
            archived = store.get_artifact(tid, selection["context_artifact_id"])
            assert archived["content"].partition("\n\n")[2] == material["log"]
            assert len(material["log"].encode()) <= 10000
            return json.dumps(
                {
                    "summary": "Test-only early error",
                    "failure_category": "test",
                    "root_cause": {
                        "description": "Hypothesis from log only",
                        "evidence_ids": [material["evidence"][0]["id"]],
                        "certainty": "hypothesis",
                    },
                    "next_action": "Inspect source separately",
                }
            ), {"total_tokens": 100}

        async def close(self):
            pass

    monkeypatch.setattr("buildmedic.orchestration.GitHubClient", GitHub)
    asyncio.run(run_claimed(tid, store.claim(tid), runner=NoRunner(), model=Model()))
    snap = store.snapshot(tid)
    assert snap["outcome"] == "diagnosed_only" and snap["executions"] == []
    assert snap["evidence"][0]["log_index"]["sections"][0]["marker_line"] == 2
