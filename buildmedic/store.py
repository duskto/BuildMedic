"""Transactional snapshots, durable events, outbox and fenced worker writes."""

import copy
import hashlib
import json
import uuid
from contextlib import nullcontext

from sqlalchemy import func, select, update

from . import db
from .config import settings
from .schemas import TERMINAL
from .security import sanitize


def uid():
    return uuid.uuid4().hex


class Conflict(Exception):
    pass


class LostLease(Exception):
    pass


def row(conn, task_id, owner=None):
    query = select(db.tasks).where(db.tasks.c.id == task_id)
    if owner is not None:
        query = query.where(db.tasks.c.owner_id == owner)
    result = conn.execute(query.with_for_update()).mappings().first()
    if not result:
        raise KeyError(task_id)
    return result


def fence(task, token):
    if (
        not token
        or task["lease_token"] != token
        or (task["heartbeat_at"] or 0) < db.now() - settings().lease_seconds
    ):
        raise LostLease("Worker lease 已失效")


def append_event(conn, task_id, snap, name, payload):
    seq = snap.get("last_event_seq", 0) + 1
    snap["last_event_seq"] = seq
    event = dict(payload, task_id=task_id, seq=seq, state_version=snap["state_version"], at=db.now())
    conn.execute(
        db.events.insert().values(
            task_id=task_id, seq=seq, event_type=name, payload=event, created_at=db.now()
        )
    )


def persist(conn, task, snap, name, payload=None, **values):
    snap["state_version"] += 1
    snap["updated_at"] = db.now()
    append_event(conn, task["id"], snap, name, payload or {})
    conn.execute(
        update(db.tasks)
        .where(db.tasks.c.id == task["id"])
        .values(
            snapshot=snap,
            lifecycle=snap["lifecycle"],
            version=snap["state_version"],
            updated_at=db.now(),
            **values,
        )
    )
    return snap


def request_hash(body, parent_id=None):
    body = dict(body)
    if not body.get("experience"):
        body.pop("experience", None)  # Preserve idempotency hashes for pre-Library clients.
    if not body.get("agent_tools"):
        body.pop("agent_tools", None)
    return hashlib.sha256(
        json.dumps({"body": body, "parent_task_id": parent_id}, sort_keys=True).encode()
    ).hexdigest()


