"""Descriptive repeat and paired analysis of an unchanged public evaluation report."""

from itertools import combinations
from statistics import mean, stdev
from typing import Literal

from pydantic import Field

from . import evaluation, store
from .schemas import StrictModel

Mode = Literal["standard", "multi", "adaptive"]


class Distribution(StrictModel):
    n: int
    mean: float | None
    minimum: float | None
    maximum: float | None
    sample_stddev: float | None


class RepeatResult(StrictModel):
    mode: Mode
    repeat: int
    planned: int
    finished: int
    public_verified: int
    public_rate: float | None
    calls: int
    tokens: int | None
    unknown_usage: int
    wall_seconds: float
    cost_usd: float | None


class RepeatVariation(StrictModel):
    mode: Mode
    planned_repeats: int
    complete_repeats: int
    public_rate: Distribution
    calls: Distribution
    tokens: Distribution
    wall_seconds: Distribution
    cost_usd: Distribution


class PairedResult(StrictModel):
    reference: Mode
    compared: Mode
    planned: int
    paired: int
    pending: int
    compared_only: int
    reference_only: int
    both_verified: int
    neither_verified: int
    public_rate_delta: float | None
    calls_delta: int
    tokens_delta: int | None
    unknown_usage_pairs: int
    wall_seconds_delta: float
    cost_usd_delta: float | None
    unknown_cost_pairs: int


class EvaluationAnalysis(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    experiment_id: str
    source_report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope: Literal["public-development-descriptive", "public-constructed-grouped-descriptive"] = (
        "public-development-descriptive"
    )
    complete: bool
    environment_consistent: bool | None
    repeat_results: list[RepeatResult]
    variation: list[RepeatVariation]
    pairs: list[PairedResult]
    analysis_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def distribution(values):
    values = [v for v in values if v is not None]
    return {
        "n": len(values),
        "mean": mean(values) if values else None,
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
        "sample_stddev": stdev(values) if len(values) > 1 else None,
    }


def analyze(report):
    """Use server-rendered public metrics; offline callers must first recompute inputs."""
    public = {k: v for k, v in report.items() if k not in {"inputs", "report_sha256"}}
    if evaluation.digest(public) != report["report_sha256"]:
        raise store.Conflict("分析来源报告指纹不匹配")
    spec = evaluation.parse_spec(report["manifest"])
    modes = evaluation.arms(spec)
    expected = []
    for repeat in range(spec.repeats):
        for index, case in enumerate(spec.cases):
            offset = (repeat + index) % len(modes)
            expected.extend((case.case_id, repeat + 1, mode) for mode in modes[offset:] + modes[:offset])
    rows = report["rows"]
    actual = [(r["case_id"], r["repeat"], r["mode"]) for r in rows]
    task_ids = [r["task_id"] for r in rows if r["task_id"]]
    if actual != expected or len(task_ids) != len(set(task_ids)):
        raise store.Conflict("分析来源逐项记录与预登记顺序或任务身份不匹配")
    repair_modes = [mode for mode in modes if mode != "logs"]
    repeats = []
    variations = []
    for mode in repair_modes:
        for repeat in range(1, spec.repeats + 1):
            group = [r for r in rows if r["mode"] == mode and r["repeat"] == repeat]
            metrics = [r["metrics"] for r in group if r["metrics"] is not None]
            verified = sum(m["public_verified"] for m in metrics)
            costs = [m["cost_usd"] for m in metrics]
            unknown = sum(not m["usage_known"] for m in metrics)
            repeats.append(
                {
                    "mode": mode,
                    "repeat": repeat,
                    "planned": len(group),
                    "finished": len(metrics),
                    "public_verified": verified,
                    "public_rate": verified / len(group) if len(metrics) == len(group) else None,
                    "calls": sum(m["calls"] for m in metrics),
                    "tokens": sum(m["tokens"] for m in metrics) if metrics and not unknown else None,
                    "unknown_usage": unknown,
                    "wall_seconds": round(sum(m["wall_seconds"] for m in metrics), 3),
                    "cost_usd": sum(costs) if costs and all(c is not None for c in costs) else None,
                }
            )
        complete = [r for r in repeats if r["mode"] == mode and r["finished"] == r["planned"]]
        variations.append(
            {
                "mode": mode,
                "planned_repeats": spec.repeats,
                "complete_repeats": len(complete),
                **{
                    key: distribution(r[key] for r in complete)
                    for key in ("public_rate", "calls", "tokens", "wall_seconds", "cost_usd")
                },
            }
        )
    indexed = {(r["case_id"], r["repeat"], r["mode"]): r["metrics"] for r in rows}
    pairs = []
    for reference, compared in combinations(repair_modes, 2):
        matched = []
        for repeat in range(1, spec.repeats + 1):
            for case in spec.cases:
                left = indexed[case.case_id, repeat, reference]
                right = indexed[case.case_id, repeat, compared]
                if left is not None and right is not None:
                    matched.append((left, right))
        planned = len(spec.cases) * spec.repeats
        wins = sum(not a["public_verified"] and b["public_verified"] for a, b in matched)
        losses = sum(a["public_verified"] and not b["public_verified"] for a, b in matched)
        unknown_usage = sum(not (a["usage_known"] and b["usage_known"]) for a, b in matched)
        unknown_cost = sum(a["cost_usd"] is None or b["cost_usd"] is None for a, b in matched)
        pairs.append(
            {
                "reference": reference,
                "compared": compared,
                "planned": planned,
                "paired": len(matched),
                "pending": planned - len(matched),
                "compared_only": wins,
                "reference_only": losses,
                "both_verified": sum(a["public_verified"] and b["public_verified"] for a, b in matched),
                "neither_verified": sum(
                    not a["public_verified"] and not b["public_verified"] for a, b in matched
                ),
                "public_rate_delta": (wins - losses) / planned if len(matched) == planned else None,
                "calls_delta": sum(b["calls"] - a["calls"] for a, b in matched),
                "tokens_delta": sum(b["tokens"] - a["tokens"] for a, b in matched)
                if matched and not unknown_usage
                else None,
                "unknown_usage_pairs": unknown_usage,
                "wall_seconds_delta": round(
                    sum(b["wall_seconds"] - a["wall_seconds"] for a, b in matched), 3
                ),
                "cost_usd_delta": sum(b["cost_usd"] - a["cost_usd"] for a, b in matched)
                if matched and not unknown_cost
                else None,
                "unknown_cost_pairs": unknown_cost,
            }
        )
    result = {
        "schema_version": "1.0",
        "experiment_id": report["id"],
        "source_report_sha256": report["report_sha256"],
        "scope": "public-constructed-grouped-descriptive"
        if spec.schema_version == "3.0"
        else "public-development-descriptive",
        "complete": report["status"] == "completed" and report["finished"] == report["planned"],
        "environment_consistent": report["environment_consistent"],
        "repeat_results": repeats,
        "variation": variations,
        "pairs": pairs,
    }
    normalized = EvaluationAnalysis.model_validate({**result, "analysis_sha256": "0" * 64}).model_dump()
    normalized["analysis_sha256"] = evaluation.digest(
        {k: v for k, v in normalized.items() if k != "analysis_sha256"}
    )
    return normalized


def export_analysis(eid, owner):
    source = evaluation.export_report(eid, owner)
    return {"analysis": analyze(source), "source_report": source}


def recompute_analysis(bundle):
    source = evaluation.recompute(bundle["source_report"])
    actual = analyze(source)
    if actual != bundle["analysis"]:
        raise store.Conflict("分析统计与冻结输入复算结果不一致")
    return actual
