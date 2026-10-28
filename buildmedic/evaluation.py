"""Preregistered, owner-scoped development comparisons; never a hidden-test score."""

import copy
import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import func, select, update

from . import db, store
from .cases import metadata
from .config import HARNESS_SHA, ROOT, settings
from .errors import Blocked
from .schemas import TERMINAL, CreateTask, Limits, Source, StrictModel


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class EvaluationCase(StrictModel):
    case_id: Literal["pagination", "imports", "signature"]
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExperimentSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.:-]{0,79}$")
    dataset: Literal["public-development"] = "public-development"
    cases: list[EvaluationCase] = Field(min_length=1, max_length=3)
    repeats: int = Field(1, ge=1, le=3, strict=True)
    agent_tools: bool = Field(False, strict=True)
    limits: Limits = Field(default_factory=lambda: Limits(max_model_calls=12, max_tokens=100000))

    @model_validator(mode="after")
    def bounded(self):
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("案例不能重复；重复运行请使用 repeats")
        for mode in ("standard", "adaptive"):
            CreateTask(
                source=Source(case_id=self.cases[0].case_id),
                mode=mode,
                limits=self.limits,
                agent_tools=self.agent_tools,
            )
        minimum = 11 if self.agent_tools else 7
        if self.limits.max_model_calls < minimum or self.limits.max_tokens < 48000:
            raise ValueError("同预算对照至少需要自适应模式的调用与 Token 额度")
        return self


def configuration():
    cfg = settings()
    policy_files = sorted((ROOT / "buildmedic").glob("*.py")) + sorted(
        p for p in (ROOT / "harnesses" / "base").rglob("*") if p.is_file()
    )
    return {
        "provider": cfg.model_provider,
        "model_id": cfg.model_id,
        "endpoint_sha256": hashlib.sha256(cfg.model_base_url.encode()).hexdigest(),
        "thinking": cfg.model_thinking,
        "max_output_tokens": cfg.max_output_tokens,
        "platform_max_model_calls": cfg.max_model_calls,
        "runner_backend": cfg.runner_backend,
        "runner_image": cfg.runner_image,
        "harness2_sha": HARNESS_SHA,
        "profile_version": "python-pytest-demo-v1",
        "policy_sha256": digest(
            {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in policy_files}
        ),
    }


def assert_runtime(binding):
    if digest(configuration()) != binding["configuration_sha256"]:
        raise Blocked("评测配置已变化，停止后续执行；请恢复原配置或新建对照")


