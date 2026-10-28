"""Detached, post-experiment scoring. This module has no private bundle access."""

import copy
import hashlib
import json
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update

from . import db, evaluation, store
from .config import settings
from .errors import AppError
from .scoring_protocol import ScoreItem, ScoreReceipt, ScoreRequest, fingerprint, verify_receipt


def existing(conn, eid, owner):
    return (
        conn.execute(
            select(db.experiment_scores)
            .where(db.experiment_scores.c.experiment_id == eid, db.experiment_scores.c.owner_id == owner)
            .with_for_update()
        )
        .mappings()
        .first()
    )


def reserve(eid, owner):
    cfg = settings()
    with db.transaction() as conn:
        store.lock_owner(conn, owner)
        experiment = evaluation.load(conn, eid, owner)
        prior = existing(conn, eid, owner)
        if prior:
            return prior["id"]
        if not cfg.scoring_ready:
            raise AppError("scoring_unavailable", "尚未配置独立评分服务、令牌和固定评分包指纹", 422)
        validate_endpoint(cfg.scorer_url)
        if experiment["status"] != "completed":
            raise store.Conflict("独立评分只能在整批实验结束并冻结输入后运行")
        public = evaluation.render(experiment)
        if public["manifest"]["schema_version"] != "2.0":
            raise AppError("score_protocol_unsupported", "独立评分仅支持新版四组完整清单", 422)
        image = public["configuration"]["runner_image"]
        if public["configuration"]["runner_backend"] != "docker":
            raise AppError("score_requires_docker", "独立行为评测要求修复任务使用固定 Docker 环境", 422)
        items = []
        for index, (row, slot) in enumerate(zip(public["rows"], experiment["content"]["slots"])):
            frozen = slot["frozen"]
            if not frozen:
                raise store.Conflict("实验终态输入尚未完整冻结")
            snap = frozen["snapshot"]
            selected = [c for c in snap["candidates"] if c.get("selected")]
            if len(selected) > 1:
                raise store.Conflict("冻结任务有多个选中候选")
            patch, patch_hash = None, None
            if selected:
                candidate = selected[0]
                artifact = (
                    conn.execute(
                        select(db.artifacts).where(
                            db.artifacts.c.id == candidate["patch_artifact_id"],
                            db.artifacts.c.task_id == snap["id"],
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    not artifact
                    or hashlib.sha256(artifact["content"].encode()).hexdigest() != candidate["patch_hash"]
                ):
                    raise store.Conflict("冻结补丁归档缺失或指纹不一致")
                patch, patch_hash = artifact["content"], candidate["patch_hash"]
            if snap.get("baseline") and any(
                e.get("isolation") != "docker" or e.get("image_digest") != image for e in snap["baseline"]
            ):
                raise store.Conflict("冻结执行环境与清单中的 Docker 镜像不一致")
            items.append(
                ScoreItem(
                    index=index,
                    task_id=snap["id"],
                    case_id=row["case_id"],
                    arm=row["mode"],
                    snapshot_sha256=snap["snapshot_sha256"],
                    public_verified=row["metrics"]["public_verified"],
                    patch=patch,
                    patch_sha256=patch_hash,
                )
            )
        sid = store.uid()
        body = ScoreRequest(
            id=sid,
            experiment_id=eid,
            report_sha256=public["report_sha256"],
            suite_sha256=cfg.scorer_suite_sha256,
            image_digest=image,
            rows=items,
        )
        conn.execute(
            db.experiment_scores.insert().values(
                id=sid,
                experiment_id=eid,
                owner_id=owner,
                status="pending",
                created_at=db.now(),
                content={
                    "request": body.model_dump(),
                    "receipt": None,
                    "error_code": None,
                    "endpoint_sha256": hashlib.sha256(cfg.scorer_url.encode()).hexdigest(),
                },
            )
        )
        return sid


def validate_endpoint(value):
    url = urlsplit(value)
    if (
        url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
        or not url.hostname
        or (
            url.scheme != "https"
            and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise AppError("scoring_configuration", "独立评分地址需为 HTTPS 或受信任的本机 HTTP 地址", 422)
    try:
        _ = url.port
    except ValueError as exc:
        raise AppError("scoring_configuration", "独立评分地址端口无效", 422) from exc


class ScorerClient:
    def __init__(self, transport=None):
        cfg = settings()
        validate_endpoint(cfg.scorer_url)
        self.client = httpx.AsyncClient(
            base_url=cfg.scorer_url,
            trust_env=False,
            follow_redirects=False,
            headers={"Authorization": "Bearer " + cfg.scorer_token},
            timeout=15,
            transport=transport,
        )

    async def submit(self, body):
        async with self.client.stream("POST", "/scorer/v1/jobs", json=body) as response:
            if response.status_code != 202:
                raise AppError(
                    "scorer_unavailable",
                    "独立评分服务拒绝请求；请检查固定评分包、镜像、令牌和队列",
                    503,
                    True,
                )
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 64000:
                    raise ValueError("scorer_response_limit")
                chunks.append(chunk)
            return json.loads(b"".join(chunks))

    async def close(self):
        await self.client.aclose()


def summarize(request, receipt):
    rows = {row["index"]: row for row in receipt["rows"]}
    summaries = []
    for arm in ("logs", "standard", "multi", "adaptive"):
        planned = [r for r in request["rows"] if r["arm"] == arm]
        results = [rows[r["index"]] for r in planned if r["index"] in rows]
        tested = [
            r for r in planned if r["index"] in rows and rows[r["index"]]["hidden_verified"] is not None
        ]
        declared = [r for r in tested if r["public_verified"]]
        regression_checked = [r for r in tested if rows[r["index"]]["new_regression"] is not None]
        summaries.append(
            {
                "mode": arm,
                "planned": len(planned),
                "repair_applicable": arm != "logs",
                "behavior_verified": sum(
                    r["public_verified"] and rows[r["index"]]["hidden_verified"] is True for r in tested
                ),
                "evaluated": len(tested),
                "evaluation_errors": sum(r["status"] == "evaluation_error" for r in results),
                "not_reproduced": sum(r["status"] == "not_reproduced" for r in results),
                "no_candidate": sum(r["status"] == "no_candidate" for r in results),
                "false_positives": sum(rows[r["index"]]["hidden_verified"] is False for r in declared),
                "false_positive_denominator": len(declared),
                "new_regressions": sum(
                    rows[r["index"]]["new_regression"] is True for r in regression_checked
                ),
                "regression_denominator": len(regression_checked),
                "diagnosis_accuracy": None,
            }
        )
    return summaries


def public(row):
    content = row["content"]
    receipt = content.get("receipt")
    return {
        "id": row["id"],
        "experiment_id": row["experiment_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "report_sha256": content["request"]["report_sha256"],
        "suite_sha256": content["request"]["suite_sha256"],
        "error_code": content.get("error_code"),
        "receipt": receipt,
        "summary": summarize(content["request"], receipt)
        if receipt and receipt["status"] == "completed"
        else [],
        "scope": "公开开发案例的独立私有行为检查；未评测隔离测试集或诊断准确率",
    }


async def get(eid, owner, client=None):
    with db.transaction() as conn:
        evaluation.load(conn, eid, owner)
        found = existing(conn, eid, owner)
        if not found:
            return None
        row = dict(found)
        row["content"] = copy.deepcopy(row["content"])
    if row["status"] in {"completed", "failed"}:
        return public(row)
    cfg = settings()
    request = ScoreRequest.model_validate(row["content"]["request"])
    if (
        cfg.scorer_suite_sha256 != request.suite_sha256
        or hashlib.sha256(cfg.scorer_url.encode()).hexdigest() != row["content"]["endpoint_sha256"]
    ):
        raise store.Conflict("独立评分的固定服务或评分包配置已变化")
    service = client
    try:
        service = service or ScorerClient()
        result = await service.submit(
            request.model_dump()
        )  # Same immutable ID; safe after a lost response/restart.
        if result.get("id") != row["id"] or result.get("request_sha256") != fingerprint(request.model_dump()):
            raise ValueError("scorer_request_mismatch")
        receipt = result.get("receipt")
        if receipt:
            receipt = verify_receipt(ScoreReceipt.model_validate(receipt), request, cfg.scorer_token)
            row["content"]["receipt"] = receipt
            row["status"] = receipt["status"]
        else:
            row["status"] = "running"
        row["content"]["error_code"] = None
    except (httpx.HTTPError, AppError):
        row["content"]["error_code"] = "scorer_unavailable"
    except ValueError:
        row["status"] = "failed"
        row["content"]["error_code"] = "score_receipt_rejected"
    finally:
        if service:
            await service.close()
    with db.transaction() as conn:
        current = existing(conn, eid, owner)
        if current["status"] in {"completed", "failed"}:
            return public(current)
        conn.execute(
            update(db.experiment_scores)
            .where(db.experiment_scores.c.id == row["id"])
            .values(status=row["status"], content=row["content"])
        )
    return public(row)
