"""Operator-only preparation/import; public reads never expose private review material."""

import copy
import secrets

from sqlalchemy import select, update

from . import db, evaluation, store
from .config import settings
from .diagnosis_protocol import ReviewReceipt, ReviewRequest, verify
from .schemas import Diagnosis
from .scoring_protocol import fingerprint


def existing(conn, eid, owner):
    return (
        conn.execute(
            select(db.diagnosis_scores)
            .where(db.diagnosis_scores.c.experiment_id == eid, db.diagnosis_scores.c.owner_id == owner)
            .with_for_update()
        )
        .mappings()
        .first()
    )


def prepare(eid, owner):
    cfg = settings()
    if len(cfg.diagnosis_review_key) < 32:
        raise ValueError("diagnosis_review_key_required")
    with db.transaction() as conn:
        store.lock_owner(conn, owner)
        experiment = evaluation.load(conn, eid, owner)
        prior = existing(conn, eid, owner)
        if prior:
            return copy.deepcopy(prior["content"]["request"])
        if experiment["status"] != "completed":
            raise store.Conflict("诊断审核要求整批实验结束并冻结")
        public = evaluation.render(experiment)
        if public["manifest"]["schema_version"] not in {"2.0", "3.0", "4.0"}:
            raise ValueError("diagnosis_review_requires_four_arms")
        rows, bindings = [], []
        for row, slot in zip(public["rows"], experiment["content"]["slots"], strict=True):
            frozen = slot.get("frozen")
            if not frozen:
                raise store.Conflict("实验输入尚未完整冻结")
            snap = frozen["snapshot"]
            diagnosis = snap.get("diagnosis")
            if diagnosis is not None:
                diagnosis = Diagnosis.model_validate(diagnosis).model_dump(exclude={"patch", "edits"})
            cited = set(diagnosis["root_cause"]["evidence_ids"]) if diagnosis else set()
            evidence = []
            for item in snap.get("evidence", []):
                if item["id"] not in cited:
                    continue
                excerpt = item["excerpt"].encode()[:600].decode("utf-8", errors="ignore")
                evidence.append(
                    {k: item.get(k) for k in ("id", "kind", "path", "line_start", "line_end")}
                    | {
                        "excerpt": excerpt,
                        "excerpt_truncated": item.get("excerpt_truncated", False)
                        or excerpt != item["excerpt"],
                    }
                )
            material = {
                "case_id": row["case_id"],
                "snapshot_sha256": snap["snapshot_sha256"],
                "diagnosis": diagnosis,
                "evidence": evidence,
            }
            rid = secrets.token_hex(16)
            rows.append(material | {"review_id": rid, "assessment_sha256": fingerprint(material)})
            bindings.append({"review_id": rid, "mode": row["mode"]})
        secrets.SystemRandom().shuffle(rows)
        request = ReviewRequest(
            id=store.uid(),
            report_sha256=public["report_sha256"],
            rubric_sha256=cfg.diagnosis_rubric_sha256,
            rows=rows,
        ).model_dump()
        conn.execute(
            db.diagnosis_scores.insert().values(
                id=request["id"],
                experiment_id=eid,
                owner_id=owner,
                status="awaiting_review",
                created_at=db.now(),
                content={"request": request, "bindings": bindings, "receipt": None},
            )
        )
        return request


def ingest(eid, owner, body):
    receipt = ReviewReceipt.model_validate(body)
    with db.transaction() as conn:
        evaluation.load(conn, eid, owner)
        row = existing(conn, eid, owner)
        if not row:
            raise KeyError(eid)
        content = copy.deepcopy(row["content"])
        if row["status"] == "completed":
            if content["receipt"] != receipt.model_dump():
                raise store.Conflict("已封存的诊断审核不可覆盖")
            return public(row)
        request = ReviewRequest.model_validate(content["request"])
        if evaluation.render(evaluation.load(conn, eid, owner))["report_sha256"] != request.report_sha256:
            raise store.Conflict("原实验报告已变化，拒绝导入")
        content["receipt"] = verify(receipt, request, settings().diagnosis_review_key)
        conn.execute(
            update(db.diagnosis_scores)
            .where(db.diagnosis_scores.c.id == row["id"])
            .values(
                status="completed",
                content=content,
            )
        )
        return public(dict(row) | {"content": content, "status": "completed"})


def summarize(content):
    verdicts = {v["review_id"]: v for v in (content.get("receipt") or {}).get("rows", [])}
    items = {r["review_id"]: r for r in content["request"]["rows"]}
    result = []
    for mode in ("logs", "standard", "multi", "adaptive"):
        ids = [b["review_id"] for b in content["bindings"] if b["mode"] == mode]
        delivered = [rid for rid in ids if items[rid]["diagnosis"] is not None]
        reviewed = [verdicts[rid] for rid in delivered if rid in verdicts]
        root = sum(r["root_cause_correct"] is True for r in reviewed)
        location = sum(r["location_correct"] is True for r in reviewed)
        both = sum(r["root_cause_correct"] is True and r["location_correct"] is True for r in reviewed)
        sealed = content.get("receipt") is not None
        result.append(
            {
                "mode": mode,
                "planned": len(ids),
                "delivered": len(delivered),
                "missing": len(ids) - len(delivered),
                "reviewed": len(reviewed),
                "unreviewed": len(delivered) - len(reviewed),
                "root_cause_correct": root,
                "location_correct": location,
                "both_correct": both,
                "diagnosis_accuracy": both / len(ids) if sealed and ids else None,
            }
        )
    return result


def public(row):
    content = row["content"]
    return {
        "id": row["id"],
        "experiment_id": row["experiment_id"],
        "status": row["status"],
        "report_sha256": content["request"]["report_sha256"],
        "rubric_sha256": content["request"]["rubric_sha256"],
        "receipt": content.get("receipt"),
        "summary": summarize(content),
        "scope": "独立人工审核：根因与定位同时正确 / 全部计划任务；缺失诊断计入分母，待审核不计准确率。",
    }


def get(eid, owner):
    with db.transaction() as conn:
        evaluation.load(conn, eid, owner)
        row = existing(conn, eid, owner)
        return public(row) if row else None
