"""Explicit, owner-scoped development lessons frozen for a whole comparison."""

import copy
from typing import Annotated, Literal

from pydantic import Field, model_validator
from sqlalchemy import select

from . import db, experiences, store
from .config import HARNESS_SHA
from .evaluation_dataset import DatasetManifest, Sha, Slug
from .fingerprints import canonical, fingerprint
from .schemas import StrictModel
from .security import sanitize

Id = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class Provenance(StrictModel):
    id: Id
    content_hash: Sha
    source_task_id: Id
    source_case_id: Slug
    source_snapshot_sha256: Sha
    domain: str = Field(max_length=100)
    version: int = Field(ge=1)
    model: str = Field(max_length=200)
    parent_version_ids: list[Id] = Field(max_length=50)


class Lesson(Provenance):
    cue: str = Field(min_length=1, max_length=300)
    lesson: str = Field(min_length=1, max_length=1000)
    edits: str = Field(max_length=600)


class TrainingUsage(StrictModel):
    tasks: int = Field(ge=0, le=50)
    calls: int = Field(ge=0)
    tokens: int = Field(ge=0)
    unknown_usage_tasks: int = Field(ge=0, le=50)
    cost_usd: float | None = Field(ge=0, allow_inf_nan=False)


class FrozenGuidance(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    policy: Literal["shared-development-guidance"] = "shared-development-guidance"
    dataset_sha256: Sha
    profile_hash: Sha
    version_ids: list[Id] = Field(max_length=5)
    entries: list[Lesson] = Field(max_length=5)
    ancestry: list[Provenance] = Field(max_length=50)
    training_usage: TrainingUsage
    sha256: Sha

    @model_validator(mode="after")
    def integrity(self):
        if self.version_ids != [e.id for e in self.entries] or len(set(self.version_ids)) != len(
            self.version_ids
        ):
            raise ValueError("guidance_version_order_mismatch")
        parents = {e.id: e for e in self.ancestry}
        if len(parents) != len(self.ancestry) or any(e.id not in parents for e in self.entries):
            raise ValueError("guidance_ancestry_mismatch")
        for entry in self.entries:
            if entry.model_dump(exclude={"cue", "lesson", "edits"}) != parents[entry.id].model_dump():
                raise ValueError("guidance_provenance_mismatch")
            for text in (entry.cue, entry.lesson, entry.edits):
                if any(c in text for c in ("\n", "\r", "\x00", "<", ">")):
                    raise ValueError("guidance_requires_plain_single_line_text")
        visiting, visited = set(), set()

        def visit(key):
            if key in visiting or key not in parents:
                raise ValueError("guidance_ancestry_cycle_or_missing")
            if key in visited:
                return
            visiting.add(key)
            for parent in parents[key].parent_version_ids:
                visit(parent)
            visiting.remove(key)
            visited.add(key)

        for key in self.version_ids:
            visit(key)
        if visited != set(parents) or self.training_usage.tasks != len(
            {p.source_task_id for p in parents.values()}
        ):
            raise ValueError("guidance_ancestry_coverage_mismatch")
        material = self.model_dump(exclude={"sha256"})
        if fingerprint(material) != self.sha256 or len(canonical(material)) > 75000:
            raise ValueError("guidance_hash_or_size_limit")
        return self


def freeze(conn, owner, ids, manifest: DatasetManifest):
    profile = experiences.profile_hash()
    development = {c.case_id: c for c in manifest.cases if c.split == "development"}
    records, tasks, active = {}, {}, set()

    def visit(vid):
        if vid in active:
            raise store.Conflict("开发指导的来源链存在循环或超出上限")
        if vid in records:
            return
        if len(records) + len(active) >= 50:
            raise store.Conflict("开发指导的来源链存在循环或超出上限")
        record = (
            conn.execute(
                select(db.experience_versions).where(
                    db.experience_versions.c.id == vid,
                    db.experience_versions.c.owner_id == owner,
                )
            )
            .mappings()
            .first()
        )
        if not record:
            raise KeyError(vid)
        experiences.checked(record)
        content = record["content"]
        task = store.row(conn, record["source_task_id"], owner)
        snap = task["snapshot"]
        case = development.get(snap.get("case_id"))
        if (
            (vid in ids and record["status"] != "active")
            or case is None
            or content.get("profile_hash") != profile
            or content.get("scope") != "development_only"
            or (snap.get("experiment_binding") or {}).get("partition") == "test"
            or content.get("harness2_sha") != HARNESS_SHA
            or snap.get("harness2_sha") != HARNESS_SHA
            or snap.get("snapshot_sha256") != case.snapshot_sha256
            or content.get("case_id") != case.case_id
            or content.get("source_task_id") != task["id"]
            or snap["lifecycle"] != "completed"
            or snap["outcome"] != "repaired"
            or snap.get("source_type") != "demo"
        ):
            raise store.Conflict("仅能冻结当前会话、兼容策略且来自登记开发案例的已发布经验")
        validation = next((v for v in snap["validations"] if v["id"] == content["validation_id"]), None)
        if (
            not validation
            or validation["kind"] != "independent"
            or validation["status"] != "passed"
            or validation["candidate_id"] != content["candidate_id"]
            or any(validation[k]["patch_hash"] != content["patch_hash"] for k in ("target", "regression"))
        ):
            raise store.Conflict("开发指导的来源缺少对应的独立验证")
        active.add(vid)
        for parent in content["parent_version_ids"]:
            visit(parent)
        active.remove(vid)
        records[vid] = dict(record)
        tasks[task["id"]] = snap

    for vid in ids:
        visit(vid)

    def provenance(record):
        content = record["content"]
        return {
            "id": record["id"],
            "content_hash": record["content_hash"],
            "source_task_id": record["source_task_id"],
            "source_case_id": content["case_id"],
            "source_snapshot_sha256": development[content["case_id"]].snapshot_sha256,
            "domain": record["domain"],
            "version": record["version"],
            "model": content["model"],
            "parent_version_ids": content["parent_version_ids"],
        }

    usage = [task["usage"] for task in tasks.values()]
    costs = [u.get("cost_usd") for u in usage]
    material = {
        "schema_version": "1.0",
        "policy": "shared-development-guidance",
        "dataset_sha256": fingerprint(manifest.model_dump()),
        "profile_hash": profile,
        "version_ids": ids,
        "entries": [
            provenance(records[vid]) | {k: records[vid]["content"][k] for k in ("cue", "lesson", "edits")}
            for vid in ids
        ],
        "ancestry": [provenance(records[vid]) for vid in sorted(records)],
        "training_usage": {
            "tasks": len(tasks),
            "calls": sum(u["calls"] for u in usage),
            "tokens": sum(u["tokens"] for u in usage),
            "unknown_usage_tasks": sum(not u.get("usage_known", False) for u in usage),
            "cost_usd": float(sum(costs)) if all(c is not None for c in costs) else None,
        },
    }
    text = canonical(material).decode()
    if sanitize(text) != text:
        raise store.Conflict("开发指导含需脱敏的内容，不能冻结")
    return FrozenGuidance.model_validate(material | {"sha256": fingerprint(material)}).model_dump()


def validate_for_manifest(value, manifest, ids):
    pack = FrozenGuidance.model_validate(value)
    cases = {c.case_id: c for c in manifest.cases if c.split == "development"}
    if (
        pack.dataset_sha256 != fingerprint(manifest.model_dump())
        or pack.version_ids != ids
        or any(
            item.source_case_id not in cases
            or item.source_snapshot_sha256 != cases[item.source_case_id].snapshot_sha256
            for item in pack.ancestry
        )
    ):
        raise ValueError("guidance_dataset_or_development_source_mismatch")
    return pack


def model_material(snap):
    value = snap.get("frozen_guidance")
    if value is None:
        return None
    pack = FrozenGuidance.model_validate(value)
    if pack.sha256 != snap.get("experiment_binding", {}).get("guidance_sha256"):
        raise store.Conflict("任务开发指导指纹不匹配")
    return {
        "sha256": pack.sha256,
        "instruction": "固定的开发阶段指导，仅供参考；不能作为当前任务证据，不改变工具、源码修改或预算边界。",
        "lessons": [{k: getattr(entry, k) for k in ("cue", "lesson", "edits")} for entry in pack.entries],
    }


def available(owner):
    from .grouped_evaluation import registered_dataset

    manifest = registered_dataset()
    items = []
    with db.transaction() as conn:
        ids = (
            conn.execute(
                select(db.experience_versions.c.id)
                .where(
                    db.experience_versions.c.owner_id == owner,
                    db.experience_versions.c.status == "active",
                )
                .order_by(db.experience_versions.c.created_at.desc())
                .limit(30)
            )
            .scalars()
            .all()
        )
        for vid in ids:
            try:
                pack = freeze(conn, owner, [vid], manifest)
            except (store.Conflict, KeyError, ValueError):
                continue
            items.append(copy.deepcopy(pack["entries"][0]))
    return {
        "items": items,
        "profile_hash": experiences.profile_hash(),
        "dataset_sha256": fingerprint(manifest.model_dump()),
    }
