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
from .patching import apply_patch
from .schemas import ExecutionRequest
from .security import sanitize


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
    result = dict(collected=len(cases), passed=0, failed=0, errors=0, skipped=0, tests=[])
    for case in cases:
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        state = "failed" if case.find("failure") is not None else "errors" if case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"
        result[state] += 1
        result["tests"].append({"id": name, "status": state})
    return result


class Engine:
    def __init__(self, backend=None, image=None, data_dir=None):
        self.backend = backend or settings().runner_backend
        self.image = image if image is not None else settings().runner_image
        self.root = Path(data_dir or settings().data_dir / "runner")
        self.root.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.lock = threading.RLock()
        self.jobs, self.processes, self.cancelled = {}, {}, set()
        for file in self.root.glob("*.json"):
            value = json.loads(file.read_text())
            if value["lifecycle"] in {"queued", "running"}:
                value.update(lifecycle="interrupted", reason="runner_restarted")
                self._save(value)
            self.jobs[value["id"]] = value

    def ready(self):
        if self.backend == "trusted-local":
            return True, "可信案例开发模式：本机进程执行，无容器隔离"
        if self.backend != "docker" or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image):
            return False, "尚未配置固定 sha256 镜像；请按 README 构建 Runner 镜像"
        try:
            p = subprocess.run(["docker", "image", "inspect", self.image], capture_output=True, timeout=5)
            return (True, "固定镜像可用") if p.returncode == 0 else (False, "Docker 或固定镜像不可用")
        except (OSError, subprocess.TimeoutExpired):
            return False, "无法连接 Docker Runner"

    def _save(self, value):
        dest = self.root / (value["id"] + ".json")
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False))
        tmp.replace(dest)

    def submit(self, req):
        fingerprint = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
        with self.lock:
            if req.execution_id in self.jobs:
                if self.jobs[req.execution_id]["request_hash"] != fingerprint:
                    raise ValueError("execution ID 已用于不同输入")
                return self.jobs[req.execution_id]
            if req.case_id not in CASES or CASES[req.case_id].get("blocked"):
                raise ValueError("案例没有可执行的登记 profile")
            if sum(j["lifecycle"] in {"queued", "running"} for j in self.jobs.values()) >= 4:
                raise ValueError("Runner 执行队列已满")
            value = dict(id=req.execution_id, lifecycle="queued", request_hash=fingerprint, created_at=time.time())
            self.jobs[req.execution_id] = value
            self._save(value)
            self.pool.submit(self._execute, req)
            return dict(value)

    def get(self, execution_id):
        with self.lock:
            return dict(self.jobs[execution_id])

    def cancel(self, execution_id):
        with self.lock:
            self.cancelled.add(execution_id)
            proc = self.processes.get(execution_id)
            if proc and proc.poll() is None:
                if self.backend == "docker":
                    subprocess.run(["docker", "kill", "bm-" + execution_id], capture_output=True, timeout=10)
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            value = self.jobs.get(execution_id)
            if value and value["lifecycle"] == "queued":
                value.update(lifecycle="cancelled", reason="user_cancelled")
                self._save(value)

    def _execute(self, req):
        eid = req.execution_id
        start = time.monotonic()
        work = Path(tempfile.mkdtemp(prefix="bm-run-", dir=self.root))
        value = self.jobs[eid]
        try:
            with self.lock:
                if eid in self.cancelled:
                    return
                value.update(lifecycle="running", started_at=time.time())
                self._save(value)
            ready, message = self.ready()
            if not ready:
                value.update(lifecycle="completed", reason="environment_blocked", message=message, exit_code=None)
                return
            repo = work / "repo"
            shutil.copytree(case_dir(req.case_id), repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            baseline_hash = source_hash(repo)
            tests_hash = source_hash(repo / "tests")
            if req.patch:
                apply_patch(repo, req.patch)
            if source_hash(repo / "tests") != tests_hash:
                raise ValueError("测试完整性发生变化")
            report = work / "report.xml"
            case = CASES[req.case_id]
            target = case.get("target", CASES.get(case.get("base_case", ""), {}).get("target"))
            check = target if req.check_id == "target" else "tests"
            pytest_args = ["-m", "pytest", "-q", "-p", "no:cacheprovider", check]
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(repo / "src"), "HOME": str(work)}
            if self.backend == "trusted-local":
                argv = [sys.executable, *pytest_args, "--junitxml=" + str(report)]
            else:
                # Runner owns this socket. API, Worker and model environments never receive it.
                work.chmod(0o777)
                for path in repo.rglob("*"):
                    path.chmod(0o755 if path.is_dir() else 0o644)
                argv = ["docker", "run", "--rm", "--name", "bm-" + eid, "--network", "none", "--read-only",
                        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", "65534:65534",
                        "--memory", "1g", "--cpus", "1", "--pids-limit", "128", "--ulimit", "fsize=8192:8192",
                        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "-v", f"{repo.resolve()}:/work/repo:ro",
                        "-v", f"{work.resolve()}:/output:rw", "-w", "/work/repo", "-e", "PYTHONPATH=/work/repo/src",
                        "-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "-e", "PYTHONDONTWRITEBYTECODE=1",
                        self.image, "python", *pytest_args, "--junitxml=/output/report.xml"]
            logfile = work / "stdout.log"
            with logfile.open("w+") as output:
                with self.lock:
                    if eid in self.cancelled:
                        value.update(lifecycle="cancelled", reason="user_cancelled")
                        return
                    proc = subprocess.Popen(argv, cwd=repo, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                    self.processes[eid] = proc
                reason = "exited"
                while proc.poll() is None:
                    if time.monotonic() - start > 180 or logfile.stat().st_size > 2_000_000:
                        reason = "timeout" if time.monotonic() - start > 180 else "output_limit"
                        self.cancel(eid)
                        break
                    time.sleep(0.1)
                proc.wait(timeout=10)
                output.seek(0)
                text = sanitize(output.read(2_000_000))
            if eid in self.cancelled and reason == "exited":
                reason = "user_cancelled"
            counts = parse_junit(report)
            value.update(lifecycle="cancelled" if reason == "user_cancelled" else "completed", reason=reason,
                check_id=req.check_id, command=["python", *pytest_args], image_digest=self.image or None,
                python_version=sys.version.split()[0] if self.backend == "trusted-local" else "3.12",
                profile_id="python-pytest-demo-v1", snapshot_sha256=baseline_hash, tests_sha256=tests_hash,
                patch_hash=hashlib.sha256(req.patch.encode()).hexdigest(), exit_code=proc.returncode,
                counts=counts, stdout=text, duration_seconds=round(time.monotonic()-start, 3),
                scope="登记 profile 内的本地验证", isolation=self.backend,
                passed=proc.returncode == 0 and reason == "exited" and bool(counts and counts["collected"] > 0 and counts["passed"] > 0 and counts["failed"] == counts["errors"] == 0))
        except Exception as exc:
            value.update(lifecycle="completed", reason="runner_error", message=sanitize(str(exc)), exit_code=None, passed=False)
        finally:
            with self.lock:
                self.processes.pop(eid, None)
                value["finished_at"] = time.time()
                self._save(value)
            shutil.rmtree(work, ignore_errors=True)


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
    return {"ready": ready, "message": message, "backend": engine().backend, "image_digest": engine().image or None}


@app.post("/runner/v1/executions", status_code=202, dependencies=[Depends(authorize)])
def execute(req: ExecutionRequest):
    try:
        return engine().submit(req)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


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
