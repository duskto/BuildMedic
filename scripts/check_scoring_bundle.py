"""Operator-only validation of an external bundle; prints fingerprints, never private assertions."""

import argparse
from pathlib import Path

from scorer_service.bundle import load_bundle

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
args = parser.parse_args()
try:
    _, sha = load_bundle(args.bundle)
except (ValueError, OSError):
    parser.exit(2, "评分包校验失败；请在受信任评分环境核对结构、登记入口、指纹与外部路径。\n")
print("BUILDMEDIC_SCORER_SUITE_SHA256=" + sha)
