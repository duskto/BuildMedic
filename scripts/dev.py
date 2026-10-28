"""Start the local API, Worker and internal Runner with group cleanup on exit."""

import argparse
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from buildmedic.database import upgrade_database

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trusted-local",
        action="store_true",
        help="explicitly execute trusted demo source and model patches as local processes, without container isolation",
    )
    parser.add_argument(
        "--web", action="store_true", help="also launch Vite (npm dependencies must be installed)"
    )
    args = parser.parse_args()
    upgrade_database()
    env = os.environ.copy()
    if not (ROOT / ".env").is_file():
        env.setdefault("BUILDMEDIC_RUNNER_TOKEN", secrets.token_hex(32))
    if args.trusted_local:
        env["BUILDMEDIC_RUNNER_BACKEND"] = "trusted-local"
        print(
            "Trusted-local mode: demo tests and model-authored patches run on this machine without container isolation.",
            flush=True,
        )
    commands = [
        [sys.executable, "-m", "uvicorn", "buildmedic.runner:app", "--host", "127.0.0.1", "--port", "8001"],
        [sys.executable, "-m", "uvicorn", "buildmedic.api:app", "--host", "127.0.0.1", "--port", "8000"],
        [sys.executable, "-m", "buildmedic.worker"],
    ]
    if args.web:
        commands.append(["npm", "run", "dev", "--prefix", str(ROOT / "apps/web")])
    children = []
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for cmd in commands:
            children.append(subprocess.Popen(cmd, cwd=ROOT, env=env, start_new_session=True))
        print(
            "API: http://127.0.0.1:8000/docs | Web: http://127.0.0.1:5173 (dev) or http://127.0.0.1:8000 (built)",
            flush=True,
        )
        while not stopped and all(p.poll() is None for p in children):
            time.sleep(0.2)
    finally:
        for proc in reversed(children):
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
        for proc in children:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    return 0 if stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
