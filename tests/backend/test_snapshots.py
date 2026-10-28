import asyncio
import gzip
import io
import tarfile

import httpx
import pytest
from pydantic import ValidationError

from buildmedic.config import Settings, settings
from buildmedic.errors import Blocked
from buildmedic.profiles import Registry, fingerprint, match_profile, registered
from buildmedic.schemas import ExecutionRequest, RemoteSnapshot
from buildmedic.snapshots import SnapshotStore, archive_files, download_archive, inventory


def tar(entries):
    dest = io.BytesIO()
    with tarfile.open(fileobj=dest, mode="w:gz") as archive:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            info.linkname = "../../outside"
            archive.addfile(info, io.BytesIO(data) if info.size else None)
    return dest.getvalue()


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/root/escape",
        "root/../escape",
        "root/./file",
        "root//file",
        "root/.git/config",
        "root/.env",
        "root/a\\b",
        ".",
    ],
)
def test_archive_rejects_ambiguous_and_escaping_paths(name):
    with pytest.raises(ValueError):
        archive_files(tar([(name, tarfile.REGTYPE, b"not executed")]))


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE]
)
def test_archive_rejects_links_and_special_files(kind):
    with pytest.raises(ValueError):
        archive_files(tar([("root/src/file.py", kind, b"")]))


@pytest.mark.parametrize(
    "entries",
    [
        [("root/a", tarfile.REGTYPE, b"a"), ("root/a", tarfile.REGTYPE, b"b")],
        [("root/a", tarfile.REGTYPE, b"a"), ("another/a", tarfile.REGTYPE, b"b")],
        [("root/a", tarfile.REGTYPE, b"a"), ("root/a/b", tarfile.REGTYPE, b"b")],
    ],
)
def test_archive_rejects_duplicate_roots_and_file_directory_conflicts(entries):
    with pytest.raises(ValueError):
        archive_files(tar(entries))


def test_compressed_and_expanded_limits_stop_before_extracting(monkeypatch):
    monkeypatch.setattr("buildmedic.snapshots.COMPRESSED_LIMIT", 100)
    with pytest.raises(ValueError, match="压缩包"):
        archive_files(b"x" * 101)
    monkeypatch.setattr("buildmedic.snapshots.COMPRESSED_LIMIT", 10000)
    monkeypatch.setattr("buildmedic.snapshots.EXPANDED_LIMIT", 1000)
    with pytest.raises(ValueError, match="展开"):
        archive_files(gzip.compress(b"x" * 1001))


def test_cache_is_verified_on_each_read_and_bad_source_never_replaces_it(registered_remote, tmp_path):
    profile, data = registered_remote
    cache = SnapshotStore(tmp_path / "snapshots")
    binding = profile.binding()
    prepared = cache.install(binding, data)
    assert prepared["workflow_path"] == profile.workflow_path
    assert prepared["binding"] == binding
    assert "src/demo/pagination.py" in prepared["source"]
    assert cache.install(binding, data) == prepared
    path = cache.path(binding) / "repo/src/demo/pagination.py"
    path.write_text("changed cache")
    with pytest.raises(ValueError, match="完整性"):
        cache.describe(binding)
    with pytest.raises(ValueError):
        cache.install(binding, data)
    assert path.read_text() == "changed cache"


def test_profile_matches_exact_commit_workflow_job_and_head_repository(registered_remote):
    p, _ = registered_remote
    args = (p.repository, p.head_sha, p.workflow_path, p.job_name, p.repository)
    assert match_profile(*args) == p
    for i, wrong in [
        (0, "someone/else"),
        (1, "e" * 40),
        (2, ".github/workflows/other.yml"),
        (3, "pytest (new matrix)"),
        (4, "fork/pytest-demo"),
    ]:
        changed = list(args)
        changed[i] = wrong
        assert match_profile(*changed) is None
    binding = dict(p.binding(), profile_hash="0" * 64)
    with pytest.raises(Blocked):
        registered(binding)
    with pytest.raises(ValidationError):
        Registry(profiles=[p, p])


