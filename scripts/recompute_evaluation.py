"""Verify and recompute an exported comparison using only its frozen inputs."""

import argparse
import json
from pathlib import Path

from buildmedic.evaluation import recompute


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = recompute(json.loads(args.report.read_text()))
    print(f"复算一致：{report['id']}，{report['finished']}/{report['planned']} 项；{report['report_sha256']}")
    for item in report["summary"]:
        result = (
            f"诊断交付 {item['diagnoses']}/{item['planned']}（未评分，不适用修复率）"
            if item.get("repair_applicable") is False
            else f"公开通过 {item['public_verified']}/{item['planned']}"
        )
        print(f"{item['mode']}: {result}，{item['calls']} 次调用，{item['tokens']} 已知 Token")


if __name__ == "__main__":
    main()
