"""Separate authenticated scoring coordinator. Start with uvicorn --factory."""

import hashlib
import hmac
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from buildmedic.config import ROOT
from buildmedic.scoring_protocol import (
    RECEIPT_ADAPTER,
    REQUEST_ADAPTER,
    GroupedScoreRequest,
    ScoreSubmission,
    fingerprint,
    sign_receipt,
)

from .bundle import load_bundle
from .engine import DockerObserver, evaluate


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUILDMEDIC_SCORER_", extra="ignore")
    bundle_path: Path
    state_dir: Path
    token: str = Field(min_length=32)
    image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class Runtime:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bundle, self.suite_hash = load_bundle(cfg.bundle_path)
        from buildmedic import evaluation_dataset, fingerprints, schemas, scoring_protocol

        code_files = [
            *Path(__file__).parent.glob("*.py"),
            *(
                Path(module.__file__)
                for module in (scoring_protocol, fingerprints, evaluation_dataset, schemas)
            ),
        ]
        self.code_hash = fingerprint({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in code_files})
        self.root = cfg.state_dir.absolute()
        if self.root.resolve().is_relative_to(ROOT.resolve()) or any(
            p.is_symlink() for p in [self.root, *self.root.parents]
        ):
            raise ValueError("评分状态目录必须位于工作区外，且不能为符号链接")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.observer = DockerObserver(cfg.image, self.root / "work")
        self.lock, self.jobs = threading.RLock(), {}
        self.pool = ThreadPoolExecutor(max_workers=1)
        for path in self.root.glob("*.json"):
            job = json.loads(path.read_text())
            request = REQUEST_ADAPTER.validate_python(job["request"])
            if job["status"] in {"queued", "running"}:
                job.update(status="failed", receipt=self.receipt(request, [], "failed", "service_restarted"))
                self.save(job)
            self.jobs[request.id] = job

    def save(self, job):
        path = self.root / (job["request"]["id"] + ".json")
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(job, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)

    def receipt(self, request, rows, status="completed", error=None):
        return RECEIPT_ADAPTER.validate_python(
            sign_receipt(
                {
                    "schema_version": request.schema_version,
                    "id": request.id,
                    "experiment_id": request.experiment_id,
                    "report_sha256": request.report_sha256,
                    "request_sha256": fingerprint(request.model_dump()),
                    "suite_sha256": request.suite_sha256,
                    "image_digest": self.cfg.image,
                    "scorer_sha256": self.code_hash,
                    "status": status,
                    "finished_at": time.time(),
                    "rows": rows,
                    "error_code": error,
                    **(
                        {"dataset_sha256": request.dataset_sha256, "partition": request.partition}
                        if isinstance(request, GroupedScoreRequest)
                        else {}
                    ),
                },
                self.cfg.token,
            )
        ).model_dump()

    def public(self, job):
        return {
            "id": job["request"]["id"],
            "request_sha256": job["request_hash"],
            "status": job["status"],
            "receipt": job.get("receipt"),
        }

    def submit(self, request):
        request_hash = fingerprint(request.model_dump())
        with self.lock:
            if request.id in self.jobs:
                job = self.jobs[request.id]
                if job["request_hash"] != request_hash:
                    raise HTTPException(409, "request_conflict")
                return self.public(job)
            _, current = load_bundle(self.cfg.bundle_path)
            if (
                request.suite_sha256 != self.suite_hash
                or current != self.suite_hash
                or request.image_digest != self.cfg.image
            ):
                raise HTTPException(409, "suite_changed")
            if {r.case_id for r in request.rows} - {c.case_id for c in self.bundle.cases}:
                raise HTTPException(422, "case_unsupported")
            if request.schema_version != self.bundle.schema_version or request.dataset != self.bundle.dataset:
                raise HTTPException(409, "dataset_mismatch")
            if isinstance(request, GroupedScoreRequest) and (
                request.dataset_sha256 != self.bundle.dataset_sha256
                or request.dataset_manifest != self.bundle.dataset_manifest
            ):
                raise HTTPException(409, "dataset_mismatch")
            if sum(j["status"] in {"queued", "running"} for j in self.jobs.values()) >= 4:
                raise HTTPException(429, "queue_full")
            job = {"request": request.model_dump(), "request_hash": request_hash, "status": "queued"}
            self.jobs[request.id] = job
            self.save(job)
            self.pool.submit(self.execute, request)
            return self.public(job)

    def execute(self, request):
        with self.lock:
            job = self.jobs[request.id]
            job["status"] = "running"
            self.save(job)
        try:
            _, current = load_bundle(self.cfg.bundle_path)
            if current != self.suite_hash:
                raise ValueError("suite_changed")
            result = self.receipt(request, evaluate(request, self.bundle, self.observer))
        except Exception:  # noqa: BLE001 - Only a fixed public failure code may cross the private boundary.
            result = self.receipt(request, [], "failed", "evaluation_failed")
        with self.lock:
            job.update(status=result["status"], receipt=result)
            self.save(job)


def make_app(cfg):
    runtime = Runtime(cfg)

    @asynccontextmanager
    async def lifespan(_):
        yield
        runtime.pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(title="BuildMedic independent scorer", lifespan=lifespan)
    app.state.runtime = runtime

    def authorize(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + cfg.token).encode()):
            raise HTTPException(401, "unauthorized")

    @app.middleware("http")
    async def bounded(request, call_next):
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            if not length.isdecimal() or int(length) > 24_000_000:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(Exception)
    async def failed(_request, _exc):
        return JSONResponse({"error": "scorer_unavailable"}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _exc):
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.get("/scorer/v1/health", dependencies=[Depends(authorize)])
    def health():
        return {
            "ready": True,
            "suite_sha256": runtime.suite_hash,
            "image_digest": cfg.image,
            "dataset": runtime.bundle.dataset,
            "protocol_version": runtime.bundle.schema_version,
        }

    @app.post("/scorer/v1/jobs", status_code=202, dependencies=[Depends(authorize)])
    def submit(body: ScoreSubmission):
        return runtime.submit(body)

    @app.get("/scorer/v1/jobs/{job_id}", dependencies=[Depends(authorize)])
    def get(job_id: str):
        with runtime.lock:
            if job_id not in runtime.jobs:
                raise HTTPException(404, "not_found")
            return runtime.public(runtime.jobs[job_id])

    return app


def create_app():
    return make_app(ServiceSettings())
