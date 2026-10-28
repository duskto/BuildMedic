"""Verify pinned Harness² and optionally run its deterministic local demo (no model)."""

import argparse
import json
import os
import subprocess
import sys

from buildmedic.config import ROOT
from buildmedic.harness_support import capability


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    status = capability()
    print(json.dumps(status, ensure_ascii=False))
    if not status["installed"]:
        return 1
    if args.demo:
        root = ROOT / "var" / "harness2-official-demo"
        root.mkdir(parents=True, exist_ok=True)
        clean_env = {
            key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "HOME", "SYSTEMROOT"}
        }
        result = subprocess.run(
            [sys.executable, "-m", "harness2.cli.demo", "--output", str(root), "--k", "2", "--mode", "both"],
            cwd=root,
            env=clean_env,
            check=False,
            timeout=60,
        )
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
