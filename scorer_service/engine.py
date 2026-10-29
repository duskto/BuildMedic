"""Black-box Docker observations, compared to private assertions in this trusted process."""

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from buildmedic.cases import case_dir, source_hash
from buildmedic.patching import apply_patch
from buildmedic.scoring_protocol import canonical


class ObservationError(Exception):
    pass


def bounded_process(argv, payload, timeout=12):
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    output, position = bytearray(), 0
    try:
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            until = time.monotonic() + timeout
            while selector.get_map():
                if time.monotonic() >= until:
                    raise ObservationError("observation_timeout")
                for key, _ in selector.select(0.1):
                    if key.fileobj is process.stdin:
                        try:
                            position += os.write(process.stdin.fileno(), payload[position : position + 4096])
                        except BrokenPipeError:
                            position = len(payload)
                        if position == len(payload):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                    else:
                        data = os.read(process.stdout.fileno(), 4096)
                        if not data:
                            selector.unregister(process.stdout)
                        output.extend(data)
                        if len(output) > 64000:
                            raise ObservationError("observation_output_limit")
        if process.wait(timeout=2) != 0:
            raise ObservationError("observation_failed")
        return bytes(output)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if not stream.closed:
                stream.close()


class DockerObserver:
    def __init__(self, image, work_dir):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("独立行为评分只接受固定 Docker 镜像")
        self.image, self.work_dir = image, Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def observe(self, case_id, expected_source, probes, patch=None):
        if source_hash(case_dir(case_id)) != expected_source:
            raise ObservationError("source_changed")
        with tempfile.TemporaryDirectory(prefix="observation-", dir=self.work_dir) as temp:
            root = Path(temp) / "repo"
            shutil.copytree(
                case_dir(case_id), root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
            )
            if patch is not None:
                apply_patch(root, patch)
            src = root / "src"
            for p in [src, *src.rglob("*")]:
                if p.is_symlink():
                    raise ObservationError("source_symlink")
                p.chmod(0o755 if p.is_dir() else 0o644)
            calls = [{"entrypoint": p.entrypoint, "args": p.args, "kwargs": p.kwargs} for p in probes]
            payload = canonical({"case_id": case_id, "calls": calls})
            if len(payload) > 16000:
                raise ObservationError("observation_input_limit")
            name = "bm-score-" + uuid.uuid4().hex
            gateway = Path(__file__).with_name("probe.py").resolve()
            argv = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--user",
                "65534:65534",
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--pids-limit",
                "32",
                "--ulimit",
                "fsize=128:128",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "-v",
                f"{src.resolve()}:/candidate:ro",
                "-v",
                f"{gateway}:/probe.py:ro",
                "--entrypoint",
                "python",
                self.image,
                "-I",
                "/probe.py",
            ]
            try:
                answers = json.loads(bounded_process(argv, payload))
            finally:
                subprocess.run(
                    ["docker", "rm", "-f", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    check=False,
                )
            if not isinstance(answers, list) or len(answers) != len(probes):
                raise ObservationError("observation_protocol")
            results = []
            for answer, probe in zip(answers, probes):
                if not isinstance(answer, dict) or type(answer.get("ok")) is not bool:
                    raise ObservationError("observation_protocol")
                if answer.get("ok") is False and answer.get("exception") == "ObservationError":
                    raise ObservationError("observation_encoding")
                if probe.raises:
                    matched = answer.get("ok") is False and answer.get("exception") == probe.raises
                else:
                    matched = (
                        answer.get("ok") is True
                        and "value" in answer
                        and canonical(answer["value"]) == canonical(probe.expected)
                    )
                results.append(matched)
            return results


def evaluate(request, bundle, observer):
    cases = {c.case_id: c for c in bundle.cases}
    baselines, rows = {}, []
    for item in request.rows:
        row = {key: getattr(item, key) for key in ("index", "task_id", "case_id", "arm", "patch_sha256")}
        if request.schema_version == "2.0":
            row["repeat"] = item.repeat
        row.update(
            status="not_applicable" if item.arm == "logs" else "no_candidate",
            hidden_verified=None,
            new_regression=None,
            probes=0,
            baseline_failed=0,
        )
        if item.arm != "logs" and item.patch is not None:
            try:
                case = cases[item.case_id]
                if case.snapshot_sha256 != item.snapshot_sha256:
                    raise ObservationError("source_changed")
                if item.case_id not in baselines:
                    baselines[item.case_id] = observer.observe(
                        item.case_id, item.snapshot_sha256, case.probes
                    )
                before = baselines[item.case_id]
                row.update(probes=len(before), baseline_failed=sum(not x for x in before))
                if all(before):
                    row["status"] = "not_reproduced"
                else:
                    after = observer.observe(item.case_id, item.snapshot_sha256, case.probes, item.patch)
                    row.update(
                        status="passed" if all(after) else "failed",
                        hidden_verified=all(after),
                        new_regression=any(old and not new for old, new in zip(before, after))
                        if any(before)
                        else None,
                    )
            except Exception:  # noqa: BLE001 - All private assertion/observation errors remain inside this service.
                # Never return private vectors, outputs, exception text, paths, or stack traces.
                row.update(status="evaluation_error", hidden_verified=None, new_regression=None)
        rows.append(row)
    return rows
