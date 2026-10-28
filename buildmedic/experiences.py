"""Frozen owner-scoped Library inputs and explicit, serialized evidence publication.

Native Harness² writes only to task-private directories. These transactions are the
sole path to shared versions; a model cannot choose the tree, scope, or verdict.
"""

import copy
import hashlib
import json

from pydantic import Field, model_validator
from sqlalchemy import func, select, update

from . import db, store
from .cases import CASES
from .config import HARNESS_SHA, ROOT
from .schemas import StrictModel
from .security import sanitize


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def profile_hash():
    base = ROOT / "harnesses" / "base"
    return digest(
        {
            "harness2_sha": HARNESS_SHA,
            "bridge": "buildmedic_http-v1",
            "files": {p.relative_to(base).as_posix(): p.read_text() for p in base.rglob("*") if p.is_file()},
        }
    )


class Reflection(StrictModel):
    has_lesson: bool = Field(strict=True)
    cue: str = Field("", max_length=300)
    lesson: str = Field("", max_length=1000)
    edits: str = Field("", max_length=600)
    reason: str = Field("", max_length=300)
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def valid_lesson(self):
        for text in (self.cue, self.lesson, self.edits, self.reason, *self.evidence_ids):
            if any(c in text for c in ("\n", "\r", "\x00", "<", ">")) or sanitize(text) != text:
                raise ValueError("反思字段必须为不含标签或敏感信息的单行文本")
        if self.has_lesson and not (self.cue.strip() and self.lesson.strip() and self.evidence_ids):
            raise ValueError("经验必须包含触发线索、可迁移指导和真实证据 ID")
        return self

    def native(self):
        return {
            "cue": self.cue,
            "lesson": self.lesson,
            "edits": self.edits,
            "evidence": ", ".join(self.evidence_ids),
            "confidence": "task_observation; development_only; transfer_not_evaluated",
        }


def checked(record):
    if digest(record["content"]) != record["content_hash"]:
        raise store.Conflict("经验内容校验失败，请检查归档")
    return record


def summary(record):
    return {
        **{
            k: record[k]
            for k in ("id", "domain", "version", "source_task_id", "status", "created_at", "content_hash")
        },
        **{k: record["content"][k] for k in ("cue", "lesson", "evidence_ids", "scope")},
    }


def get_snapshot(snapshot_id, owner=None, *, task_id=None):
    with db.transaction() as conn:
        query = select(db.experience_snapshots).where(db.experience_snapshots.c.id == snapshot_id)
        if owner is not None:
            query = query.where(db.experience_snapshots.c.owner_id == owner)
        if task_id is not None:
            query = query.where(db.experience_snapshots.c.task_id == task_id)
        record = conn.execute(query).mappings().first()
        if not record:
            raise KeyError(snapshot_id)
        checked(record)
        return {k: copy.deepcopy(v) for k, v in record.items() if k != "owner_id"}


