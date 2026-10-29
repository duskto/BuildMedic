"""Internal Runner. Run separately from API/Worker; never exposes a shell API."""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException

from .cases import CASES, case_dir, source_hash
from .config import settings
from .errors import Blocked
from .patching import apply_patch
from .profiles import registered
from .runner_runtime import DockerContainer, ExecutionTermination, classify_exit
from .schemas import ExecutionRequest, RemoteSnapshot
from .security import sanitize
from .snapshots import SnapshotStore, protected_hash

MAX_EXECUTION_SECONDS = 180
MAX_OUTPUT_BYTES = 2_000_000
DOCKER_MEMORY = "1g"


def parse_junit(path):
    if not path.is_file() or path.stat().st_size > 2_000_000:
        return None
    data = path.read_text()
    if "<!DOCTYPE" in data or "<!ENTITY" in data:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    cases = root.findall(".//testcase")
    result = {"collected": len(cases), "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "tests": []}
    for case in cases:
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        state = (
            "failed"
            if case.find("failure") is not None
            else "errors"
            if case.find("error") is not None
            else "skipped"
            if case.find("skipped") is not None
            else "passed"
        )
        result[state] += 1
        result["tests"].append({"id": name, "status": state})
    return result


class Engine:
    def __init__(self, backend=None, image=None, data_dir=None):
        self.backend = backend or settings().runner_backend
        self.image = image if image is not None else settings().runner_image
        self.root = Path(data_dir or settings().data_dir / "runner").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshots = SnapshotStore(self.root / "snapshots")
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.lock = threading.RLock()
        self.jobs, self.processes, self.containers, self.stop_reasons = {}, {}, {}, {}
        for file in self.root.glob("*.json"):
            value = json.loads(file.read_text())
            pending = value.get("termination", {}).get("cleanup_confirmed") is False
            if value["lifecycle"] in {"queued", "running"} or pending:
                termination = (
                    ExecutionTermination.model_validate(value["termination"])
                    if pending
                    else ExecutionTermination(cause="runner_restarted")
                )
                if self.backend == "docker":
                    container = self.container(value)
                    termination.cleanup_confirmed, termination.cleanup_error = container.cleanup()
                else:
                    # A previous trusted-local process cannot safely be identified by a reused PID.
                    termination.cleanup_confirmed = value["lifecycle"] == "queued"
                    if not termination.cleanup_confirmed:
                        termination.cleanup_error = "local_process_unconfirmed"
                if not self.cleanup_workspace(value):
                    termination.cleanup_confirmed = False
                    termination.cleanup_error = "workspace_cleanup_failed"
                value.update(
                    lifecycle="interrupted",
                    reason=value.get("reason", "cleanup_unconfirmed") if pending else "runner_restarted",
                    passed=False,
                    termination=termination.model_dump(),
                    finished_at=time.time(),
                )
                self._save(value)
            self.jobs[value["id"]] = value

    def ready(self):
        with self.lock:
            if any(j.get("termination", {}).get("cleanup_confirmed") is False for j in self.jobs.values()):
                return False, "存在尚未确认清理的执行，请恢复 Runner 并重试清理"
        if self.backend == "trusted-local":
            return True, "可信案例开发模式：本机进程执行，无容器隔离"
        if self.backend != "docker" or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image):
            return False, "尚未配置固定 sha256 镜像；请按 README 构建 Runner 镜像"
        try:
            p = subprocess.run(
                ["docker", "image", "inspect", self.image], capture_output=True, timeout=5, check=False
            )
            return (True, "固定镜像可用") if p.returncode == 0 else (False, "Docker 或固定镜像不可用")
        except (OSError, subprocess.TimeoutExpired):
            return False, "无法连接 Docker Runner"

    def _save(self, value):
        dest = self.root / (value["id"] + ".json")
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False))
        tmp.replace(dest)

    def submit(self, req):
        fingerprint = hashlib.sha256(req.model_dump_json(exclude_none=True).encode()).hexdigest()
        with self.lock:
            if req.execution_id in self.jobs:
                if self.jobs[req.execution_id]["request_hash"] != fingerprint:
                    raise ValueError("execution ID 已用于不同输入")
                return self.jobs[req.execution_id]
            if req.remote:
                self.remote_profile(req.remote.model_dump())
                self.snapshots.describe(req.remote.model_dump())
            elif req.case_id not in CASES or CASES[req.case_id].get("blocked"):
                raise ValueError("案例没有可执行的登记 profile")
            if sum(j["lifecycle"] in {"queued", "running"} for j in self.jobs.values()) >= 4:
                raise ValueError("Runner 执行队列已满")
            value = {
                "id": req.execution_id,
                "lifecycle": "queued",
                "request_hash": fingerprint,
                "created_at": time.time(),
            }
            self.jobs[req.execution_id] = value
            self._save(value)
            self.pool.submit(self._execute, req)
            return dict(value)

    def remote_profile(self, binding):
        profile = registered(binding)
        if self.backend != "docker" or self.image != profile.runtime_digest:
            raise ValueError("远程仓库仅能在登记的固定 Docker 镜像中运行")
        return profile

    def get(self, execution_id):
        with self.lock:
            return dict(self.jobs[execution_id])

    def container(self, value):
        return DockerContainer(
            self.root,
            value["id"],
            value["request_hash"],
            legacy=not value.get("container_labels_version"),
        )

    def cleanup_workspace(self, value):
        name = value.get("workspace_name")
        if not name:
            return True
        if not re.fullmatch(r"bm-run-[a-z0-9_]+", name):
            return False
        path = self.root / name
        if path.is_symlink():
            return False
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True

    def _stop(self, execution_id, reason):
        with self.lock:
            value = self.jobs.get(execution_id)
            if not value or value["lifecycle"] not in {"queued", "running"}:
                return
            self.stop_reasons.setdefault(execution_id, reason)
            proc = self.processes.get(execution_id)
            container = self.containers.get(execution_id)
        if proc and proc.poll() is None:
            if container:
                container.kill()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def cancel(self, execution_id):
        self._stop(execution_id, "user_cancelled")
        # Retrying cancellation also retries cleanup after a daemon outage or Runner restart.
        with self.lock:
            value = self.jobs.get(execution_id)
            pending = (
                value
                and value["lifecycle"] == "interrupted"
                and value.get("termination", {}).get("cleanup_confirmed") is False
            )
            container = self.container(value) if pending and self.backend == "docker" else None
        if container:
            cleaned, error = container.cleanup()
            if not self.cleanup_workspace(value):
                cleaned, error = False, "workspace_cleanup_failed"
            with self.lock:
                value["termination"].update(cleanup_confirmed=cleaned, cleanup_error=error)
                self._save(value)

    def _execute(self, req):
        eid = req.execution_id
        start = time.monotonic()
        work = Path(tempfile.mkdtemp(prefix="bm-run-", dir=self.root))
        value = dict(self.jobs[eid])
        termination = ExecutionTermination(state_confirmed=self.backend == "trusted-local")
        container, proc = None, None
        try:
            with self.lock:
                if eid in self.stop_reasons:
                    value.update(lifecycle="cancelled", reason=self.stop_reasons[eid], passed=False)
                    return
                value.update(lifecycle="running", started_at=time.time())
                value["workspace_name"] = work.name
                if self.backend == "docker":
                    value["container_labels_version"] = 1
                self.jobs[eid] = dict(value)
                self._save(value)
            ready, message = self.ready()
            if not ready:
                value.update(
                    lifecycle="completed",
                    reason="environment_blocked",
                    message=message,
                    exit_code=None,
                    passed=False,
                )
                return
            repo = work / "repo"
            profile = self.remote_profile(req.remote.model_dump()) if req.remote else None
            if profile:
                self.snapshots.describe(req.remote.model_dump())
                source = self.snapshots.path(req.remote.model_dump()) / "repo" / profile.workspace
            else:
                source = case_dir(req.case_id)
            shutil.copytree(
                source,
                repo,
                ignore=None if profile else shutil.ignore_patterns("__pycache__", ".pytest_cache"),
            )
            baseline_hash = profile.snapshot_sha256 if profile else source_hash(repo)
            tests_hash = protected_hash(repo) if profile else source_hash(repo / "tests")
            if req.patch:
                apply_patch(repo, req.patch)
            if (protected_hash(repo) if profile else source_hash(repo / "tests")) != tests_hash:
                raise ValueError("测试完整性发生变化")
            output_dir = work / "output"
            output_dir.mkdir(mode=0o777)
            output_dir.chmod(0o777)
            report = output_dir / "report.xml"
            case = CASES.get(req.case_id, {})
            target = (
                profile.target
                if profile
                else case.get("target", CASES.get(case.get("base_case", ""), {}).get("target"))
            )
            check = target if req.check_id == "target" else "tests"
            pytest_args = ["-m", "pytest", "-q", "-p", "no:cacheprovider", check]
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(repo / "src"),
                "HOME": str(work),
            }
            if self.backend == "trusted-local":
                argv = [sys.executable, *pytest_args, "--junitxml=" + str(report)]
            else:
                # Runner owns this socket. API, Worker and model environments never receive it.
                work.chmod(0o777)
                for path in repo.rglob("*"):
                    path.chmod(0o755 if path.is_dir() else 0o644)
                container = DockerContainer(self.root, eid, value["request_hash"])
                with self.lock:
                    self.containers[eid] = container
                argv = [
                    "docker",
                    "create",
                    "--name",
                    "bm-" + eid,
                    *container.label_args(),
                    "--log-driver",
                    "none",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--user",
                    "65534:65534",
                    "--memory",
                    DOCKER_MEMORY,
                    "--memory-swap",
                    DOCKER_MEMORY,
                    "--cpus",
                    "1",
                    "--pids-limit",
                    "128",
                    "--ulimit",
                    "fsize=8192:8192",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=64m",
                    "-v",
                    f"{repo.resolve()}:/work/repo:ro",
                    "-v",
                    f"{output_dir.resolve()}:/output:rw",
                    "-w",
                    "/work/repo",
                    "-e",
                    "PYTHONPATH=/work/repo/src",
                    "-e",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                    "-e",
                    "PYTHONDONTWRITEBYTECODE=1",
                    self.image,
                    "python",
                    *pytest_args,
                    "--junitxml=/output/report.xml",
                ]
                if not container.create(argv):
                    termination.cli_exit_code = container.create_exit_code
                    value.update(
                        lifecycle="completed",
                        reason="runtime_error",
                        passed=False,
                        exit_code=None,
                        message="Docker 无法创建登记的执行容器",
                    )
                    return
                argv = ["docker", "start", "--attach", container.container_id or container.name]
            logfile = work / "stdout.log"
            with logfile.open("w+b") as output:
                with self.lock:
                    if eid in self.stop_reasons:
                        value.update(lifecycle="cancelled", reason=self.stop_reasons[eid], passed=False)
                        return
                    proc = subprocess.Popen(
                        argv,
                        cwd=repo,
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    self.processes[eid] = proc
                reason = "exited"
                while proc.poll() is None:
                    if (
                        time.monotonic() - start > MAX_EXECUTION_SECONDS
                        or logfile.stat().st_size > MAX_OUTPUT_BYTES
                    ):
                        reason = (
                            "timeout" if time.monotonic() - start > MAX_EXECUTION_SECONDS else "output_limit"
                        )
                        self._stop(eid, reason)
                        break
                    time.sleep(0.1)
                proc.wait(timeout=10)
                if logfile.stat().st_size > MAX_OUTPUT_BYTES:
                    self._stop(eid, "output_limit")
                output.seek(0)
                text = sanitize(output.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace"))
            state = None
            if container:
                _, state = container.inspect()
            reason = classify_exit(
                self.backend, proc.returncode, termination, state, self.stop_reasons.get(eid)
            )
            counts = parse_junit(report)
            import importlib.metadata

            limits = {
                "wall_seconds": MAX_EXECUTION_SECONDS,
                "output_bytes": MAX_OUTPUT_BYTES,
                "memory": DOCKER_MEMORY if container else None,
                "memory_swap": DOCKER_MEMORY if container else None,
                "cpus": 1 if container else None,
                "pids": 128 if container else None,
            }
            environment = {
                "backend": self.backend,
                "image": self.image or None,
                "python": sys.version if self.backend == "trusted-local" else None,
                "pytest": importlib.metadata.version("pytest") if self.backend == "trusted-local" else None,
                "limits": limits,
            }
            environment_fingerprint = hashlib.sha256(
                json.dumps(environment, sort_keys=True).encode()
            ).hexdigest()
            value.update(
                lifecycle="cancelled" if reason == "user_cancelled" else "completed",
                reason=reason,
                check_id=req.check_id,
                command=["python", *pytest_args],
                image_digest=self.image or None,
                python_version=sys.version.split()[0] if self.backend == "trusted-local" else None,
                profile_id=profile.id if profile else "python-pytest-demo-v1",
                profile_hash=profile.digest if profile else None,
                head_sha=profile.head_sha if profile else None,
                environment_fingerprint=environment_fingerprint,
                snapshot_sha256=baseline_hash,
                tests_sha256=tests_hash,
                patch_hash=hashlib.sha256(req.patch.encode()).hexdigest(),
                exit_code=termination.container_exit_code if container else proc.returncode,
                counts=counts,
                stdout=text,
                duration_seconds=round(time.monotonic() - start, 3),
                scope="登记 profile 内的本地验证",
                isolation=self.backend,
                resource_limits=limits,
                passed=proc.returncode == 0
                and reason == "exited"
                and bool(
                    counts
                    and counts["collected"] > 0
                    and counts["passed"] > 0
                    and counts["failed"] == counts["errors"] == 0
                ),
            )
        except Exception:  # noqa: BLE001 - Persist execution failure before releasing the worker slot.
            value.update(
                lifecycle="completed",
                reason="runner_error",
                message="Runner 无法完成执行，请检查执行节点状态",
                exit_code=None,
                passed=False,
            )
        finally:
            # A test can fork children and let its parent exit; always clean the process group.
            process_cleaned = True
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    process_cleaned = False
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process_cleaned = False
            if container:
                termination.cleanup_confirmed, termination.cleanup_error = container.cleanup()
            else:
                termination.cleanup_confirmed = True
            if not process_cleaned:
                termination.cleanup_confirmed = False
                termination.cleanup_error = "process_cleanup_failed"
            if not self.cleanup_workspace({"workspace_name": work.name}):
                termination.cleanup_confirmed = False
                termination.cleanup_error = "workspace_cleanup_failed"
            with self.lock:
                requested = self.stop_reasons.pop(eid, None)
                if requested:
                    value.update(
                        lifecycle="cancelled" if requested == "user_cancelled" else "completed",
                        reason=requested,
                        passed=False,
                    )
                termination.cause = value.get("reason", "runner_error")
                if not termination.cleanup_confirmed:
                    value.update(lifecycle="interrupted", reason="cleanup_unconfirmed", passed=False)
                value["termination"] = termination.model_dump()
                value["finished_at"] = time.time()
                self.processes.pop(eid, None)
                self.containers.pop(eid, None)
                self.jobs[eid] = value
                self._save(value)


app = FastAPI(title="BuildMedic internal Runner")
_engine = None


def engine():
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine


def authorize(authorization: str = Header(default="")):
    import hmac

    token = settings().runner_token
    if not token or not hmac.compare_digest(authorization, "Bearer " + token):
        raise HTTPException(401, "Runner authentication required")


@app.get("/runner/v1/health", dependencies=[Depends(authorize)])
def health():
    ready, message = engine().ready()
    return {
        "ready": ready,
        "message": message,
        "backend": engine().backend,
        "image_digest": engine().image or None,
    }


@app.post("/runner/v1/executions", status_code=202, dependencies=[Depends(authorize)])
def execute(req: ExecutionRequest):
    try:
        return engine().submit(req)
    except (ValueError, Blocked) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/runner/v1/snapshots", dependencies=[Depends(authorize)])
async def prepare_snapshot(req: RemoteSnapshot):
    try:
        engine().remote_profile(req.model_dump())
        return await engine().snapshots.prepare(req.model_dump())
    except (ValueError, Blocked) as exc:
        raise HTTPException(409, sanitize(str(exc))) from exc


@app.get("/runner/v1/executions/{eid}", dependencies=[Depends(authorize)])
def get_execution(eid: str):
    try:
        return engine().get(eid)
    except KeyError as exc:
        raise HTTPException(404, "Execution not found") from exc


@app.post("/runner/v1/executions/{eid}/cancel", dependencies=[Depends(authorize)])
def cancel_execution(eid: str):
    engine().cancel(eid)
    return get_execution(eid)
