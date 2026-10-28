import copy
import json
import subprocess
import sys
from math import sqrt

import pytest
from fastapi.testclient import TestClient

from buildmedic import evaluation, evaluation_analysis, store
from buildmedic.api import app


def spec(repeats=1, version="2.0"):
    log = evaluation.captured_log("pagination")
    case = {"case_id": log.case_id, "snapshot_sha256": log.snapshot_sha256}
    if version == "2.0":
        case["log_sha256"] = log.log_sha256
    return evaluation.parse_spec(
        {
            "schema_version": version,
            "name": "analysis-test",
            "cases": [case],
            "repeats": repeats,
        }
    )


def sign(report):
    report["report_sha256"] = evaluation.digest({k: v for k, v in report.items() if k != "report_sha256"})
    return report


def fixture_report():
    """Explicit synthetic metrics for exact descriptive arithmetic, not real repairs."""
    eid = evaluation.create("owner", "analysis-fixture", spec(repeats=3))
    report = evaluation.get(eid, "owner")
    outcomes = {"standard": [True, False, True], "multi": [True, True, True], "adaptive": [False, True, True]}
    for index, row in enumerate(report["rows"]):
        row["task_id"] = f"fixture-task-{index}"
        row["metrics"] = {
            "public_verified": outcomes.get(row["mode"], [False] * 3)[row["repeat"] - 1],
            "calls": 1 if row["mode"] == "standard" else 4,
            "tokens": 100 if row["mode"] == "standard" else 400,
            "usage_known": True,
            "wall_seconds": 1.25 if row["mode"] == "standard" else 4.5,
            "cost_usd": 0.0 if row["mode"] == "standard" else 0.25,
        }
    report.update(status="completed", finished=report["planned"])
    return sign(report)


def test_repeat_variation_and_pairing_keep_all_costs_and_do_not_score_logs():
    source = fixture_report()
    saved = copy.deepcopy(source)
    result = evaluation_analysis.analyze(source)
    assert source == saved  # Existing report hashes and inputs are never rewritten.
    assert result["source_report_sha256"] == source["report_sha256"]
    assert result["complete"] is True
    assert len(result["repeat_results"]) == 9
    standard = result["variation"][0]
    assert standard["public_rate"]["n"] == 3
    assert standard["public_rate"]["mean"] == pytest.approx(2 / 3)
    assert standard["public_rate"]["sample_stddev"] == pytest.approx(sqrt(1 / 3))
    assert standard["cost_usd"]["mean"] == 0
    multi, adaptive, multi_adaptive = result["pairs"]
    assert multi["public_rate_delta"] == pytest.approx(1 / 3)
    assert (
        adaptive["compared_only"],
        adaptive["reference_only"],
        adaptive["both_verified"],
        adaptive["neither_verified"],
    ) == (1, 1, 1, 0)
    assert adaptive["public_rate_delta"] == 0
    assert adaptive["calls_delta"] == 9 and adaptive["tokens_delta"] == 900
    assert adaptive["wall_seconds_delta"] == 9.75 and adaptive["cost_usd_delta"] == 0.75
    assert multi_adaptive["public_rate_delta"] == pytest.approx(-1 / 3)
    assert evaluation_analysis.EvaluationAnalysis.model_validate(result)
    assert result["analysis_sha256"] == evaluation.digest(
        {k: v for k, v in result.items() if k != "analysis_sha256"}
    )


def test_partial_rounds_are_pending_without_imputing_outcomes_or_zero_variance():
    report = fixture_report()
    missing = next(r for r in report["rows"] if r["mode"] == "adaptive" and r["repeat"] == 2)
    missing["metrics"] = None
    report.update(status="cancelled", finished=report["planned"] - 1)
    result = evaluation_analysis.analyze(sign(report))
    pair = result["pairs"][1]
    assert result["complete"] is False
    assert (pair["paired"], pair["pending"], pair["reference_only"]) == (2, 1, 1)
    assert pair["public_rate_delta"] is None
    assert pair["calls_delta"] == 6 and pair["tokens_delta"] == 600
    round_two = next(r for r in result["repeat_results"] if r["mode"] == "adaptive" and r["repeat"] == 2)
    assert round_two["public_rate"] is None and round_two["tokens"] is None
    assert result["variation"][2]["complete_repeats"] == 2
    assert result["variation"][2]["public_rate"]["mean"] == 0.5


def test_unknown_usage_and_cost_are_not_replaced_by_known_subtotals():
    report = fixture_report()
    unknown = next(r for r in report["rows"] if r["mode"] == "adaptive" and r["repeat"] == 2)
    unknown["metrics"].update(usage_known=False, cost_usd=None)
    result = evaluation_analysis.analyze(sign(report))
    pair = result["pairs"][1]
    assert pair["tokens_delta"] is None and pair["unknown_usage_pairs"] == 1
    assert pair["cost_usd_delta"] is None and pair["unknown_cost_pairs"] == 1
    assert pair["calls_delta"] == 9
    assert result["variation"][2]["tokens"]["n"] == 2
    assert result["variation"][2]["cost_usd"]["n"] == 2
    assert result["variation"][2]["public_rate"]["n"] == 3


