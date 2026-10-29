"""Execution of the preregistered log-only and independent fixed baselines."""

import hashlib
import shutil
import tempfile
from pathlib import Path

from . import db, store
from .errors import AppError, Blocked
from .evaluation import CapturedLog, captured_log, digest


def policy(ctx):
    binding = ctx.snap.get("experiment_binding", {})
    if binding.get("protocol") != "four-arm-v1":
        return None
    arm, attempts = binding.get("arm"), binding.get("attempts")
    if arm not in {"logs", "standard", "multi", "adaptive"} or type(attempts) is not int:
        raise Blocked("实验执行协议不完整")
    if (arm == "multi" and not 2 <= attempts <= 4) or (arm != "multi" and attempts != 1):
        raise Blocked("实验独立尝试次数不合规")
    if ctx.body["mode"] != ("adaptive" if arm == "adaptive" else "standard"):
        raise Blocked("实验组与任务执行模式不一致")
    record = CapturedLog.model_validate(ctx.snap.get("evaluation_log"))
    expected = captured_log(ctx.body["source"]["case_id"])
    if record != expected:
        raise Blocked("实验任务的日志不再匹配登记归档")
    if arm == "logs" and ctx.body.get("agent_tools"):
        raise Blocked("日志解释组不能使用工具循环")
    if binding.get("guidance_sha256"):
        from .frozen_guidance import model_material

        if ctx.body.get("experience") or (arm == "logs" and ctx.snap.get("frozen_guidance") is not None):
            raise Blocked("冻结指导实验不允许在线经验更新或向日志组提供开发指导")
        if arm != "logs" and model_material(ctx.snap) is None:
            raise Blocked("冻结指导实验缺少固定输入")
    return binding


def attach_log(ctx):
    record = ctx.snap["evaluation_log"]
    ctx.evidence("ci_log", record["log"], path=f"evaluation/public_logs/{record['case_id']}.json")


async def run_logs(ctx):
    ctx.phase("ingest", "读取固定的本地 Docker 历史日志，不执行代码")
    attach_log(ctx)
    ctx.update("reproduction.not_run", {"reproduction_status": "not_run"})
    ctx.phase("diagnose", "仅根据归档日志解释失败原因，诊断准确率尚未评分")
    materials = {
        "goal": "只解释日志中的失败类别、根因和可能位置。没有源码、实时测试或修复工具；不得生成补丁，certainty 不得为 supported。",
        "source": [],
        "logs": ctx.snap["evaluation_log"]["log"],
        "evidence": store.snapshot(ctx.id)["evidence"],
    }
    for attempt in range(2):
        try:
            diagnosis = await ctx.diagnose(materials, label=f"日志解释.{attempt + 1}")
            if diagnosis.patch or diagnosis.edits or diagnosis.root_cause.certainty == "supported":
                ctx.model_record(
                    store.snapshot(ctx.id)["model_calls"][-1]["id"],
                    "rejected",
                    error_code="log_only_delivery_rejected",
                )
                raise AppError("model_format_error", "日志解释组不得交付补丁或声称源码根因已验证", 502)
            ctx.update("diagnosis.created", {"diagnosis": diagnosis.model_dump(exclude={"patch", "edits"})})
            return "diagnosed_only", "日志诊断已交付；未读取源码、未执行代码、未评分诊断准确率"
        except AppError as exc:
            if exc.code not in {"model_format_error", "model_evidence_error"}:
                raise
            if attempt == 1:
                return "unresolved", "日志解释未通过结构或证据检查，未执行修复"
            materials["format_feedback"] = (
                "请使用有效证据 ID 交付无补丁诊断，certainty 为 hypothesis 或 insufficient。"
            )


async def run_fixed(ctx, case_id, materials, baseline, attempts):
    from .orchestration import SYSTEM_PROMPT, validate

    def finish(rid, **values):
        ctx.update(
            "rollout.finished",
            transform=lambda s: next(r for r in s["rollouts"] if r["id"] == rid).update(
                finished_at=db.now(), **values
            ),
        )

    passed = []
    for index in range(attempts):
        ctx.guard()
        if index:
            ctx.phase("diagnose", f"开始独立固定策略尝试 {index + 1}，使用相同初始材料")
        rollout = {
            "id": store.uid(),
            "label": f"独立固定策略 {index + 1}/{attempts}",
            "status": "running",
            "harness_hash": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            "input_sha256": digest(materials),
            "started_at": db.now(),
        }
        ctx.update("rollout.started", transform=lambda s, r=rollout: s["rollouts"].append(r))
        try:
            diagnosis = await ctx.diagnose(
                materials,
                label=rollout["label"],
                harness_hash=rollout["harness_hash"],
                rollout_id=rollout["id"],
            )
        except AppError as exc:
            if exc.code not in {"model_format_error", "model_evidence_error"}:
                raise
            finish(rollout["id"], status="format_error", error=exc.code)
            continue
        data = diagnosis.model_dump(exclude={"patch", "edits"})
        ctx.update("diagnosis.created", {"diagnosis": data})
        if not diagnosis.patch and not diagnosis.edits:
            finish(rollout["id"], status="diagnosed_only")
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="bm-evaluation-policy-") as temp:
                source = Path(temp) / "repo"
                shutil.copytree(
                    ctx.source_root(case_id), source, ignore=shutil.ignore_patterns("__pycache__")
                )
                candidate, _ = ctx.deliver_candidate(source, diagnosis, rollout["id"])
        except ValueError as exc:
            finish(rollout["id"], status="patch_rejected", error=str(exc))
            continue
        ctx.phase("validate", "检查本次独立候选，结果不传入其他尝试")
        result, _ = await validate(ctx, case_id, candidate, baseline, kind="candidate")
        finish(rollout["id"], status=result["status"])
        if result["status"] == "passed":
            passed.append((candidate, data))
    if not passed:
        if not store.snapshot(ctx.id)["candidates"] and store.snapshot(ctx.id).get("diagnosis"):
            return "diagnosed_only", "独立尝试未交付可检查的源码补丁"
        return "unresolved", "独立固定策略尝试均未通过公开候选检查"
    candidate, data = passed[0]
    ctx.update(
        "candidate.selected",
        {"diagnosis": data, "selection_summary": "采用首个通过公开检查的独立候选；未使用隐藏标准"},
        transform=lambda s: [c.update(selected=c["id"] == candidate["id"]) for c in s["candidates"]],
    )
    independent, _ = await validate(ctx, case_id, candidate, baseline)
    ctx.update("validation.finished", {"validation_status": independent["status"]})
    return (
        ("repaired", "选中补丁通过最终独立公开检查")
        if independent["status"] == "passed"
        else ("unresolved", "最终独立检查未通过")
    )
