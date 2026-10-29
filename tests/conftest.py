import pytest
from fastapi.testclient import TestClient

from buildmedic import db
from buildmedic.config import settings
from buildmedic.database import upgrade_database


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    cfg = settings()
    monkeypatch.setattr(cfg, "data_dir", tmp_path)
    monkeypatch.setattr(cfg, "session_secret", "test-session-secret-" + "s" * 32)
    monkeypatch.setattr(cfg, "model_base_url", "")
    monkeypatch.setattr(cfg, "model_api_key", "")
    monkeypatch.setattr(cfg, "model_id", "")
    monkeypatch.setattr(cfg, "model_provider", "compatible")
    monkeypatch.setattr(cfg, "model_endpoint_mode", "official")
    monkeypatch.setattr(cfg, "model_token_parameter", "auto")
    monkeypatch.setattr(cfg, "model_thinking", "disabled")
    monkeypatch.setattr(cfg, "model_reasoning_effort", None)
    monkeypatch.setattr(cfg, "adaptive_enabled", False)
    monkeypatch.setattr(cfg, "runner_profiles", None)
    monkeypatch.setattr(cfg, "github_token", "")
    monkeypatch.setattr(cfg, "github_log_cache_enabled", True)
    monkeypatch.setattr(cfg, "scorer_url", "")
    monkeypatch.setattr(cfg, "scorer_token", "")
    monkeypatch.setattr(cfg, "scorer_suite_sha256", "")
    monkeypatch.setattr(cfg, "scorer_dataset", "public-development")
    monkeypatch.setattr(cfg, "diagnosis_review_key", "")
    monkeypatch.setattr(cfg, "diagnosis_rubric_sha256", "")
    monkeypatch.setattr(cfg, "runner_token", "test-internal-runner-token")
    engine = db.make_engine("sqlite:///" + str(tmp_path / "test.db"))
    monkeypatch.setattr(db, "engine", engine)
    upgrade_database()
    yield
    engine.dispose()


@pytest.fixture
def client():
    from buildmedic.api import app

    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        client.get("/api/v1/demo-cases")
        yield client


@pytest.fixture
def grouped_scoring_bundle():
    import json

    from buildmedic.config import ROOT
    from buildmedic.grouped_evaluation import registered_dataset
    from buildmedic.scoring_protocol import fingerprint
    from scorer_service.bundle import GroupedBundle

    manifest = registered_dataset()
    vectors = json.loads((ROOT / "tests/support/synthetic_probes.json").read_text())["cases"]
    return GroupedBundle.model_validate(
        {
            "schema_version": "2.0",
            "suite_id": "synthetic-grouped-behavior",
            "version": 1,
            "dataset": "public-constructed-grouped",
            "dataset_manifest": manifest.model_dump(),
            "dataset_sha256": fingerprint(manifest.model_dump()),
            "cases": [
                {
                    "case_id": c.case_id,
                    "group": c.template_group,
                    "snapshot_sha256": c.snapshot_sha256,
                    "probes": vectors[c.case_id],
                }
                for c in manifest.cases
            ],
        }
    )


@pytest.fixture
def grouped_score_request(grouped_scoring_bundle):
    import hashlib

    from buildmedic.scoring_protocol import GroupedScoreRequest, fingerprint
    from buildmedic.store import uid

    def make(partition="test", repeats=1, patches=None):
        manifest = grouped_scoring_bundle.dataset_manifest
        cases = [c for c in manifest.cases if c.split == partition]
        rows = []
        modes = ("logs", "standard", "multi", "adaptive")
        for repeat in range(repeats):
            for index, case in enumerate(cases):
                offset = (repeat + index) % 4
                for arm in modes[offset:] + modes[:offset]:
                    patch = (patches or {}).get(case.case_id) if arm != "logs" else None
                    rows.append(
                        {
                            "index": len(rows),
                            "task_id": uid(),
                            "case_id": case.case_id,
                            "snapshot_sha256": case.snapshot_sha256,
                            "repeat": repeat + 1,
                            "arm": arm,
                            "public_verified": patch is not None,
                            "patch": patch,
                            "patch_sha256": hashlib.sha256(patch.encode()).hexdigest()
                            if patch is not None
                            else None,
                        }
                    )
        return GroupedScoreRequest(
            id=uid(),
            experiment_id=uid(),
            report_sha256="a" * 64,
            suite_sha256=fingerprint(grouped_scoring_bundle.model_dump()),
            image_digest="sha256:" + "1" * 64,
            dataset_manifest=manifest,
            dataset_sha256=grouped_scoring_bundle.dataset_sha256,
            partition=partition,
            repeats=repeats,
            rows=rows,
        )

    return make


@pytest.fixture
def registered_remote(tmp_path, monkeypatch):
    """Explicit synthetic GitHub archive with real failing pytest source and pinned registry."""
    import hashlib
    import io
    import os
    import tarfile

    from buildmedic.cases import case_dir
    from buildmedic.profiles import Registry, RunnerProfile, fingerprint
    from buildmedic.snapshots import archive_files, inventory

    workflow = b"name: tests\non: push\njobs:\n  pytest:\n    runs-on: ubuntu-latest\n    steps:\n      - run: python -m pytest -q tests\n"
    files = {
        p.relative_to(case_dir("pagination")).as_posix(): p.read_bytes()
        for p in case_dir("pagination").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
    }
    files[".github/workflows/tests.yml"] = workflow
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo("owner-demo-pinned/" + name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    data = raw.getvalue()
    parsed, directories = archive_files(data)
    image = os.environ.get("BUILDMEDIC_TEST_DOCKER_IMAGE") or settings().runner_image or "sha256:" + "1" * 64
    profile = RunnerProfile(
        id="registered-pytest-v1",
        repository="owner/pytest-demo",
        head_sha="d" * 40,
        snapshot_sha256=fingerprint(inventory(parsed, directories)),
        workflow_path=".github/workflows/tests.yml",
        workflow_sha256=hashlib.sha256(workflow).hexdigest(),
        job_name="pytest",
        runtime_digest=image,
        target="tests/test_pagination.py",
        failure_fingerprint="test_pages",
    )
    path = tmp_path / "runner-profiles.json"
    path.write_text(Registry(profiles=[profile]).model_dump_json())
    monkeypatch.setattr(settings(), "runner_profiles", path)
    return profile, data