def test_workflow_and_secret_containing_source_are_not_accepted(registered_remote, tmp_path):
    p, data = registered_remote
    files, dirs = archive_files(data)
    files["src/demo/pagination.py"] = b'API_KEY="sk-' + b"a" * 32 + b'"\n'
    bad = tar([("root/" + path, tarfile.REGTYPE, text) for path, text in files.items()])
    parsed, dirs = archive_files(bad)
    changed = p.model_copy(update={"snapshot_sha256": fingerprint(inventory(parsed, dirs))})
    settings().runner_profiles.write_text(Registry(profiles=[changed]).model_dump_json())
    cache = SnapshotStore(tmp_path / "cache")
    with pytest.raises(ValueError, match="脱敏"):
        cache.install(changed.binding(), bad)
    assert not cache.path(changed.binding()).exists()


def test_archive_download_is_fixed_sha_no_credentials_and_no_redirects(registered_remote):
    p, data = registered_remote
    calls = []

    def response(request):
        calls.append(request)
        assert request.url.host == "codeload.github.com"
        assert request.url.path == f"/{p.repository}/tar.gz/{p.head_sha}"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=data)

    assert asyncio.run(download_archive(p.repository, p.head_sha, httpx.MockTransport(response))) == data
    with pytest.raises(Blocked):
        asyncio.run(
            download_archive(
                p.repository,
                p.head_sha,
                httpx.MockTransport(
                    lambda _: httpx.Response(302, headers={"Location": "https://attacker.invalid/archive"})
                ),
            )
        )
    assert len(calls) == 1
    with pytest.raises(ValueError):
        asyncio.run(download_archive("../bad", p.head_sha))


def test_optional_profile_environment_and_runner_request_union(registered_remote):
    p, _ = registered_remote
    assert Settings(_env_file=None, runner_profiles="").runner_profiles is None
    remote = RemoteSnapshot.model_validate(p.binding())
    assert ExecutionRequest(execution_id="a" * 32, remote=remote, check_id="target").case_id is None
    for values in ({}, {"case_id": "pagination", "remote": p.binding()}):
        with pytest.raises(ValidationError):
            ExecutionRequest(execution_id="a" * 32, check_id="target", **values)


def test_remote_snapshots_require_internal_token_and_exact_docker_image(
    registered_remote, tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from buildmedic import runner

    p, _ = registered_remote
    engine = runner.Engine(backend="trusted-local", image=p.runtime_digest, data_dir=tmp_path / "runner")
    monkeypatch.setattr(runner, "_engine", engine)
    try:
        with TestClient(runner.app) as client:
            assert client.post("/runner/v1/snapshots", json=p.binding()).status_code == 401
            response = client.post(
                "/runner/v1/snapshots",
                json=p.binding(),
                headers={"Authorization": "Bearer " + settings().runner_token},
            )
            assert response.status_code == 409 and "Docker" in response.json()["detail"]
        engine.backend, engine.image = "docker", "sha256:" + "f" * 64
        with pytest.raises(ValueError, match="固定 Docker"):
            engine.submit(ExecutionRequest(execution_id="b" * 32, remote=p.binding(), check_id="target"))
        assert not engine.jobs
    finally:
        engine.pool.shutdown(wait=True)


def test_registration_helper_generates_reviewable_file_without_enabling_it(
    registered_remote, tmp_path, monkeypatch
):
    import importlib.util
    from argparse import Namespace

    from buildmedic.config import ROOT

    p, data = registered_remote
    spec = importlib.util.spec_from_file_location("register_profile", ROOT / "scripts/register_profile.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def download(*_):
        return data

    monkeypatch.setattr(module, "download_archive", download)
    output = tmp_path / "candidate-registry.json"
    configured = settings().runner_profiles
    args = Namespace(
        id=p.id,
        repository=p.repository,
        sha=p.head_sha,
        workflow=p.workflow_path,
        job=p.job_name,
        image=p.runtime_digest,
        workspace=p.workspace,
        target=p.target,
        fingerprint=p.failure_fingerprint,
        require_file=[],
        output=output,
    )
    asyncio.run(module.build(args))
    assert Registry.model_validate_json(output.read_text()).profiles == [p]
    assert settings().runner_profiles == configured
    original = output.read_bytes()
    with pytest.raises(ValueError):
        asyncio.run(module.build(args))
    assert output.read_bytes() == original
