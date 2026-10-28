"""Offline descriptive analysis, with frozen-input verification and no model requests."""

import argparse
import json
from pathlib import Path

from buildmedic.evaluation import recompute
from buildmedic.evaluation_analysis import analyze, recompute_analysis


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="原公开报告或独立分析报告")
    args = parser.parse_args()
    value = json.loads(args.report.read_text())
    if "source_report" in value:
        result = recompute_analysis(value)
    else:
        result = analyze(recompute(value))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
