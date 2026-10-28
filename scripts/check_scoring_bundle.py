"""Operator-only validation of an external bundle; prints fingerprints, never private assertions."""

import argparse
from pathlib import Path

from scorer_service.bundle import load_bundle

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("bundle", type=Path)
args = parser.parse_args()
_, sha = load_bundle(args.bundle)
print("BUILDMEDIC_SCORER_SUITE_SHA256=" + sha)
