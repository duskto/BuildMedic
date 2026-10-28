"""Inspect a public fixed-SHA archive and produce an administrator-reviewed profile.

This command downloads data only. It never runs repository files or changes service settings.
"""

import argparse
import asyncio
import fcntl
import hashlib
import tempfile
from pathlib import Path

from buildmedic.config import settings
from buildmedic.errors import Blocked
from buildmedic.profiles import Registry, RunnerProfile, fingerprint
from buildmedic.snapshots import archive_files, download_archive, inventory, validate_files


async def build(args):
    provisional = RunnerProfile(
        id=args.id,
        repository=args.repository,
        head_sha=args.sha,
        snapshot_sha256="0" * 64,
        workflow_path=args.workflow,
        workflow_sha256="0" * 64,
        job_name=args.job,
        runtime_digest=args.image or settings().runner_image,
        workspace=args.workspace,
        target=args.target,
        failure_fingerprint=args.fingerprint,
    )
    data = await download_archive(provisional.repository, provisional.head_sha)
    files, directories = archive_files(data)
    if provisional.workflow_path not in files:
        raise ValueError("指定 SHA 中不存在 workflow 文件")
    required = {}
    for name in args.require_file:
        if name not in files:
            raise ValueError("指定 SHA 中缺少登记依赖文件")
        required[name] = hashlib.sha256(files[name]).hexdigest()
    profile = RunnerProfile.model_validate(
        provisional.model_dump()
        | {
            "snapshot_sha256": fingerprint(inventory(files, directories)),
            "workflow_sha256": hashlib.sha256(files[provisional.workflow_path]).hexdigest(),
            "required_files": required,
        }
    )
    validate_files(profile, files)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.with_name(args.output.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            registry = (
                Registry.model_validate_json(args.output.read_text()) if args.output.exists() else Registry()
            )
            registry = Registry.model_validate({"profiles": [*registry.profiles, profile]})
            text = registry.model_dump_json(indent=2) + "\n"
            if len(text.encode()) > 256000:
                raise ValueError("登记文件超过 256 KB 上限")
            with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, delete=False) as file:
                file.write(text)
                temp = Path(file.name)
            try:
                temp.replace(args.output)
            finally:
                temp.unlink(missing_ok=True)
        print("已生成登记文件：", args.output)
        print(
            "请核对离线镜像依赖和失败指纹后，在 API、Worker、Runner 配置相同的 BUILDMEDIC_RUNNER_PROFILES。"
        )
    else:
        print(Registry(profiles=[profile]).model_dump_json(indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("id", "repository", "sha", "workflow", "job", "target", "fingerprint"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--image", default="")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--require-file", action="append", default=[])
    parser.add_argument("--output", type=Path)
    try:
        asyncio.run(build(parser.parse_args()))
    except (OSError, ValueError, Blocked) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
