"""Bounded public GitHub archive ingestion. Never extracts links or executes repo files."""

import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import httpx

from .errors import Blocked
from .patching import editable_source
from .profiles import HASH, REPO, SHA, fingerprint, registered, relative_path
from .security import sanitize

COMPRESSED_LIMIT = 8_000_000
EXPANDED_LIMIT = 32_000_000
FILE_LIMIT = 2_000_000
COUNT_LIMIT = 1500


def archive_files(data):
    if len(data) > COMPRESSED_LIMIT:
        raise ValueError("仓库压缩包超过 8 MB 上限")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            raw = stream.read(EXPANDED_LIMIT + 1)
        if len(raw) > EXPANDED_LIMIT:
            raise ValueError("仓库归档展开超过 32 MB 上限")
        files, directories, seen, prefix = {}, set(), set(), None
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for index, member in enumerate(archive):
                if index >= COUNT_LIMIT:
                    raise ValueError("仓库归档条目过多")
                name = member.name.rstrip("/") if member.isdir() else member.name
                relative_path(name)
                path = PurePosixPath(name)
                if prefix is None:
                    prefix = path.parts[0]
                if path.parts[0] != prefix or name in seen:
                    raise ValueError("归档根目录不一致或条目重复")
                seen.add(name)
                if member.issparse() or not (
                    member.isdir() or member.type in (tarfile.REGTYPE, tarfile.AREGTYPE)
                ):
                    raise ValueError("归档包含链接、设备或非普通文件")
                if len(path.parts) == 1:
                    if not member.isdir():
                        raise ValueError("归档根必须是目录")
                    continue
                rel = PurePosixPath(*path.parts[1:]).as_posix()
                if member.isdir():
                    directories.add(rel)
                else:
                    if member.size < 0 or member.size > FILE_LIMIT:
                        raise ValueError("归档单个文件超过上限")
                    content = archive.extractfile(member).read(FILE_LIMIT + 1)
                    if len(content) != member.size:
                        raise ValueError("归档文件长度不一致")
                    files[rel] = content
                for parent in PurePosixPath(rel).parents:
                    if parent.as_posix() != ".":
                        directories.add(parent.as_posix())
                if len(files) + len(directories) > COUNT_LIMIT:
                    raise ValueError("仓库展开后的文件与目录条目过多")
        if not files or set(files) & directories:
            raise ValueError("归档为空或文件与目录冲突")
        return files, directories
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise ValueError("仓库归档损坏或格式无效") from exc


def inventory(files, directories):
    return {
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        "directories": sorted(directories),
    }


def directory_inventory(root):
    files, directories = {}, set()
    size = 0
    for index, path in enumerate(root.rglob("*")):
        if index >= COUNT_LIMIT:
            raise ValueError("固定快照条目超过上限")
        if path.is_symlink():
            raise ValueError("固定快照出现符号链接")
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.add(name)
        elif path.is_file():
            size += path.stat().st_size
            if size > EXPANDED_LIMIT:
                raise ValueError("固定快照总大小超过上限")
            if path.stat().st_size > FILE_LIMIT:
                raise ValueError("固定快照文件过大")
            files[name] = path.read_bytes()
        else:
            raise ValueError("固定快照出现非普通文件")
    return inventory(files, directories)


def protected_hash(root):
    entries = directory_inventory(root)
    entries["files"] = {name: sha for name, sha in entries["files"].items() if not editable_source(name)}
    return fingerprint(entries)


def validate_files(profile, files):
    expected = {profile.workflow_path: profile.workflow_sha256, **profile.required_files}
    for name, sha in expected.items():
        if name not in files or hashlib.sha256(files[name]).hexdigest() != sha:
            raise ValueError("workflow 或登记依赖文件的指纹不匹配")
    workflow = files[profile.workflow_path].decode("utf-8")
    if len(workflow.encode()) > 20000 or sanitize(workflow) != workflow:
        raise ValueError("workflow 超出读取限制或含需脱敏的内容")
    prefix = "" if profile.workspace == "." else profile.workspace + "/"
    if prefix + profile.target not in files:
        raise ValueError("固定快照缺少登记的目标测试")
    source = {
        name[len(prefix) :]: data.decode("utf-8")
        for name, data in files.items()
        if name.startswith(prefix + "src/") and name.endswith(".py")
    }
    if not source or len(source) > 40 or sum(len(v.encode()) for v in source.values()) > 160000:
        raise ValueError("源码为空或超过受限诊断的输入规模")
    if any(len(text.encode()) > 20000 or sanitize(text) != text for text in source.values()):
        raise ValueError("源码文件过大或含需脱敏的内容，未向模型发送")
    return workflow, source


class SnapshotStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, binding):
        return self.root / fingerprint(binding)

    def describe(self, binding):
        profile = registered(binding)
        dest = self.path(binding)
        repo = dest / "repo"
        if dest.is_symlink() or repo.is_symlink() or not repo.is_dir():
            raise ValueError("登记快照尚未准备或缓存无效")
        manifest = dest / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 2_000_000:
            raise ValueError("固定快照清单无效")
        record = json.loads(manifest.read_text())
        current = directory_inventory(repo)
        if (
            not isinstance(record, dict)
            or record.get("binding") != binding
            or fingerprint(current) != profile.snapshot_sha256
            or current != record.get("inventory")
            or not re.fullmatch(HASH, str(record.get("archive_sha256", "")))
        ):
            raise ValueError("固定快照缓存的完整性校验失败")
        files = {name: (repo / name).read_bytes() for name in current["files"]}
        workflow, source = validate_files(profile, files)
        return {
            "binding": binding,
            "archive_sha256": record["archive_sha256"],
            "inventory": current,
            "workflow_path": profile.workflow_path,
            "workflow": workflow,
            "source": source,
        }

    def install(self, binding, data):
        profile = registered(binding)
        files, directories = archive_files(data)
        record = inventory(files, directories)
        if fingerprint(record) != profile.snapshot_sha256:
            raise ValueError("下载快照与登记的 SHA256 不一致")
        validate_files(profile, files)
        key = fingerprint(binding)
        with (self.root / (key + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path(binding).exists() or self.path(binding).is_symlink():
                return self.describe(binding)
            temp = Path(tempfile.mkdtemp(prefix="snapshot-", dir=self.root))
            try:
                repo = temp / "repo"
                repo.mkdir()
                for name in sorted(directories):
                    (repo / name).mkdir(parents=True, exist_ok=True)
                for name, data_bytes in files.items():
                    (repo / name).parent.mkdir(parents=True, exist_ok=True)
                    (repo / name).write_bytes(data_bytes)
                (temp / "manifest.json").write_text(
                    json.dumps(
                        {
                            "binding": binding,
                            "inventory": record,
                            "archive_sha256": hashlib.sha256(data).hexdigest(),
                        },
                        ensure_ascii=False,
                    )
                )
                os.rename(temp, self.path(binding))
            finally:
                if temp.exists():
                    shutil.rmtree(temp)
        return self.describe(binding)

    async def prepare(self, binding, transport=None):
        import asyncio

        registered(binding)
        if self.path(binding).exists() or self.path(binding).is_symlink():
            return await asyncio.to_thread(self.describe, binding)
        content = await download_archive(binding["repository"], binding["head_sha"], transport)
        return await asyncio.to_thread(self.install, binding, content)


async def download_archive(repository, sha, transport=None):
    if (
        not re.fullmatch(REPO, repository)
        or not re.fullmatch(SHA, sha)
        or any(p in {".", ".."} for p in repository.split("/"))
    ):
        raise ValueError("归档必须来自规范仓库和完整提交 SHA")
    # Direct official public archive URL, no API token/cookies and no redirects.
    url = f"https://codeload.github.com/{repository}/tar.gz/{sha}"
    try:
        async with (
            httpx.AsyncClient(timeout=30, follow_redirects=False, transport=transport) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise Blocked("固定 SHA 的公开仓库归档不可用，未执行仓库代码")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > COMPRESSED_LIMIT:
                    raise ValueError("仓库压缩包超过 8 MB 上限")
        return bytes(content)
    except httpx.HTTPError as exc:
        raise Blocked("无法下载固定 SHA 的仓库快照，请检查 Runner 网络") from exc
