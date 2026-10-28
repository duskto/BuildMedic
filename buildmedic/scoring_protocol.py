"""Public wire protocol only. Private assertions never enter this package."""

import hashlib
import hmac
import json
from typing import Literal

from pydantic import Field, model_validator

from .schemas import StrictModel

CaseId = Literal["pagination", "imports", "signature"]
Arm = Literal["logs", "standard", "multi", "adaptive"]


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class ScoreItem(StrictModel):
    index: int = Field(ge=0, le=35, strict=True)
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    case_id: CaseId
    arm: Arm
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    public_verified: bool = Field(strict=True)
    patch: str | None = Field(None, max_length=40000)
    patch_sha256: str | None = Field(None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bound_patch(self):
        if (self.patch is None) != (self.patch_sha256 is None):
            raise ValueError("patch 和指纹必须同时提供")
        if self.patch is not None and hashlib.sha256(self.patch.encode()).hexdigest() != self.patch_sha256:
            raise ValueError("patch 指纹不匹配")
        if self.patch is not None and len(self.patch.encode()) > 40000:
            raise ValueError("patch 字节数超出上限")
        if self.arm == "logs" and (self.patch is not None or self.public_verified):
            raise ValueError("日志组不参与行为修复评分")
        return self


class ScoreRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    experiment_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    suite_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    dataset: Literal["public-development"] = "public-development"
    rows: list[ScoreItem] = Field(min_length=1, max_length=36)

    @model_validator(mode="after")
    def unique_rows(self):
        if [r.index for r in self.rows] != list(range(len(self.rows))) or len(
            {r.task_id for r in self.rows}
        ) != len(self.rows):
            raise ValueError("评分任务序号和归属必须唯一且连续")
        return self


class ScoreRow(StrictModel):
    index: int = Field(ge=0, le=35)
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    case_id: CaseId
    arm: Arm
    patch_sha256: str | None
    status: Literal[
        "not_applicable", "no_candidate", "passed", "failed", "not_reproduced", "evaluation_error"
    ]
    hidden_verified: bool | None
    new_regression: bool | None
    probes: int = Field(ge=0, le=50)
    baseline_failed: int = Field(ge=0, le=50)

    @model_validator(mode="after")
    def verdict(self):
        expected = True if self.status == "passed" else False if self.status == "failed" else None
        if self.hidden_verified is not expected:
            raise ValueError("评分结论与状态不匹配")
        if self.status in {"passed", "failed"} and (not self.probes or not self.baseline_failed):
            raise ValueError("行为评分需要真实基线复现与观察")
        if self.status in {"passed", "failed"} and (
            (self.baseline_failed < self.probes) != (self.new_regression is not None)
        ):
            raise ValueError("新增回归必须有原先通过的行为作比较")
        if self.baseline_failed > self.probes:
            raise ValueError("基线失败数超出检查数")
        if self.arm == "logs" and (self.status != "not_applicable" or self.patch_sha256 is not None):
            raise ValueError("日志组不参与行为修复评分")
        if self.status in {"passed", "failed", "not_reproduced"} and self.patch_sha256 is None:
            raise ValueError("补丁行为评分缺少候选指纹")
        return self


class ScoreReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    experiment_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    report_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    suite_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    scorer_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["completed", "failed"]
    finished_at: float
    rows: list[ScoreRow] = Field(max_length=36)
    error_code: Literal["evaluation_failed", "service_restarted"] | None = None
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")


def sign_receipt(data, secret):
    return {**data, "signature": hmac.new(secret.encode(), canonical(data), hashlib.sha256).hexdigest()}


def verify_receipt(receipt, request, secret):
    value = receipt.model_dump()
    signature = value.pop("signature")
    if not hmac.compare_digest(sign_receipt(value, secret)["signature"], signature):
        raise ValueError("评分签名不匹配")
    if any(
        getattr(receipt, key) != getattr(request, key)
        for key in ("id", "experiment_id", "report_sha256", "suite_sha256", "image_digest")
    ) or receipt.request_sha256 != fingerprint(request.model_dump()):
        raise ValueError("评分回执不属于当前冻结请求")
    if receipt.status == "completed":
        if len(receipt.rows) != len(request.rows):
            raise ValueError("评分回执缺少任务")
        for expected, actual in zip(request.rows, receipt.rows):
            if any(
                getattr(expected, key) != getattr(actual, key)
                for key in ("index", "task_id", "case_id", "arm", "patch_sha256")
            ):
                raise ValueError("评分行与冻结候选不匹配")
    return receipt.model_dump()
