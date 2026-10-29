"""Public dataset preflight; validation does not register executable cases."""

import argparse
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .scoring_protocol import fingerprint

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")]
Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class DatasetCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: Slug
    repository_group: Slug
    template_group: Slug
    split: Literal["development", "test"]
    snapshot_sha256: Sha
    log_sha256: Sha


class DatasetManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["public-development", "grouped-benchmark"]
    cases: list[DatasetCase] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def isolation(self):
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate_case_id")
        dev = [c for c in self.cases if c.split == "development"]
        test = [c for c in self.cases if c.split == "test"]
        if self.purpose == "public-development" and test:
            raise ValueError("public_development_has_no_test_split")
        if self.purpose == "grouped-benchmark" and (not dev or not test or len(self.cases) < 20):
            raise ValueError("benchmark_requires_20_to_30_cases_and_both_splits")
        for axis in ("repository_group", "template_group", "snapshot_sha256", "log_sha256"):
            if {getattr(c, axis) for c in dev} & {getattr(c, axis) for c in test}:
                raise ValueError("split_overlap_" + axis)
        # Renaming a known public case cannot turn its source into a held-out sample.
        from .evaluation import captured_log

        public_ids = {"pagination", "imports", "signature"}
        public_sources = {captured_log(cid).snapshot_sha256 for cid in public_ids}
        public_logs = {captured_log(cid).log_sha256 for cid in public_ids}
        if any(
            c.case_id in public_ids or c.snapshot_sha256 in public_sources or c.log_sha256 in public_logs
            for c in test
        ):
            raise ValueError("known_public_case_cannot_be_test")
        return self


def load_manifest(path: Path):
    if path.stat().st_size > 65536:
        raise ValueError("manifest_size_limit")
    return DatasetManifest.model_validate_json(path.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    print(
        json.dumps(
            {
                "manifest_sha256": fingerprint(manifest.model_dump()),
                "purpose": manifest.purpose,
                "cases": len(manifest.cases),
                "development": sum(c.split == "development" for c in manifest.cases),
                "test": sum(c.split == "test" for c in manifest.cases),
                "execution_registered": False,
            }
        )
    )


if __name__ == "__main__":
    main()