def bind_snapshot(conn, owner, task_id, body, parent_id):
    domain = CASES[body["source"]["case_id"]]["category"]
    if parent_id:
        parent = store.row(conn, parent_id, owner)["snapshot"].get("experience")
        if not parent:
            raise store.Conflict("重试任务缺少原经验快照")
        source = (
            conn.execute(
                select(db.experience_snapshots).where(
                    db.experience_snapshots.c.id == parent["snapshot_id"],
                    db.experience_snapshots.c.owner_id == owner,
                )
            )
            .mappings()
            .one()
        )
        content = copy.deepcopy(checked(source)["content"])
    else:
        profile = profile_hash()
        own_domain = (db.experience_versions.c.owner_id == owner, db.experience_versions.c.domain == domain)
        head = conn.scalar(select(func.max(db.experience_versions.c.version)).where(*own_domain)) or 0
        chosen = (
            conn.execute(
                select(db.experience_versions)
                .where(
                    *own_domain,
                    db.experience_versions.c.status == "active",
                    db.experience_versions.c.content["profile_hash"].as_string() == profile,
                )
                .order_by(db.experience_versions.c.version.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if chosen:
            checked(chosen)
        content = {
            "domain": domain,
            "head_version": head,
            "profile_hash": profile,
            "scope": "development_only",
            "entries": [dict(summary(chosen), content=chosen["content"])] if chosen else [],
        }
    sid, fingerprint = store.uid(), digest(content)
    conn.execute(
        db.experience_snapshots.insert().values(
            id=sid,
            owner_id=owner,
            task_id=task_id,
            created_at=db.now(),
            content_hash=fingerprint,
            content=content,
        )
    )
    return {
        "enabled": True,
        "snapshot_id": sid,
        "snapshot_hash": fingerprint,
        "domain": content["domain"],
        "head_version": content["head_version"],
        "entries": [{k: v for k, v in e.items() if k != "content"} for e in content["entries"]],
        "reflection_status": "pending",
        "draft": None,
        "published_version_id": None,
    }


def list_versions(owner, domain=None, page=1, page_size=20):
    query = select(db.experience_versions).where(db.experience_versions.c.owner_id == owner)
    if domain:
        query = query.where(db.experience_versions.c.domain == domain)
    with db.transaction() as conn:
        total = conn.scalar(select(func.count()).select_from(query.subquery()))
        records = (
            conn.execute(
                query.order_by(db.experience_versions.c.created_at.desc(), db.experience_versions.c.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            .mappings()
            .all()
        )
        return {
            "items": [summary(checked(r)) for r in records],
            "total": total,
            "page": page,
            "page_size": page_size,
        }


def get_version(version_id, owner):
    with db.transaction() as conn:
        record = (
            conn.execute(
                select(db.experience_versions).where(
                    db.experience_versions.c.id == version_id, db.experience_versions.c.owner_id == owner
                )
            )
            .mappings()
            .first()
        )
        if not record:
            raise KeyError(version_id)
        return {**summary(checked(record)), "content": copy.deepcopy(record["content"])}


def revoke(version_id, owner):
    with db.transaction() as conn:
        store.lock_owner(conn, owner)
        record = (
            conn.execute(
                select(db.experience_versions)
                .where(db.experience_versions.c.id == version_id, db.experience_versions.c.owner_id == owner)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if not record:
            raise KeyError(version_id)
        conn.execute(
            update(db.experience_versions)
            .where(db.experience_versions.c.id == version_id)
            .values(status="revoked")
        )
        return {**summary(checked(record)), "status": "revoked"}


def _artifact(conn, tid, aid, kind):
    record = (
        conn.execute(
            select(db.artifacts).where(
                db.artifacts.c.id == aid, db.artifacts.c.task_id == tid, db.artifacts.c.kind == kind
            )
        )
        .mappings()
        .first()
    )
    if not record or hashlib.sha256(record["content"].encode()).hexdigest() != record["sha256"]:
        raise store.Conflict("经验引用的归档缺失或校验失败")
    return record


def publish(tid, owner, key):
    with db.transaction() as conn:
        store.lock_owner(conn, owner)
        task = store.row(conn, tid, owner)
        existing = (
            conn.execute(
                select(db.experience_versions).where(
                    db.experience_versions.c.owner_id == owner,
                    db.experience_versions.c.idempotency_key == key,
                )
            )
            .mappings()
            .first()
        )
        if existing and existing["source_task_id"] != tid:
            raise store.Conflict("同一个 Idempotency-Key 已用于其他经验发布")
        existing = (
            existing
            or conn.execute(
                select(db.experience_versions).where(db.experience_versions.c.source_task_id == tid)
            )
            .mappings()
            .first()
        )
        if existing:
            return summary(checked(existing))
        snap = copy.deepcopy(task["snapshot"])
        experience = snap.get("experience") or {}
        draft = experience.get("draft")
        if (
            snap["lifecycle"] != "completed"
            or snap["mode"] != "adaptive"
            or snap["outcome"] != "repaired"
            or snap["validation_status"] != "passed"
            or not draft
            or experience.get("reflection_status") != "available"
        ):
            raise store.Conflict("仅能发布已完成且通过最终独立验证的经验草稿")
        content = json.loads(_artifact(conn, tid, draft["artifact_id"], "experience_draft")["content"])
        reflection = Reflection.model_validate({k: content[k] for k in Reflection.model_fields})
        evidence_ids = {e["id"] for e in snap["evidence"]}
        selected = next((c for c in snap["candidates"] if c["selected"]), None)
        rollout = next((r for r in snap["rollouts"] if selected and r["id"] == selected["rollout_id"]), None)
        validation = next((v for v in snap["validations"] if v["id"] == content["validation_id"]), None)
        call = next((c for c in snap["model_calls"] if c["id"] == content["model_call_id"]), None)
        if (
            not reflection.has_lesson
            or not set(reflection.evidence_ids) <= evidence_ids
            or not selected
            or content["candidate_id"] != selected["id"]
            or not rollout
            or content["harness_hash"] != rollout["harness_hash"]
            or content["harness_hash"] != digest(content["tree"])
            or not validation
            or validation["kind"] != "independent"
            or validation["status"] != "passed"
            or validation["candidate_id"] != selected["id"]
            or not call
            or call["role"] != "experience_reflection"
            or call["status"] != "completed"
            or call.get("harness_hash") != content["harness_hash"]
            or any(validation[k]["patch_hash"] != selected["patch_hash"] for k in ("target", "regression"))
        ):
            raise store.Conflict("经验与选中策略、证据或最终验证不匹配")
        for evidence in snap["evidence"]:
            if evidence["id"] in reflection.evidence_ids:
                _artifact(conn, tid, evidence["artifact_id"], evidence["kind"])
        frozen = (
            conn.execute(
                select(db.experience_snapshots).where(
                    db.experience_snapshots.c.id == experience["snapshot_id"],
                    db.experience_snapshots.c.owner_id == owner,
                    db.experience_snapshots.c.task_id == tid,
                )
            )
            .mappings()
            .one()
        )
        checked(frozen)
        tree = json.loads(_artifact(conn, tid, rollout["harness_artifact_id"], "harness_snapshot")["content"])
        patch = _artifact(conn, tid, selected["patch_artifact_id"], "patch")
        if (
            tree != content["tree"]
            or frozen["content_hash"] != experience["snapshot_hash"]
            or frozen["content"]["domain"] != experience["domain"]
            or content["profile_hash"] != frozen["content"]["profile_hash"]
            or patch["sha256"] != selected["patch_hash"]
        ):
            raise store.Conflict("策略或输入快照与经验草稿不匹配")
        domain = experience["domain"]
        version = (
            conn.scalar(
                select(func.max(db.experience_versions.c.version)).where(
                    db.experience_versions.c.owner_id == owner, db.experience_versions.c.domain == domain
                )
            )
            or 0
        ) + 1
        vid, created = store.uid(), db.now()
        content.update(
            scope="development_only",
            source_task_id=tid,
            case_id=snap["case_id"],
            harness2_sha=snap["harness2_sha"],
            patch_hash=selected["patch_hash"],
            model=call["model"],
            response_model=call.get("response_model"),
            input_snapshot_id=frozen["id"],
            input_snapshot_hash=frozen["content_hash"],
            parent_version_ids=[e["id"] for e in frozen["content"]["entries"]],
        )
        record = {
            "id": vid,
            "owner_id": owner,
            "domain": domain,
            "version": version,
            "source_task_id": tid,
            "idempotency_key": key,
            "status": "active",
            "created_at": created,
            "content_hash": digest(content),
            "content": content,
        }
        conn.execute(db.experience_versions.insert().values(**record))
        experience["published_version_id"] = vid
        store.persist(conn, task, snap, "experience.published", {"version_id": vid, "version": version})
        return summary(record)
