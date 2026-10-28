"""Reuse completed logs only after the caller revalidates their public Run binding."""

import hashlib
import json

from sqlalchemy import delete, select

from . import db, store
from .config import settings
from .errors import AppError
from .security import sanitize

CACHE_TTL = 86400
MAX_OWNER_ENTRIES = 20
MAX_ENTRIES = 200
MAX_BYTES = 32_000_000
MAX_LOG_BYTES = 6_000_000  # 2 MB download, allowing UTF-8 replacement expansion.


def digest(content):
    return hashlib.sha256(content.encode()).hexdigest()


def cache_key(owner, binding):
    return digest(json.dumps([owner, binding], sort_keys=True, separators=(",", ":")))


def read_entry(conn, key, owner, binding):
    entry = (
        conn.execute(
            select(db.ci_log_cache).where(db.ci_log_cache.c.key == key, db.ci_log_cache.c.owner_id == owner)
        )
        .mappings()
        .first()
    )
    if not entry:
        return None
    content = entry["content"]
    if (
        entry["binding"] != binding
        or entry["expires_at"] <= db.now()
        or entry["created_at"] > db.now()
        or entry["expires_at"] - entry["created_at"] > CACHE_TTL
        or not content.strip()
        or len(content.encode()) != entry["size_bytes"]
        or entry["size_bytes"] > MAX_LOG_BYTES
        or digest(content) != entry["sha256"]
        or sanitize(content) != content
    ):
        conn.execute(delete(db.ci_log_cache).where(db.ci_log_cache.c.key == key))
        return None
    return dict(entry)


def prune(conn):
    conn.execute(delete(db.ci_log_cache).where(db.ci_log_cache.c.expires_at <= db.now()))
    entries = (
        conn.execute(
            select(db.ci_log_cache.c.key, db.ci_log_cache.c.owner_id, db.ci_log_cache.c.size_bytes).order_by(
                db.ci_log_cache.c.created_at.desc(), db.ci_log_cache.c.key
            )
        )
        .mappings()
        .all()
    )
    owners, size, count, remove = {}, 0, 0, []
    for entry in entries:
        owner = entry["owner_id"]
        if (
            owners.get(owner, 0) >= MAX_OWNER_ENTRIES
            or count >= MAX_ENTRIES
            or size + entry["size_bytes"] > MAX_BYTES
        ):
            remove.append(entry["key"])
            continue
        owners[owner] = owners.get(owner, 0) + 1
        size += entry["size_bytes"]
        count += 1
    if remove:
        conn.execute(delete(db.ci_log_cache).where(db.ci_log_cache.c.key.in_(remove)))


def origin(source, binding, content, entry=None):
    return {
        "source": source,
        "binding": binding,
        "sha256": digest(content),
        "size_bytes": len(content.encode()),
        "cached_at": entry["created_at"] if entry else None,
        "expires_at": entry["expires_at"] if entry else None,
    }


async def read_job_log(github, owner, preview, job_id):
    """No network inside DB transactions; no API headers or signed URLs are cached."""
    binding = {k: preview[k] for k in ("repo", "run_id", "run_attempt", "head_sha")}
    binding["repo"] = binding["repo"].lower()
    binding["head_sha"] = binding["head_sha"].lower()
    binding["job_id"] = job_id
    job = next(j for j in preview["jobs"] if j["id"] == job_id)
    enabled = settings().github_log_cache_enabled and job.get("status") == "completed"
    key = cache_key(owner, binding)
    if enabled:
        with db.transaction() as conn:
            store.lock_owner(conn, "ci-log-cache")
            entry = read_entry(conn, key, owner, binding)
        if entry:
            return entry["content"], origin("cache", binding, entry["content"], entry)
    content = sanitize(await github.logs(preview["repo"], job_id))
    entry = None
    if enabled and content.strip() and len(content.encode()) <= MAX_LOG_BYTES:
        with db.transaction() as conn:
            store.lock_owner(conn, "ci-log-cache")
            entry = read_entry(conn, key, owner, binding)
            if entry and entry["sha256"] != digest(content):
                raise AppError("github_log_changed", "同一固定 Job 的日志内容发生变化，已停止诊断", 409)
            if not entry:
                now = db.now()
                entry = {
                    "key": key,
                    "owner_id": owner,
                    "binding": binding,
                    "created_at": now,
                    "expires_at": now + CACHE_TTL,
                    "sha256": digest(content),
                    "size_bytes": len(content.encode()),
                    "content": content,
                }
                conn.execute(db.ci_log_cache.insert().values(**entry))
            prune(conn)
    return content, origin("network", binding, content, entry)