def test_rotated_order_pairs_by_case_and_repeat_instead_of_zipping_rows():
    body = spec(repeats=2).model_dump()
    log = evaluation.captured_log("signature")
    body["cases"].append(
        {
            "case_id": log.case_id,
            "snapshot_sha256": log.snapshot_sha256,
            "log_sha256": log.log_sha256,
        }
    )
    eid = evaluation.create("owner", "two-case-analysis", evaluation.parse_spec(body))
    report = evaluation.get(eid, "owner")
    for index, row in enumerate(report["rows"]):
        row["task_id"] = f"fixture-task-{index}"
        row["metrics"] = {
            "public_verified": (row["mode"] == "standard") == (row["case_id"] == "pagination"),
            "calls": 1,
            "tokens": 10,
            "usage_known": True,
            "wall_seconds": 1,
            "cost_usd": 0,
        }
    report.update(status="completed", finished=report["planned"])
    result = evaluation_analysis.analyze(sign(report))
    pair = result["pairs"][1]
    assert (
        pair["compared_only"],
        pair["reference_only"],
        pair["both_verified"],
        pair["neither_verified"],
    ) == (2, 2, 0, 0)
    assert pair["paired"] == 4 and pair["public_rate_delta"] == 0
    assert all(r["planned"] == r["finished"] == 2 for r in result["repeat_results"])
    assert all(v["public_rate"]["sample_stddev"] == 0 for v in result["variation"])


@pytest.mark.parametrize("version", ["1.0", "2.0"])
def test_empty_analysis_and_single_repeat_have_no_variance(version):
    eid = evaluation.create("owner", "analysis-test", spec(version=version))
    empty = evaluation_analysis.analyze(evaluation.get(eid, "owner"))
    assert len(empty["pairs"]) == (3 if version == "2.0" else 1)
    assert all(v["public_rate"]["n"] == 0 and v["public_rate"]["mean"] is None for v in empty["variation"])
    while True:
        step = evaluation.advance(eid, "owner")
        if step["status"] == "completed":
            break
        store.cancel(step["task_id"], "owner")
    complete = evaluation_analysis.analyze(evaluation.get(eid, "owner"))
    assert complete["complete"] is True
    for variation in complete["variation"]:
        assert variation["public_rate"] == {
            "n": 1,
            "mean": 0,
            "minimum": 0,
            "maximum": 0,
            "sample_stddev": None,
        }
    assert all(p["neither_verified"] == 1 and p["public_rate_delta"] == 0 for p in complete["pairs"])
    bundle = evaluation_analysis.export_analysis(eid, "owner")
    assert evaluation_analysis.recompute_analysis(bundle) == complete
    bundle["source_report"]["inputs"][0]["snapshot"]["usage"]["tokens"] += 1
    with pytest.raises(store.Conflict, match="指纹"):
        evaluation_analysis.recompute_analysis(bundle)


@pytest.mark.parametrize("change", ["checksum", "duplicate", "missing", "order", "task_id"])
def test_analysis_rejects_tampered_identity_and_cherry_picked_rows(change):
    report = fixture_report()
    if change == "checksum":
        report["rows"][0]["metrics"]["tokens"] += 1
    elif change == "duplicate":
        report["rows"][1] = copy.deepcopy(report["rows"][0])
    elif change == "missing":
        report["rows"].pop()
    elif change == "order":
        report["rows"].reverse()
    else:
        report["rows"][1]["task_id"] = report["rows"][0]["task_id"]
    if change != "checksum":
        sign(report)
    with pytest.raises(store.Conflict):
        evaluation_analysis.analyze(report)


def test_analysis_export_is_owner_scoped_reproducible_and_detects_tampering(client, tmp_path):
    eid = client.post(
        "/api/v1/experiments", json=spec().model_dump(), headers={"Idempotency-Key": "analysis-api-test"}
    ).json()["data"]["id"]
    before = client.get(f"/api/v1/experiments/{eid}/report").json()
    result = client.get(f"/api/v1/experiments/{eid}/analysis")
    assert result.status_code == 200
    download = client.get(f"/api/v1/experiments/{eid}/analysis/report")
    assert "attachment" in download.headers["content-disposition"]
    bundle = download.json()
    assert bundle["source_report"] == before
    assert evaluation_analysis.recompute_analysis(bundle) == result.json()["data"]
    assert client.get("/api/v1/tasks").json()["data"]["total"] == 0
    assert client.get(f"/api/v1/experiments/{eid}/report").json() == before
    with TestClient(app) as other:
        for suffix in ("/analysis", "/analysis/report"):
            assert other.get(f"/api/v1/experiments/{eid}{suffix}").status_code == 404
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(bundle))
    process = subprocess.run(
        [sys.executable, "scripts/analyze_evaluation.py", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == bundle["analysis"]
    bundle["analysis"]["pairs"][0]["public_rate_delta"] = 1
    with pytest.raises(store.Conflict, match="复算"):
        evaluation_analysis.recompute_analysis(bundle)
