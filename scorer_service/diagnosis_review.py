"""Offline, operator-controlled diagnosis adjudication. Private references stay external."""

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from buildmedic.config import ROOT
from buildmedic.diagnosis_protocol import ReviewReceipt, ReviewRequest, Verdict, sign, verify
from buildmedic.evaluation_dataset import DatasetManifest, Slug
from buildmedic.scoring_protocol import fingerprint


def external_path(value):
    path = Path(value).absolute()
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or path.resolve().is_relative_to(ROOT):
        raise ValueError("review_material_must_be_external_without_symlinks")
    return path


def read_private(path):
    path = external_path(path)
    if path.stat().st_size > 4000000:
        raise ValueError("review_file_size_limit")
    return json.loads(path.read_bytes())


def write_private(path, data):
    """Exclusive, mode-0600 output. Identical reruns are safe; never overwrite a review."""
    path = external_path(path)
    encoded = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600 or path.read_bytes() != encoded:
                raise ValueError("review_output_exists") from None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: Slug
    root_cause: str = Field(min_length=1, max_length=5000)
    location: str = Field(min_length=1, max_length=5000)
    guidance: str = Field(min_length=1, max_length=5000)


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["diagnosis-rubric-v1"] = "diagnosis-rubric-v1"
    version: int = Field(ge=1, strict=True)
    dataset: DatasetManifest
    references: list[Reference] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def coverage(self):
        if len({r.case_id for r in self.references}) != len(self.references) or {
            r.case_id for r in self.references
        } != {c.case_id for c in self.dataset.cases}:
            raise ValueError("rubric_case_coverage")
        return self


def load_rubric(path):
    rubric = Rubric.model_validate(read_private(path))
    return rubric, fingerprint(rubric.model_dump())


class SheetRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: dict
    reference: Reference
    root_cause_correct: StrictBool | None = None
    location_correct: StrictBool | None = None
    notes: str = Field(default="", max_length=2000)


class Sheet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["diagnosis-review-sheet-v1"] = "diagnosis-review-sheet-v1"
    request_sha256: str
    rubric_sha256: str
    reviewer: str = Field(default="", max_length=200)
    human_review_complete: StrictBool = False
    rows: list[SheetRow] = Field(min_length=1, max_length=36)


def export_sheet(request: ReviewRequest, rubric: Rubric):
    rubric_hash = fingerprint(rubric.model_dump())
    if rubric_hash != request.rubric_sha256:
        raise ValueError("rubric_mismatch")
    cases = {c.case_id: c for c in rubric.dataset.cases}
    refs = {r.case_id: r for r in rubric.references}
    rows = []
    for item in request.rows:
        case = cases.get(item.case_id)
        if case is None or case.snapshot_sha256 != item.snapshot_sha256:
            raise ValueError("rubric_source_mismatch")
        rows.append(SheetRow(item=item.model_dump(), reference=refs[item.case_id]))
    # The operator sheet has no report ID/hash, arm, model, patch, public outcome, usage or task ID.
    return Sheet(request_sha256=fingerprint(request.model_dump()), rubric_sha256=rubric_hash, rows=rows)


def seal(request: ReviewRequest, rubric: Rubric, sheet: Sheet, key: str):
    expected = export_sheet(request, rubric)
    if (sheet.request_sha256, sheet.rubric_sha256) != (expected.request_sha256, expected.rubric_sha256):
        raise ValueError("sheet_binding_mismatch")
    if not sheet.human_review_complete or not sheet.reviewer.strip():
        raise ValueError("human_review_attestation_required")
    original = {r.item["review_id"]: r for r in expected.rows}
    seen, rows = set(), []
    for row in sheet.rows:
        rid = row.item.get("review_id")
        reference = original.get(rid)
        if (
            rid in seen
            or reference is None
            or row.item != reference.item
            or row.reference != reference.reference
        ):
            raise ValueError("sheet_material_changed")
        seen.add(rid)
        rows.append(
            Verdict(
                review_id=rid,
                assessment_sha256=row.item["assessment_sha256"],
                root_cause_correct=row.root_cause_correct,
                location_correct=row.location_correct,
            )
        )
    data = {k: getattr(request, k) for k in ("protocol", "id", "report_sha256", "rubric_sha256")} | {
        "request_sha256": expected.request_sha256,
        "reviewer_sha256": fingerprint(sheet.reviewer.strip()),
        "sealed_at": time.time(),
        "rows": [r.model_dump() for r in rows],
    }
    return verify(ReviewReceipt.model_validate(sign(data, key)), request, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["rubric-hash", "export", "seal"])
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--sheet", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rubric, digest = load_rubric(args.rubric)
    if args.operation == "rubric-hash":
        print(digest)
        return
    if not args.request or not args.output:
        parser.error("export/seal requires --request and --output")
    request = ReviewRequest.model_validate(read_private(args.request))
    if args.operation == "export":
        result = export_sheet(request, rubric).model_dump()
    else:
        if not args.sheet:
            parser.error("seal requires --sheet")
        result = seal(
            request,
            rubric,
            Sheet.model_validate(read_private(args.sheet)),
            os.environ.get("BUILDMEDIC_DIAGNOSIS_REVIEW_KEY", ""),
        )
    write_private(args.output, result)
    print("Offline review artifact written (private, mode 0600).")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError):
        print(
            "Review rejected: check private paths, frozen inputs, complete human labels and signing key.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