def load(conn, eid, owner):
    row = (
        conn.execute(
            select(db.experiments)
            .where(db.experiments.c.id == eid, db.experiments.c.owner_id == owner)
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if not row:
        raise KeyError(eid)
    return row


def create(owner, key, spec):
    manifest = spec.model_dump(mode="json")
    request_hash = digest(manifest)
    with db.transaction() as conn:
        store.lock_owner(conn, owner)
        prior = (
            conn.execute(
                select(db.experiments).where(
                    db.experiments.c.owner_id == owner, db.experiments.c.idempotency_key == key
                )
            )
            .mappings()
            .first()
        )
        if prior:
            if prior["request_hash"] != request_hash:
                raise store.Conflict("同一个 Idempotency-Key 已用于不同评测清单")
            return prior["id"]
        if (
            conn.scalar(
                select(func.count()).select_from(db.experiments).where(db.experiments.c.owner_id == owner)
            )
            >= 50
        ):
            raise store.Conflict("当前会话评测记录已达上限")
        for case in spec.cases:
            if metadata(case.case_id)["snapshot_sha256"] != case.snapshot_sha256:
                raise store.Conflict("清单源码指纹与当前登记案例不一致")
        slots = []
        for repeat in range(spec.repeats):
            for index, case in enumerate(spec.cases):
                # Freeze alternating order before any task runs; no cherry-picked imports.
                modes = ("standard", "adaptive") if (repeat + index) % 2 == 0 else ("adaptive", "standard")
                for mode in modes:
                    slots.append(
                        {
                            "case_id": case.case_id,
                            "repeat": repeat + 1,
                            "mode": mode,
                            "task_id": None,
                            "frozen": None,
                        }
                    )
        eid = store.uid()
        config = configuration()
        conn.execute(
            db.experiments.insert().values(
                id=eid,
                owner_id=owner,
                idempotency_key=key,
                request_hash=request_hash,
                created_at=db.now(),
                status="planned",
                content={
                    "manifest": manifest,
                    "manifest_sha256": request_hash,
                    "configuration": config,
                    "configuration_sha256": digest(config),
                    "slots": slots,
                },
            )
        )
        return eid


def freeze(conn, task):
    snap = copy.deepcopy(task["snapshot"])
    reports = [a for a in snap["artifacts"] if a["kind"] == "report"]
    reference = None
    if reports:
        ref = reports[-1]
        art = (
            conn.execute(
                select(db.artifacts).where(
                    db.artifacts.c.id == ref["id"], db.artifacts.c.task_id == task["id"]
                )
            )
            .mappings()
            .first()
        )
        if not art or hashlib.sha256(art["content"].encode()).hexdigest() != ref["sha256"]:
            raise store.Conflict("任务结束报告缺失或指纹损坏，不能归档评测")
        snap = json.loads(art["content"])
        reference = {"artifact_id": ref["id"], "sha256": ref["sha256"]}
        if snap["id"] != task["id"] or snap["lifecycle"] != task["lifecycle"]:
            raise store.Conflict("任务报告与终态不匹配")
    return {"snapshot": snap, "sha256": digest(snap), "report": reference}


def advance(eid, owner, check_task=None):
    with db.transaction() as conn:
        store.lock_owner(conn, owner)
        row = load(conn, eid, owner)
        if row["status"] in {"completed", "cancelled"}:
            return {"status": row["status"], "task_id": None}
        content = copy.deepcopy(row["content"])
        spec = ExperimentSpec.model_validate(content["manifest"])
        for index, slot in enumerate(content["slots"]):
            if slot["frozen"]:
                continue
            if slot["task_id"]:
                task = store.row(conn, slot["task_id"], owner)
                if task["lifecycle"] not in TERMINAL or task["lease_token"]:
                    return {"status": "running", "task_id": task["id"]}
                slot["frozen"] = freeze(conn, task)
                continue
            assert_runtime(content)
            expected = next(c for c in spec.cases if c.case_id == slot["case_id"])
            meta = metadata(slot["case_id"])
            if meta["snapshot_sha256"] != expected.snapshot_sha256:
                raise store.Conflict("登记案例已变化，不能继续当前清单")
            meta["experiment_binding"] = {
                "id": eid,
                "slot": index,
                "manifest_sha256": content["manifest_sha256"],
                "configuration_sha256": content["configuration_sha256"],
            }
            body = CreateTask(
                source=Source(case_id=slot["case_id"]),
                mode=slot["mode"],
                limits=spec.limits,
                agent_tools=spec.agent_tools,
            ).model_dump()
            if check_task:
                check_task(CreateTask.model_validate(body))
            slot["task_id"] = store.create(owner, f"experiment-{eid}-{index}", body, meta, connection=conn)
            conn.execute(
                update(db.experiments)
                .where(db.experiments.c.id == eid)
                .values(status="running", content=content)
            )
            return {"status": "running", "task_id": slot["task_id"]}
        conn.execute(
            update(db.experiments)
            .where(db.experiments.c.id == eid)
            .values(status="completed", content=content)
        )
        return {"status": "completed", "task_id": None}


def cancel(eid, owner):
    with db.transaction() as conn:
        store.lock_owner(conn, owner)
        row = load(conn, eid, owner)
        slots = row["content"]["slots"]
        if row["status"] != "completed":
            conn.execute(update(db.experiments).where(db.experiments.c.id == eid).values(status="cancelled"))
        pending = [s["task_id"] for s in slots if s["task_id"] and not s["frozen"]]
    # Persist the stop first. Repeating cancel also retries cleanup after a process interruption.
    for tid in pending:
        store.cancel(tid, owner)


def observed(snap):
    """Recheck public evidence, without fabricating hidden behavioral labels."""
    from .orchestration import verification_ok

    selected = [c for c in snap["candidates"] if c.get("selected")]
    baseline = snap.get("baseline", [])
    executions = {e["id"]: e for e in snap["executions"]}
    verified = False
    for candidate in selected:
        checks = [
            v
            for v in snap["validations"]
            if v.get("kind") == "independent"
            and v["candidate_id"] == candidate["id"]
            and v["status"] == "passed"
        ]
        for v in checks:
            target, regression = v.get("target"), v.get("regression")
            if len(baseline) != 2 or not target or not regression:
                continue
            ids = {e["id"] for e in (*baseline, target, regression)}
            if len(ids) != 4 or any(executions.get(e["id"]) != e for e in (target, regression)):
                continue
            other = {
                e["id"]
                for check in snap["validations"]
                if check.get("kind") == "candidate"
                for e in (check.get("target"), check.get("regression"))
                if e
            }
            if ids & other or any(
                e.get("patch_hash") != candidate["patch_hash"] for e in (target, regression)
            ):
                continue
            ok, _ = verification_ok(baseline, target, regression)
            verified |= ok
    verified = bool(
        verified
        and snap["lifecycle"] == "completed"
        and snap["outcome"] == "repaired"
        and snap["reproduction_status"] == "reproduced"
    )
    usage = snap["usage"]
    duration = (snap.get("finished_at") or snap["updated_at"]) - snap.get("started_at", snap["created_at"])
    return {
        "lifecycle": snap["lifecycle"],
        "outcome": snap["outcome"],
        "public_verified": verified,
        "reproduced": snap["reproduction_status"] == "reproduced",
        "calls": usage["calls"],
        "tokens": usage["tokens"],
        "usage_known": usage.get("usage_known", False),
        "cost_usd": usage.get("cost_usd"),
        "accounted_tokens": usage.get("accounted_tokens", usage["tokens"]),
        "wall_seconds": round(max(0, duration), 3),
        "checks": len(snap["executions"]),
        "rollouts": len(snap["rollouts"]),
        "hidden_verified": None,
        "harness2_sha": snap.get("harness2_sha"),
        "harness_hashes": sorted({r["harness_hash"] for r in snap["rollouts"] if r.get("harness_hash")}),
        "models": sorted({c["model"] for c in snap.get("model_calls", [])}),
        "response_models": sorted(
            {c["response_model"] for c in snap.get("model_calls", []) if c.get("response_model")}
        ),
        "environment_fingerprints": sorted(
            {e["environment_fingerprint"] for e in baseline if e.get("environment_fingerprint")}
        ),
        "error_code": snap.get("error_code"),
    }


def render(row, include_rows=True):
    content = row["content"]
    rows = []
    for slot in content["slots"]:
        record = {k: v for k, v in slot.items() if k != "frozen"}
        frozen = slot["frozen"]
        record["metrics"] = None
        if frozen:
            if digest(frozen["snapshot"]) != frozen["sha256"]:
                raise store.Conflict("评测归档指纹校验失败")
            record.update(
                metrics=observed(frozen["snapshot"]),
                snapshot_sha256=frozen["sha256"],
                report=frozen["report"],
            )
        rows.append(record)
    aggregates = []
    for mode in ("standard", "adaptive"):
        group = [r for r in rows if r["mode"] == mode]
        complete = [r["metrics"] for r in group if r["metrics"]]
        costs = [r["cost_usd"] for r in complete]
        known_cost = sum(costs) if costs and all(c is not None for c in costs) else None
        verified = sum(r["public_verified"] for r in complete)
        tokens = sum(r["tokens"] for r in complete)
        aggregates.append(
            {
                "mode": mode,
                "planned": len(group),
                "finished": len(complete),
                "public_verified": verified,
                "reproduced": sum(r["reproduced"] for r in complete),
                "calls": sum(r["calls"] for r in complete),
                "tokens": tokens,
                "unknown_usage": sum(not r["usage_known"] for r in complete),
                "cost_usd": known_cost,
                "cost_per_public_verified": known_cost / verified
                if known_cost is not None and verified
                else None,
                "tokens_per_public_verified": tokens / verified
                if verified and all(r["usage_known"] for r in complete)
                else None,
                "wall_seconds": round(sum(r["wall_seconds"] for r in complete), 3),
                "checks": sum(r["checks"] for r in complete),
                "rollouts": sum(r["rollouts"] for r in complete),
                "blocked": sum(r["outcome"] == "blocked" for r in complete),
                "budget_exhausted": sum(r["outcome"] == "budget_exhausted" for r in complete),
                "interrupted": sum(
                    r["lifecycle"] in {"failed", "interrupted", "cancelled"} for r in complete
                ),
            }
        )
    environments = {f for row in rows if row["metrics"] for f in row["metrics"]["environment_fingerprints"]}
    public = {
        "id": row["id"],
        "created_at": row["created_at"],
        "status": row["status"],
        "manifest": content["manifest"],
        "manifest_sha256": content["manifest_sha256"],
        "configuration": content["configuration"],
        "configuration_sha256": content["configuration_sha256"],
        "summary": aggregates,
        "finished": sum(r["metrics"] is not None for r in rows),
        "planned": len(rows),
        "scope": "公开开发集与公开回归检查；未评测隐藏行为或泛化收益",
        "environment_consistent": len(environments) == 1 if environments else None,
        "rows": rows if include_rows else [],
    }
    public["report_sha256"] = digest(public)
    return public


def get(eid, owner):
    with db.transaction() as conn:
        row = dict(load(conn, eid, owner))
        if row["status"] == "cancelled":
            content = copy.deepcopy(row["content"])
            changed = False
            for slot in content["slots"]:
                if slot["task_id"] and not slot["frozen"]:
                    task = store.row(conn, slot["task_id"], owner)
                    if task["lifecycle"] in TERMINAL and not task["lease_token"]:
                        slot["frozen"] = freeze(conn, task)
                        changed = True
            if changed:
                conn.execute(update(db.experiments).where(db.experiments.c.id == eid).values(content=content))
                row["content"] = content
        return render(row)


def export_report(eid, owner):
    get(eid, owner)  # Settle any now-terminal cancellation before taking one consistent export.
    with db.transaction() as conn:
        row = load(conn, eid, owner)
        report = render(row)
        report["inputs"] = [slot["frozen"] for slot in row["content"]["slots"]]
        return report


def recompute(report):
    """Rebuild the exact public report from its frozen inputs without network or model access."""
    public = {key: value for key, value in report.items() if key != "inputs"}
    inputs = report["inputs"]
    if len(inputs) != len(public["rows"]):
        raise store.Conflict("报告的输入数量不匹配")
    slots = []
    for row, frozen in zip(public["rows"], inputs):
        if frozen and (
            frozen["snapshot"]["id"] != row["task_id"] or frozen["sha256"] != row["snapshot_sha256"]
        ):
            raise store.Conflict("报告的冻结输入与任务归属不匹配")
        slots.append({key: row[key] for key in ("case_id", "repeat", "mode", "task_id")} | {"frozen": frozen})
    content = {
        key: public[key] for key in ("manifest", "manifest_sha256", "configuration", "configuration_sha256")
    }
    if (
        digest(content["manifest"]) != content["manifest_sha256"]
        or digest(content["configuration"]) != content["configuration_sha256"]
    ):
        raise store.Conflict("报告的清单或配置指纹不匹配")
    content["slots"] = slots
    actual = render({key: public[key] for key in ("id", "status", "created_at")} | {"content": content})
    if actual != public:
        raise store.Conflict("报告统计与冻结输入复算结果不一致")
    return actual


def listing(owner, page=1, page_size=20):
    with db.transaction() as conn:
        where = db.experiments.c.owner_id == owner
        total = conn.scalar(select(func.count()).select_from(db.experiments).where(where))
        rows = conn.execute(
            select(db.experiments)
            .where(where)
            .order_by(db.experiments.c.created_at.desc(), db.experiments.c.id)
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).mappings()
        return {
            "items": [render(r, include_rows=False) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
