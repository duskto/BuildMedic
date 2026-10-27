import copy
import hashlib
import json
import uuid
from sqlalchemy import select, update, func
from . import db
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


def append_event(conn, task_id, snapshot, name, payload):
    seq = snapshot.get("last_event_seq", 0) + 1
    snapshot["last_event_seq"] = seq
    event = dict(payload, task_id=task_id, seq=seq, state_version=snapshot["state_version"], at=db.now())
    conn.execute(db.events.insert().values(task_id=task_id, seq=seq, event_type=name,
                                         payload=event, created_at=db.now()))


def create(owner, key, body, source_meta, parent_id=None):
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with db.transaction() as conn:
        existing = conn.execute(select(db.tasks).where(db.tasks.c.owner_id == owner,
            db.tasks.c.idempotency_key == key)).mappings().first()
        if existing:
            if existing["request_hash"] != digest:
                raise Conflict("同一个 Idempotency-Key 已用于不同请求")
            return existing["id"]
        active = conn.scalar(select(func.count()).select_from(db.tasks).where(
            db.tasks.c.owner_id == owner, db.tasks.c.lifecycle.in_(["queued", "running", "cancelling"])))
        from .config import settings
        if active >= settings().max_active_tasks:
            raise Conflict("正在执行的任务已达上限，请稍后重试")
        task_id = uid()
        snapshot = dict(id=task_id, parent_task_id=parent_id, **source_meta, mode=body["mode"],
            lifecycle="queued", phase="ingest", outcome=None, validation_status="not_run",
            reproduction_status="blocked", state_version=1, last_event_seq=0,
            created_at=db.now(), updated_at=db.now(), evidence=[], candidates=[], validations=[],
            rollouts=[], diagnosis=None, message="等待 Worker 领取任务", usage={"calls": 0, "tokens": 0,
            "cost_usd": None, "usage_known": True}, limits=body["limits"], artifacts=[])
        append_event(conn, task_id, snapshot, "task.queued", {"lifecycle": "queued"})
        conn.execute(db.tasks.insert().values(id=task_id, owner_id=owner, idempotency_key=key,
            request_hash=digest, lifecycle="queued", version=1, created_at=db.now(), updated_at=db.now(),
            body=body, snapshot=snapshot, cancel_requested=False))
        conn.execute(db.outbox.insert().values(id=uid(), task_id=task_id, kind="diagnose", payload={}, created_at=db.now()))
        return task_id


def snapshot(task_id, owner=None):
    with db.transaction() as conn:
        return copy.deepcopy(row(conn, task_id, owner)["snapshot"])


def mutate(task_id, event_name, changes=None, transform=None, token=None):
    with db.transaction() as conn:
        task = row(conn, task_id)
        if token and task["lease_token"] != token:
            raise LostLease("Worker lease 已失效")
        snap = copy.deepcopy(task["snapshot"])
        if task["lifecycle"] in TERMINAL and changes and changes.get("lifecycle") in {"running", "queued"}:
            raise LostLease("不能重新激活已结束的任务")
        snap.update(changes or {})
        if transform:
            transform(snap)
        snap["state_version"] += 1
        snap["updated_at"] = db.now()
        append_event(conn, task_id, snap, event_name, changes or {})
        conn.execute(update(db.tasks).where(db.tasks.c.id == task_id).values(snapshot=snap,
            lifecycle=snap["lifecycle"], version=snap["state_version"], updated_at=db.now()))
        return snap


def artifact(task_id, kind, content, token, content_type="text/plain; charset=utf-8"):
    content = sanitize(content)
    aid = uid()
    meta = dict(id=aid, kind=kind, sha256=hashlib.sha256(content.encode()).hexdigest(),
                size_bytes=len(content.encode()), content_type=content_type)
    with db.transaction() as conn:
        task = row(conn, task_id)
        if task["lease_token"] != token:
            raise LostLease()
        conn.execute(db.artifacts.insert().values(**meta, task_id=task_id, content=content, created_at=db.now()))
    mutate(task_id, "artifact.created", transform=lambda s: s["artifacts"].append(meta), token=token)
    return meta


def claim(task_id):
    with db.transaction() as conn:
        task = row(conn, task_id)
        if task["lifecycle"] != "queued":
            return None
        token = uid()
        snap = copy.deepcopy(task["snapshot"])
        snap.update(lifecycle="running", started_at=db.now(), state_version=snap["state_version"] + 1)
        append_event(conn, task_id, snap, "task.started", {"lifecycle": "running"})
        conn.execute(update(db.tasks).where(db.tasks.c.id == task_id).values(lifecycle="running",
            lease_token=token, heartbeat_at=db.now(), snapshot=snap, version=snap["state_version"]))
        conn.execute(db.attempts.insert().values(id=uid(), task_id=task_id, lease_token=token, started_at=db.now()))
        return token, task["body"], snap


def cancel(task_id, owner):
    with db.transaction() as conn:
        task = row(conn, task_id, owner)
        if task["lifecycle"] in TERMINAL:
            return task["snapshot"]
        snap = copy.deepcopy(task["snapshot"])
        snap["lifecycle"] = "cancelled" if task["lifecycle"] == "queued" else "cancelling"
        snap["state_version"] += 1
        if snap["lifecycle"] == "cancelled":
            snap["finished_at"] = db.now()
        append_event(conn, task_id, snap, "task.finished" if snap["lifecycle"] == "cancelled" else "task.cancelling",
                     {"lifecycle": snap["lifecycle"]})
        conn.execute(update(db.tasks).where(db.tasks.c.id == task_id).values(cancel_requested=True,
            lifecycle=snap["lifecycle"], snapshot=snap, version=snap["state_version"]))
        return snap
