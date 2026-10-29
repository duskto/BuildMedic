"""Detached human review protocol. No private references or model/repair outcomes."""

import hashlib
import hmac
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .evaluation_dataset import Sha, Slug
from .scoring_protocol import canonical, fingerprint


class ReviewEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=80)
    kind: str = Field(max_length=40)
    path: str | None = Field(default=None, max_length=300)
    line_start: int = Field(ge=0)
    line_end: int = Field(ge=0)
    excerpt: str = Field(max_length=600)
    excerpt_truncated: bool


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    case_id: Slug
    snapshot_sha256: Sha
    assessment_sha256: Sha
    diagnosis: dict | None
    evidence: list[ReviewEvidence] = Field(max_length=30)

    @model_validator(mode="after")
    def binding(self):
        material = self.model_dump(exclude={"review_id", "assessment_sha256"})
        if fingerprint(material) != self.assessment_sha256:
            raise ValueError("assessment_mismatch")
        if len(canonical(material)) > 80000:
            raise ValueError("assessment_size_limit")
        if self.diagnosis is not None:
            from .schemas import Diagnosis

            diagnosis = Diagnosis.model_validate(self.diagnosis)
            expected = diagnosis.model_dump(exclude={"patch", "edits"})
            if expected != self.diagnosis:
                raise ValueError("diagnosis_projection_required")
        return self


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["blinded-diagnosis-v1"] = "blinded-diagnosis-v1"
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    report_sha256: Sha
    rubric_sha256: Sha
    rows: list[ReviewItem] = Field(min_length=1, max_length=360)

    @model_validator(mode="after")
    def unique(self):
        if len({r.review_id for r in self.rows}) != len(self.rows):
            raise ValueError("duplicate_review_id")
        if len(canonical(self.model_dump())) > 32000000:
            raise ValueError("review_size_limit")
        return self


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    assessment_sha256: Sha
    root_cause_correct: StrictBool | None
    location_correct: StrictBool | None


class ReviewReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["blinded-diagnosis-v1"]
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    report_sha256: Sha
    rubric_sha256: Sha
    request_sha256: Sha
    reviewer_sha256: Sha
    sealed_at: float = Field(gt=0, allow_inf_nan=False)
    rows: list[Verdict] = Field(min_length=1, max_length=360)
    signature: Sha


def sign(data, key):
    if len(key) < 32:
        raise ValueError("review_key_too_short")
    unsigned = {k: v for k, v in data.items() if k != "signature"}
    signature = hmac.new(key.encode(), b"buildmedic-diagnosis-v1\0" + canonical(unsigned), hashlib.sha256)
    return unsigned | {"signature": signature.hexdigest()}


def verify(receipt: ReviewReceipt, request: ReviewRequest, key: str):
    data = receipt.model_dump()
    if not hmac.compare_digest(sign(data, key)["signature"], receipt.signature):
        raise ValueError("review_signature_mismatch")
    for name in ("protocol", "id", "report_sha256", "rubric_sha256"):
        if getattr(receipt, name) != getattr(request, name):
            raise ValueError("review_binding_mismatch")
    if receipt.request_sha256 != fingerprint(request.model_dump()):
        raise ValueError("review_request_mismatch")
    rows = {row.review_id: row for row in receipt.rows}
    if len(rows) != len(receipt.rows) or set(rows) != {row.review_id for row in request.rows}:
        raise ValueError("review_rows_mismatch")
    for item in request.rows:
        row = rows[item.review_id]
        if row.assessment_sha256 != item.assessment_sha256:
            raise ValueError("review_assessment_mismatch")
        missing = row.root_cause_correct is None or row.location_correct is None
        if (
            item.diagnosis is None
            and (row.root_cause_correct is not None or row.location_correct is not None)
        ) or (item.diagnosis is not None and missing):
            raise ValueError("review_incomplete_or_fabricated")
    return data