def existing_request(owner, key, body, parent_id=None):
    """Resolve a retry before contacting any external source; create still handles races."""
    with db.transaction() as conn:
        existing = (
            conn.execute(
                select(db.tasks.c.id, db.tasks.c.request_hash).where(
                    db.tasks.c.owner_id == owner, db.tasks.c.idempotency_key == key
                )
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["request_hash"] != request_hash(body, parent_id):
                raise Conflict("同一个 Idempotency-Key 已用于不同请求")
            return existing["id"]
    return None


def lock_owner(conn, owner):
    if db.engine.dialect.name == "postgresql":
        lock_id = int.from_bytes(hashlib.sha256(owner.encode()).digest()[:8], "big", signed=True)
        conn.execute(select(func.pg_advisory_xact_lock(lock_id)))


def create(owner, key, body, source_meta, parent_id=None, *, connection=None):
    digest = request_hash(body, parent_id)
    with db.transaction() if connection is None else nullcontext(connection) as conn:
        # Serialize an owner's empty task set too, avoiding concurrent quota/idempotency races.
        lock_owner(conn, owner)
        existing = (
            conn.execute(
                select(db.tasks).where(db.tasks.c.owner_id == owner, db.tasks.c.idempotency_key == key)
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["request_hash"] != digest:
                raise Conflict("同一个 Idempotency-Key 已用于不同请求")
            return existing["id"]
        active = conn.scalar(
            select(func.count())
            .select_from(db.tasks)
            .where(
                db.tasks.c.owner_id == owner, db.tasks.c.lifecycle.in_(["queued", "running", "cancelling"])
            )
        )
        if active >= settings().max_active_tasks:
            raise Conflict("正在执行的任务已达上限，请稍后重试")
        task_id = uid()
        snap = dict(
            id=task_id,
            parent_task_id=parent_id,
            **source_meta,
            mode=body["mode"],
            agent_tools=body.get("agent_tools", False),
            lifecycle="queued",
            phase="ingest",
            phase_history=["ingest"],
            outcome=None,
            validation_status="not_run",
            reproduction_status="blocked",
            state_version=1,
            last_event_seq=0,
            created_at=db.now(),
            updated_at=db.now(),
            evidence=[],
            candidates=[],
            validations=[],
            rollouts=[],
            diagnosis=None,
            message="等待 Worker 领取任务",
            executions=[],
            model_calls=[],
            agent_actions=[],
            usage={"calls": 0, "tokens": 0, "cost_usd": None, "usage_known": True},
            limits=body["limits"],
            artifacts=[],
        )
        if body.get("experience"):
            from .experiences import bind_snapshot

            snap["experience"] = bind_snapshot(conn, owner, task_id, body, parent_id)
        append_event(conn, task_id, snap, "task.queued", {"lifecycle": "queued"})
        conn.execute(
            db.tasks.insert().values(
                id=task_id,
                owner_id=owner,
                idempotency_key=key,
                request_hash=digest,
                lifecycle="queued",
                version=1,
                created_at=db.now(),
                updated_at=db.now(),
                body=body,
                snapshot=snap,
                cancel_requested=False,
            )
        )
        conn.execute(
            db.outbox.insert().values(
                id=uid(), task_id=task_id, kind="diagnose", payload={}, created_at=db.now()
            )
        )
        return task_id


def snapshot(task_id, owner=None):
    with db.transaction() as conn:
        return copy.deepcopy(row(conn, task_id, owner)["snapshot"])


def task_body(task_id, owner):
    with db.transaction() as conn:
        return copy.deepcopy(row(conn, task_id, owner)["body"])


def mutate(task_id, event_name, changes=None, transform=None, token=None):
    with db.transaction() as conn:
        task = row(conn, task_id)
        fence(task, token)
        snap = copy.deepcopy(task["snapshot"])
        if task["lifecycle"] in TERMINAL and changes and changes.get("lifecycle") in {"running", "queued"}:
            raise LostLease("不能重新激活已结束的任务")
        snap.update(changes or {})
        if transform:
            transform(snap)
        return persist(conn, task, snap, event_name, changes)


def artifact(task_id, kind, content, token, content_type="text/plain; charset=utf-8", redact=True):
    cleaned = sanitize(content)
    # A patch must retain the exact validated bytes. Refuse secret-like content instead of silently changing it.
    if not redact and cleaned != content:
        raise ValueError("补丁含需脱敏的内容，无法安全交付")
    content = cleaned
    aid = uid()
    meta = {
        "id": aid,
        "kind": kind,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "size_bytes": len(content.encode()),
        "content_type": content_type,
    }
    with db.transaction() as conn:
        task = row(conn, task_id)
        fence(task, token)
        conn.execute(
            db.artifacts.insert().values(**meta, task_id=task_id, content=content, created_at=db.now())
        )
        snap = copy.deepcopy(task["snapshot"])
        snap["artifacts"].append(meta)
        persist(conn, task, snap, "artifact.created", {"artifact_id": aid, "kind": kind})
    return meta


def get_artifact(task_id, aid, owner=None):
    with db.transaction() as conn:
        row(conn, task_id, owner)
        result = (
            conn.execute(
                select(db.artifacts).where(db.artifacts.c.id == aid, db.artifacts.c.task_id == task_id)
            )
            .mappings()
            .first()
        )
        if not result:
            raise KeyError(aid)
        return dict(result)


def claim(task_id, validation_id=None):
    with db.transaction() as conn:
        task = row(conn, task_id)
        if validation_id:
            val = (
                conn.execute(
                    select(db.validations)
                    .where(db.validations.c.id == validation_id, db.validations.c.task_id == task_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if (
                not val
                or val["status"] != "queued"
                or task["lifecycle"] not in TERMINAL
                or task["lease_token"]
            ):
                return None
            conn.execute(
                update(db.validations).where(db.validations.c.id == validation_id).values(status="running")
            )
        elif task["lifecycle"] != "queued":
            return None
        token = uid()
        snap = copy.deepcopy(task["snapshot"])
        if not validation_id:
            snap.update(lifecycle="running", started_at=db.now())
        else:
            for validation in snap["validations"]:
                if validation["id"] == validation_id:
                    validation["status"] = "running"
        persist(
            conn,
            task,
            snap,
            "validation.started" if validation_id else "task.started",
            {"validation_id": validation_id} if validation_id else {"lifecycle": "running"},
            lease_token=token,
            heartbeat_at=db.now(),
        )
        conn.execute(
            db.attempts.insert().values(id=uid(), task_id=task_id, lease_token=token, started_at=db.now())
        )
        return token, copy.deepcopy(task["body"]), snap


def heartbeat(task_id, token):
    with db.transaction() as conn:
        task = row(conn, task_id)
        fence(task, token)
        conn.execute(update(db.tasks).where(db.tasks.c.id == task_id).values(heartbeat_at=db.now()))
        return task["cancel_requested"]


def control(task_id, token):
    with db.transaction() as conn:
        task = row(conn, task_id)
        fence(task, token)
        return task["cancel_requested"]


def close_open_records(snap, lifecycle):
    """A finished task cannot leave model calls or rollouts displayed as running."""
    for record in [*snap.get("model_calls", []), *snap.get("rollouts", []), *snap.get("agent_actions", [])]:
        if record["status"] in {"running", "response_received"}:
            record.update(
                status="interrupted" if lifecycle != "cancelled" else "cancelled", finished_at=db.now()
            )
    if (snap.get("harness") or {}).get("status") == "running":
        snap["harness"]["status"] = "cancelled" if lifecycle == "cancelled" else "interrupted"
    experience = snap.get("experience") or {}
    if experience.get("reflection_status") in {"pending", "running", "awaiting_validation"}:
        experience.update(reflection_status="cancelled" if lifecycle == "cancelled" else "interrupted")


def finish(task_id, token, lifecycle, outcome=None, message="", **changes):
    with db.transaction() as conn:
        task = row(conn, task_id)
        fence(task, token)
        snap = copy.deepcopy(task["snapshot"])
        if task["cancel_requested"] and lifecycle == "completed":
            lifecycle, outcome, message = "cancelled", None, "任务已取消，执行已清理"
        if outcome == "repaired":
            selected = next((c for c in snap["candidates"] if c["selected"]), None)
            independent = [
                v
                for v in snap["validations"]
                if v["kind"] == "independent"
                and v["status"] == "passed"
                and selected
                and v["candidate_id"] == selected["id"]
            ]
            if (
                snap["reproduction_status"] != "reproduced"
                or snap["validation_status"] != "passed"
                or not independent
            ):
                raise ValueError("repaired requires reproduction and independent verification evidence")
        snap.update(changes, lifecycle=lifecycle, outcome=outcome, message=message, finished_at=db.now())
        close_open_records(snap, lifecycle)
        persist(
            conn, task, snap, "task.finished", {"lifecycle": lifecycle, "outcome": outcome}, lease_token=None
        )
        conn.execute(
            update(db.attempts)
            .where(db.attempts.c.lease_token == token)
            .values(finished_at=db.now(), reason=lifecycle)
        )
        return snap


def cancel(task_id, owner):
    with db.transaction() as conn:
        task = row(conn, task_id, owner)
        if task["lifecycle"] in TERMINAL:
            return task["snapshot"]
        snap = copy.deepcopy(task["snapshot"])
        snap["lifecycle"] = "cancelled" if task["lifecycle"] == "queued" else "cancelling"
        if snap["lifecycle"] == "cancelled":
            snap["finished_at"] = db.now()
            close_open_records(snap, "cancelled")
        return persist(
            conn,
            task,
            snap,
            "task.finished" if snap["lifecycle"] == "cancelled" else "task.cancelling",
            {"lifecycle": snap["lifecycle"]},
            cancel_requested=True,
        )


def list_tasks(owner, lifecycle=None, repo=None, page=1, page_size=20):
    with db.transaction() as conn:
        filters = [db.tasks.c.owner_id == owner]
        if lifecycle:
            filters.append(db.tasks.c.lifecycle == lifecycle)
        rows = (
            conn.execute(select(db.tasks.c.snapshot).where(*filters).order_by(db.tasks.c.created_at.desc()))
            .scalars()
            .all()
        )
        if repo:
            rows = [r for r in rows if repo.lower() in r["repo"].lower()]
        return {
            "items": rows[(page - 1) * page_size : page * page_size],
            "total": len(rows),
            "page": page,
            "page_size": page_size,
        }


def read_events(task_id, after, owner=None):
    with db.transaction() as conn:
        task = row(conn, task_id, owner)
        rows = (
            conn.execute(
                select(db.events)
                .where(db.events.c.task_id == task_id, db.events.c.seq > after)
                .order_by(db.events.c.seq)
                .limit(250)
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows], task["lifecycle"] in TERMINAL, task["snapshot"]["last_event_seq"]


def enqueue_validation(task_id, owner, candidate_id):
    with db.transaction() as conn:
        task = row(conn, task_id, owner)
        snap = copy.deepcopy(task["snapshot"])
        if task["lifecycle"] != "completed" or not any(c["id"] == candidate_id for c in snap["candidates"]):
            raise Conflict("仅可重新验证已完成任务中的补丁")
        active = conn.scalar(
            select(func.count())
            .select_from(db.validations)
            .where(db.validations.c.task_id == task_id, db.validations.c.status.in_(["queued", "running"]))
        )
        if active or task["lease_token"]:
            raise Conflict("该任务已有进行中的验证")
        vid = uid()
        conn.execute(
            db.validations.insert().values(
                id=vid,
                task_id=task_id,
                candidate_id=candidate_id,
                status="queued",
                created_at=db.now(),
                result={},
            )
        )
        snap["validations"].append(
            {"id": vid, "candidate_id": candidate_id, "kind": "revalidation", "status": "queued"}
        )
        persist(conn, task, snap, "validation.queued", {"validation_id": vid})
        conn.execute(
            db.outbox.insert().values(
                id=uid(),
                task_id=task_id,
                kind="validate",
                payload={"validation_id": vid, "candidate_id": candidate_id},
                created_at=db.now(),
            )
        )
        return vid


def save_validation(task_id, token, result, revalidation=False):
    with db.transaction() as conn:
        task = row(conn, task_id)
        fence(task, token)
        snap = copy.deepcopy(task["snapshot"])
        snap["validations"] = [v for v in snap["validations"] if v["id"] != result["id"]] + [result]
        if revalidation:
            conn.execute(
                update(db.validations)
                .where(db.validations.c.id == result["id"])
                .values(status=result["status"], result=result)
            )
        persist(
            conn,
            task,
            snap,
            "validation.finished",
            {"validation_id": result["id"], "status": result["status"]},
            **({"lease_token": None} if revalidation else {}),
        )
        if revalidation:
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.lease_token == token)
                .values(finished_at=db.now(), reason=result["status"])
            )
