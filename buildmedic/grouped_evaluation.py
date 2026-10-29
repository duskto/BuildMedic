"""Registered public constructed dataset. No private labels or caller-defined commands."""

from .cases import CASES, EVALUATION_CASE_IDS, metadata
from .config import ROOT
from .evaluation_dataset import DatasetManifest
from .scoring_protocol import fingerprint


def registered_dataset():
    from .evaluation import captured_log

    manifest = DatasetManifest.model_validate_json((ROOT / "evaluation/grouped-dataset.json").read_bytes())
    if [c.case_id for c in manifest.cases] != list(EVALUATION_CASE_IDS):
        raise ValueError("registered_dataset_identity_mismatch")
    for case in manifest.cases:
        log = captured_log(case.case_id)
        meta = CASES[case.case_id]
        if (
            case.snapshot_sha256 != metadata(case.case_id)["snapshot_sha256"]
            or case.log_sha256 != log.log_sha256
            or case.repository_group != meta.get("repository_group", "buildmedic-demo")
            or case.template_group != meta.get("template_group", case.case_id)
            or case.split != meta.get("split", "development")
        ):
            raise ValueError("registered_dataset_material_mismatch")
    return manifest


def plan(split):
    manifest = registered_dataset()
    from .evaluation import GroupedSpec

    return GroupedSpec(
        schema_version="3.0",
        name="grouped-" + split,
        dataset="public-constructed-grouped",
        partition=split,
        dataset_manifest=manifest,
        dataset_sha256=fingerprint(manifest.model_dump()),
        cases=[
            c.model_dump(include={"case_id", "snapshot_sha256", "log_sha256"})
            for c in manifest.cases
            if c.split == split
        ],
    )
