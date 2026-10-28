"""Administrator-owned offline pytest profiles; browser/model input cannot register one."""

import hashlib
import json
import re
from pathlib import PurePosixPath

from pydantic import Field, ValidationError, model_validator

from .config import settings
from .errors import Blocked
from .schemas import StrictModel

SHA = r"^[a-f0-9]{40}$"
HASH = r"^[a-f0-9]{64}$"
REPO = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


def relative_path(value, *, root=False):
    if root and value == ".":
        return value
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or len(value.encode()) > 1024
        or any(len(part.encode()) > 255 for part in path.parts)
        or path.is_absolute()
        or str(path) != value
        or ".." in path.parts
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or any(p in {".git", ".env"} for p in path.parts)
    ):
        raise ValueError("登记路径必须是规范的仓库内相对路径")
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class RunnerProfile(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    repository: str = Field(pattern=REPO, max_length=200)
    head_sha: str = Field(pattern=SHA)
    snapshot_sha256: str = Field(pattern=HASH)
    workflow_path: str = Field(pattern=r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$")
    workflow_sha256: str = Field(pattern=HASH)
    job_name: str = Field(min_length=1, max_length=200)
    runtime_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    workspace: str = Field(".", max_length=200)
    target: str = Field(pattern=r"^tests/[A-Za-z0-9_./-]+\.py$", max_length=200)
    failure_fingerprint: str = Field(min_length=3, max_length=200)
    required_files: dict[str, str] = Field(default_factory=dict, max_length=30)

    @model_validator(mode="after")
    def safe_profile(self):
        if any(part in {".", ".."} for part in self.repository.split("/")):
            raise ValueError("仓库名称不合法")
        relative_path(self.workspace, root=True)
        relative_path(self.target)
        for path, sha in self.required_files.items():
            relative_path(path)
            if not re.fullmatch(HASH, sha):
                raise ValueError("依赖或配置文件必须固定 SHA256")
        if self.required_files.get(self.workflow_path, self.workflow_sha256) != self.workflow_sha256:
            raise ValueError("workflow 指纹不能被 required_files 覆盖")
        if any(ord(c) < 32 for c in self.job_name + self.failure_fingerprint):
            raise ValueError("Job 名称和失败指纹必须为单行文本")
        return self

    @property
    def digest(self):
        return fingerprint(self.model_dump())

    def binding(self):
        return {
            "profile_id": self.id,
            "profile_hash": self.digest,
            "repository": self.repository,
            "head_sha": self.head_sha,
            "snapshot_sha256": self.snapshot_sha256,
        }


class Registry(StrictModel):
    profiles: list[RunnerProfile] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unambiguous(self):
        ids, jobs = set(), set()
        for p in self.profiles:
            key = (p.repository.lower(), p.head_sha, p.workflow_path, p.job_name)
            if p.id in ids or key in jobs:
                raise ValueError("登记 profile ID 或仓库/SHA/workflow/Job 重复")
            ids.add(p.id)
            jobs.add(key)
        return self


def registry():
    path = settings().runner_profiles
    if path is None:
        return Registry()
    try:
        if path.stat().st_size > 256000:
            raise ValueError("Registry too large")
        return Registry.model_validate_json(path.read_text())
    except (OSError, ValueError, ValidationError) as exc:
        raise Blocked("服务端 Runner profile 登记文件无效，请检查配置") from exc


def registered(binding):
    p = next((p for p in registry().profiles if p.id == binding["profile_id"]), None)
    if not p or p.binding() != binding:
        raise Blocked("登记的仓库、SHA 或 profile 已变化，不能执行原任务")
    return p


def match_profile(repo, sha, workflow_path, job_name, head_repo, *, candidates=None):
    if not head_repo or head_repo.lower() != repo.lower():
        return None
    return next(
        (
            p
            for p in (registry().profiles if candidates is None else candidates)
            if p.repository.lower() == repo.lower()
            and p.head_sha == sha.lower()
            and p.workflow_path == workflow_path
            and p.job_name == job_name
        ),
        None,
    )
