"""Preregistered, owner-scoped development comparisons; never a hidden-test score."""

import copy
import hashlib
import json
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator
from sqlalchemy import func, select, update

from . import db, store
from .cases import EVALUATION_CASE_IDS, metadata
from .config import HARNESS_SHA, ROOT, settings
from .errors import Blocked
from .evaluation_dataset import DatasetManifest, Sha, Slug
from .schemas import TERMINAL, CreateTask, Limits, Source, StrictModel
from .scoring_protocol import fingerprint
from .security import sanitize


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


class FourArmCase(EvaluationCase):
    log_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class FourArmSpec(ExperimentSpec):
    schema_version: Literal["2.0"]
    cases: list[FourArmCase] = Field(min_length=1, max_length=3)
    multi_attempts: int = Field(4, ge=2, le=4, strict=True)


class GroupedCase(FourArmCase):
    case_id: Slug


class GroupedSpec(FourArmSpec):
    schema_version: Literal["3.0"]
    dataset: Literal["public-constructed-grouped"] = "public-constructed-grouped"
    partition: Literal["development", "test"]
    dataset_manifest: DatasetManifest
    dataset_sha256: Sha
    cases: list[GroupedCase] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def complete_partition(self):
        if self.dataset_manifest.purpose != "grouped-benchmark":
            raise ValueError("grouped_experiment_requires_benchmark")
        if fingerprint(self.dataset_manifest.model_dump()) != self.dataset_sha256:
            raise ValueError("dataset_hash_mismatch")
        expected = [
            c.model_dump(include={"case_id", "snapshot_sha256", "log_sha256"})
            for c in self.dataset_manifest.cases
            if c.split == self.partition
        ]
        if [c.model_dump() for c in self.cases] != expected:
            raise ValueError("complete_partition_required_without_cherry_picking")
        return self


ExperimentRequest = ExperimentSpec | FourArmSpec | GroupedSpec
SPEC_ADAPTER = TypeAdapter(ExperimentRequest)


def parse_spec(value):
    return SPEC_ADAPTER.validate_python(value)


def arms(spec):
    return (
        ("logs", "standard", "multi", "adaptive")
        if spec.schema_version in {"2.0", "3.0"}
        else ("standard", "adaptive")
    )


def arm_limits(spec, arm):
    if arm == "logs":
        return spec.limits.model_copy(
            update={"max_model_calls": 2, "max_tokens": min(spec.limits.max_tokens, 100000)}
        )
    return spec.limits


class CapturedLog(EvaluationCase):
    case_id: Slug
    schema_version: Literal["1.0"]
    origin: Literal["local-docker-target-capture"]
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    log_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    log: str = Field(min_length=1, max_length=16000)


def captured_log(case_id):
    if case_id not in EVALUATION_CASE_IDS:
        raise store.Conflict("未登记公开日志案例")
    record = CapturedLog.model_validate_json(
        (ROOT / "evaluation" / "public_logs" / (case_id + ".json")).read_text()
    )
    if (
        record.case_id != case_id
        or record.snapshot_sha256 != metadata(case_id)["snapshot_sha256"]
        or hashlib.sha256(record.log.encode()).hexdigest() != record.log_sha256
        or sanitize(record.log) != record.log
    ):
        raise store.Conflict("公开日志身份、源码指纹或内容校验失败")
    return record


def available_cases():
    return [
        {
            **metadata(case_id),
            "log_sha256": captured_log(case_id).log_sha256,
            "log_origin": "local-docker-target-capture",
        }
        for case_id in ("pagination", "imports", "signature")
    ]


