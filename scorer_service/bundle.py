"""Trusted external declarative assertions. No references or hidden vectors in the repo."""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from buildmedic.config import ROOT
from buildmedic.evaluation_dataset import DatasetManifest, Sha, Slug
from buildmedic.schemas import StrictModel
from buildmedic.scoring_protocol import CaseId, canonical, fingerprint

from .probe import ALLOWED as ENTRYPOINTS


class Probe(StrictModel):
    entrypoint: str = Field(max_length=40)
    args: list[JsonValue] = Field(default_factory=list, max_length=5)
    kwargs: dict[str, JsonValue] = Field(default_factory=dict, max_length=5)
    expected: JsonValue = None
    raises: Literal["ValueError", "TypeError"] | None = None


class PrivateCase(StrictModel):
    case_id: CaseId
    group: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    probes: list[Probe] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def registered_functions(self):
        if any(p.entrypoint not in ENTRYPOINTS.get(self.case_id, set()) for p in self.probes):
            raise ValueError("未登记的行为入口")
        if len(canonical(self.model_dump())) > 16000:
            raise ValueError("私有案例超出输入上限")
        return self


class Bundle(StrictModel):
    schema_version: Literal["1.0"]
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    version: int = Field(ge=1, strict=True)
    dataset: Literal["public-development"]
    development_groups: list[str] = Field(min_length=1, max_length=30)
    test_groups: list[str] = Field(default_factory=list, max_length=30)
    cases: list[PrivateCase] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def partition(self):
        dev, test = set(self.development_groups), set(self.test_groups)
        if dev & test or len(dev) != len(self.development_groups) or len(test) != len(self.test_groups):
            raise ValueError("开发集和测试集的缺陷组必须去重且互不重叠")
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("私有案例重复")
        if any(c.group not in dev for c in self.cases):
            raise ValueError("已公开开发案例不能标成隔离测试集")
        canonical(self.model_dump())  # Reject NaN/Infinity, including nested probe data.
        return self


class GroupedPrivateCase(PrivateCase):
    case_id: Slug


class GroupedBundle(StrictModel):
    schema_version: Literal["2.0"]
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,79}$")
    version: int = Field(ge=1, strict=True)
    dataset: Literal["public-constructed-grouped"]
    dataset_manifest: DatasetManifest
    dataset_sha256: Sha
    cases: list[GroupedPrivateCase] = Field(min_length=20, max_length=30)

    @model_validator(mode="after")
    def complete_catalog(self):
        manifest = self.dataset_manifest
        if (
            manifest.purpose != "grouped-benchmark"
            or fingerprint(manifest.model_dump()) != self.dataset_sha256
        ):
            raise ValueError("private_bundle_dataset_mismatch")
        cases = {c.case_id: c for c in self.cases}
        if len(cases) != len(self.cases) or set(cases) != {c.case_id for c in manifest.cases}:
            raise ValueError("private_bundle_requires_complete_catalog")
        for case in manifest.cases:
            private = cases[case.case_id]
            if private.snapshot_sha256 != case.snapshot_sha256 or private.group != case.template_group:
                raise ValueError("private_bundle_group_or_source_mismatch")
        canonical(self.model_dump())
        return self


BUNDLE_ADAPTER = TypeAdapter(Bundle | GroupedBundle)


def load_bundle(path):
    path = Path(path).absolute()
    if (
        path.is_symlink()
        or path.resolve().is_relative_to(ROOT.resolve())
        or any(p.is_symlink() for p in path.parents)
    ):
        raise ValueError("私有评分材料必须位于工作区外，不能通过符号链接指向工作区")
    if path.stat().st_size > 600000:
        raise ValueError("私有评分包超出大小限制")
    bundle = BUNDLE_ADAPTER.validate_python(json.loads(path.read_text()))
    return bundle, fingerprint(bundle.model_dump())