def configuration():
    cfg = settings()
    policy_files = (
        sorted((ROOT / "buildmedic").glob("*.py"))
        + sorted((ROOT / "evaluation" / "public_logs").glob("*.json"))
        + [ROOT / "evaluation/grouped-dataset.json"]
        + sorted(p for p in (ROOT / "harnesses" / "base").rglob("*") if p.is_file())
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


def validate_grouped(spec):
    from .grouped_evaluation import registered_dataset

    try:
        registered = registered_dataset()
    except (ValueError, OSError) as exc:
        raise store.Conflict("登记分组数据集缺失或内容已变化") from exc
    if registered != spec.dataset_manifest:
        raise store.Conflict("仅接受管理员登记的完整分组数据集")
    cfg = settings()
    if cfg.runner_backend != "docker" or any(
        captured_log(c.case_id).image_digest != cfg.runner_image for c in spec.cases
    ):
        raise store.Conflict("分组对照要求与归档日志一致的固定 Docker 镜像")


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
        if spec.schema_version == "3.0":
            validate_grouped(spec)
        for case in spec.cases:
            if metadata(case.case_id)["snapshot_sha256"] != case.snapshot_sha256:
                raise store.Conflict("清单源码指纹与当前登记案例不一致")
            if (
                spec.schema_version in {"2.0", "3.0"}
                and captured_log(case.case_id).log_sha256 != case.log_sha256
            ):
                raise store.Conflict("清单日志指纹与公开归档不一致")
        slots = []
        for repeat in range(spec.repeats):
            for index, case in enumerate(spec.cases):
                # Freeze alternating order before any task runs; no cherry-picked imports.
                modes = arms(spec)
                offset = (repeat + index) % len(modes)
                modes = modes[offset:] + modes[:offset]
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
        spec = parse_spec(content["manifest"])
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
            if spec.schema_version == "3.0":
                validate_grouped(spec)
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
            if spec.schema_version == "3.0":
                group = next(c for c in spec.dataset_manifest.cases if c.case_id == slot["case_id"])
                meta["experiment_binding"].update(
                    dataset_sha256=spec.dataset_sha256,
                    partition=spec.partition,
                    repository_group=group.repository_group,
                    template_group=group.template_group,
                )
            if spec.schema_version in {"2.0", "3.0"}:
                record = captured_log(slot["case_id"])
                if record.log_sha256 != expected.log_sha256:
                    raise store.Conflict("公开归档日志已变化，不能继续当前清单")
                meta["experiment_binding"].update(
                    protocol="four-arm-v1",
                    arm=slot["mode"],
                    attempts=spec.multi_attempts if slot["mode"] == "multi" else 1,
                )
                meta["evaluation_log"] = record.model_dump()
            body = CreateTask(
                source=Source(case_id=slot["case_id"]),
                mode="adaptive" if slot["mode"] == "adaptive" else "standard",
                limits=arm_limits(spec, slot["mode"]),
                agent_tools=spec.agent_tools and slot["mode"] != "logs",
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
    four_arm = content["manifest"]["schema_version"] in {"2.0", "3.0"}
    spec = parse_spec(content["manifest"]) if four_arm else None
    if content["manifest"]["schema_version"] == "3.0":
        expected = []
        modes = arms(spec)
        for repeat in range(spec.repeats):
            for index, case in enumerate(spec.cases):
                offset = (repeat + index) % len(modes)
                expected.extend((case.case_id, repeat + 1, mode) for mode in modes[offset:] + modes[:offset])
        actual = [(s["case_id"], s["repeat"], s["mode"]) for s in content["slots"]]
        ids = [s["task_id"] for s in content["slots"] if s["task_id"]]
        if actual != expected or len(ids) != len(set(ids)):
            raise store.Conflict("分组报告任务顺序、完整分区或任务身份不匹配")
    rows = []
    for slot_index, slot in enumerate(content["slots"]):
        record = {k: v for k, v in slot.items() if k != "frozen"}
        frozen = slot["frozen"]
        record["metrics"] = None
        if four_arm:
            record["limits"] = arm_limits(spec, slot["mode"]).model_dump()
        if frozen:
            if digest(frozen["snapshot"]) != frozen["sha256"]:
                raise store.Conflict("评测归档指纹校验失败")
            if content["manifest"]["schema_version"] == "3.0":
                snap = frozen["snapshot"]
                group = next(c for c in spec.dataset_manifest.cases if c.case_id == slot["case_id"])
                binding = snap.get("experiment_binding", {})
                log = snap.get("evaluation_log", {})
                required = {
                    "id": row["id"],
                    "slot": slot_index,
                    "manifest_sha256": content["manifest_sha256"],
                    "configuration_sha256": content["configuration_sha256"],
                    "dataset_sha256": spec.dataset_sha256,
                    "partition": spec.partition,
                    "repository_group": group.repository_group,
                    "template_group": group.template_group,
                    "protocol": "four-arm-v1",
                    "arm": slot["mode"],
                    "attempts": spec.multi_attempts if slot["mode"] == "multi" else 1,
                }
                if (
                    binding != required
                    or snap["id"] != slot["task_id"]
                    or snap.get("case_id") != group.case_id
                    or snap["snapshot_sha256"] != group.snapshot_sha256
                    or log.get("case_id") != group.case_id
                    or log.get("snapshot_sha256") != group.snapshot_sha256
                    or log.get("log_sha256") != group.log_sha256
                    or hashlib.sha256(log.get("log", "").encode()).hexdigest() != group.log_sha256
                    or log.get("image_digest") != content["configuration"]["runner_image"]
                ):
                    raise store.Conflict("分组任务归档与固定来源、日志或执行协议不匹配")
            record.update(
                metrics=observed(frozen["snapshot"]),
                snapshot_sha256=frozen["sha256"],
                report=frozen["report"],
            )
            if four_arm:
                record["metrics"]["diagnosis_delivered"] = bool(frozen["snapshot"].get("diagnosis"))
        rows.append(record)
    aggregates = []
    for mode in ("logs", "standard", "multi", "adaptive") if four_arm else ("standard", "adaptive"):
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
        if four_arm:
            aggregates[-1].update(
                repair_applicable=mode != "logs",
                diagnosis_accuracy=None,
                diagnoses=sum(r["diagnosis_delivered"] for r in complete),
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
    if content["manifest"]["schema_version"] == "3.0":
        public["scope"] = (
            "公开构造任务的分组对照；分区为 "
            + content["manifest"]["partition"]
            + "；未证明真实仓库泛化或独立行为修复率"
        )
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
