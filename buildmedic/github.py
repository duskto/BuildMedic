"""Read-only GitHub ingestion; caller input never becomes a download host."""

import asyncio
import json
import math
import re
import time
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import httpx

from .config import settings
from .errors import AppError
from .profiles import match_profile, registry
from .security import sanitize

RUN_PATH = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/actions/runs/([1-9][0-9]*)/?$")
MAX_READ_ATTEMPTS = 3
MAX_RETRY_WAIT = 2.0
TRANSIENT_STATUSES = {500, 502, 503, 504}


def retry_information(headers):
    """Expose only parsed timing fields; never echo arbitrary upstream headers."""
    now = time.time()
    deadlines, details = [], {}
    value = headers.get("retry-after", "")
    if value and len(value) <= 128:
        try:
            delay = float(value) if value.isascii() and value.isdigit() else None
            at = now + delay if delay is not None else parsedate_to_datetime(value).timestamp()
            if math.isfinite(at) and at >= 0:
                deadlines.append(max(now, min(at, 253402300799)))
        except (ValueError, TypeError, OverflowError):
            pass
    value = headers.get("x-ratelimit-reset", "")
    if value.isascii() and value.isdigit() and len(value) <= 12:
        reset = int(value)
        details["reset_at"] = str(reset)
        if headers.get("x-ratelimit-remaining") == "0" and now < reset <= 253402300799:
            deadlines.append(float(reset))
    if deadlines:
        details["retry_at"] = max(deadlines)
    return details


def parse_run_url(url):
    parsed = urlparse(url)
    match = RUN_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or not match:
        raise AppError(
            "invalid_run_url", "请输入 https://github.com/owner/repo/actions/runs/123 形式的完整地址"
        )
    owner, repo, run_id = match.groups()
    if owner in {".", ".."} or repo in {".", ".."}:
        raise AppError("invalid_run_url", "仓库路径不合法")
    query = parse_qs(parsed.query)
    values = query.get("attempt", query.get("run_attempt", []))
    attempt = None
    if values:
        if len(values) != 1 or not re.fullmatch(r"[1-9][0-9]*", values[0]):
            raise AppError("invalid_attempt", "Run attempt 必须是正整数")
        attempt = int(values[0])
    return f"{owner}/{repo}", int(run_id), attempt


class GitHubClient:
    def __init__(self, transport=None, download_transport=None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if settings().github_token:
            headers["Authorization"] = "Bearer " + settings().github_token
        self.client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=headers,
            timeout=20,
            follow_redirects=False,
            transport=transport,
        )
        self.download_transport = download_transport

    async def close(self):
        await self.client.aclose()

    def check(self, response):
        timing = retry_information(response.headers)
        if response.status_code in (403, 429):
            limited = (
                response.status_code == 429
                or response.headers.get("x-ratelimit-remaining") == "0"
                or "retry_at" in timing
            )
            if limited and "retry_at" not in timing:
                timing["retry_at"] = time.time() + 60
            raise AppError(
                "github_rate_limited" if limited else "github_forbidden",
                "GitHub 已限流，请在允许重试时间之后重试"
                if limited
                else "GitHub 拒绝读取，请检查服务端凭据与 Actions 读取权限",
                503,
                limited,
                timing,
            )
        if response.status_code in (404, 410):
            raise AppError("github_unavailable", "运行记录或日志不可用，可能已过期或无读取权限", 404)
        if response.status_code >= 400:
            raise AppError(
                "github_error",
                "GitHub 返回错误，请稍后重试",
                502,
                response.status_code in TRANSIENT_STATUSES,
                {"http_status": response.status_code, **timing},
            )

    async def retry_read(self, operation):
        """Retry complete idempotent reads, discarding partial bodies and expired links."""
        for attempt in range(MAX_READ_ATTEMPTS):
            try:
                return await operation()
            except httpx.RequestError:
                error = AppError("github_connection", "无法连接 GitHub，请检查后端网络", 503, True)
            except AppError as exc:
                error = exc
            delay = max(0.25 * 2**attempt, error.details.get("retry_at", 0) - time.time())
            if not error.retryable or attempt == MAX_READ_ATTEMPTS - 1 or delay > MAX_RETRY_WAIT:
                raise error from None
            await asyncio.sleep(delay)

    async def read_limited(self, response, limit, code, message):
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise AppError(code, message, 413)
            chunks.append(chunk)
        return b"".join(chunks)

    async def get(self, path, **kwargs):
        async def read():
            async with self.client.stream("GET", path, **kwargs) as response:
                self.check(response)
                if response.is_redirect:
                    raise AppError("github_redirect", "GitHub 资源地址已变更，请使用仓库当前的规范地址", 422)
                content = await self.read_limited(
                    response, 4_000_000, "github_response_too_large", "运行元数据超出读取上限"
                )
                try:
                    return json.loads(content)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise AppError("github_invalid_response", "GitHub 返回的元数据格式无效", 502) from exc

        return await self.retry_read(read)

    async def preview(self, url, fixed_attempt=None):
        repo, run_id, url_attempt = parse_run_url(url)
        repository = await self.get(f"/repos/{repo}")
        if repository.get("private"):
            raise AppError("private_repository", "当前版本仅接入公开仓库", 422)
        repo = repository.get("full_name", repo)
        run = await self.get(f"/repos/{repo}/actions/runs/{run_id}")
        attempt = fixed_attempt or url_attempt or run["run_attempt"]
        if url_attempt and fixed_attempt and url_attempt != fixed_attempt:
            raise AppError("attempt_mismatch", "URL 与所选 Run attempt 不一致", 422)
        fixed = await self.get(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")
        if (
            fixed["id"] != run_id
            or fixed["run_attempt"] != attempt
            or not re.fullmatch(r"[a-fA-F0-9]{40}", fixed["head_sha"])
        ):
            raise AppError("github_inconsistent", "GitHub 返回的运行版本不一致", 502)
        jobs = []
        profiles = registry().profiles
        head_repo = (fixed.get("head_repository") or run.get("head_repository") or {}).get("full_name")
        workflow_path = fixed.get("path")
        for page in range(1, 101):
            data = await self.get(
                f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
                params={"per_page": 100, "page": page},
            )
            batch = data["jobs"]
            for job in batch:
                if (
                    job.get("run_id") != run_id
                    or job.get("run_attempt", attempt) != attempt
                    or job.get("head_sha", fixed["head_sha"]) != fixed["head_sha"]
                ):
                    raise AppError("github_inconsistent", "Job 与固定运行版本不一致", 502)
                if job.get("conclusion") in {"failure", "timed_out", "action_required", "startup_failure"}:
                    profile = match_profile(
                        repo, fixed["head_sha"], workflow_path, job["name"], head_repo, candidates=profiles
                    )
                    jobs.append(
                        {
                            "id": job["id"],
                            "name": sanitize(job["name"]),
                            "status": job.get("status"),
                            "conclusion": job["conclusion"],
                            "execution_supported": profile is not None,
                            "profile_version": profile.id if profile else None,
                            "failed_steps": [
                                {"number": s["number"], "name": sanitize(s["name"])}
                                for s in job.get("steps", [])
                                if s.get("conclusion") == "failure"
                            ],
                        }
                    )
            if len(batch) < 100:
                break
        else:
            raise AppError("github_too_many_jobs", "Job 数量超出此版本的接入上限", 422)
        return {
            "repo": repo,
            "run_id": run_id,
            "run_attempt": attempt,
            "head_sha": fixed["head_sha"],
            "jobs": jobs,
            "profile_version": None,
            "execution_supported": any(j["execution_supported"] for j in jobs),
            "head_repository": head_repo,
            "workflow_path": workflow_path,
            "message": "按所选 Job 的登记状态进行独立复现或仅日志诊断；本地验证不代表远端 CI 成功"
            if any(j["execution_supported"] for j in jobs)
            else "尚未登记复现环境，可进行日志诊断；不能自动执行或验证该仓库",
        }

    async def logs(self, repo, job_id):
        async def read():
            async with self.client.stream("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs") as response:
                self.check(response)
                if not response.is_redirect:
                    content = await self.read_limited(response, 2_000_000, "log_too_large", "日志超过 2 MB")
                    return sanitize(content.decode("utf-8", errors="replace"))
                location = response.headers.get("location", "")
            try:
                parsed = urlparse(location)
                host = parsed.hostname or ""
                safe = (
                    parsed.scheme == "https"
                    and not parsed.username
                    and not parsed.password
                    and parsed.port in (None, 443)
                    and host.endswith((".blob.core.windows.net", ".actions.githubusercontent.com"))
                )
            except ValueError:
                safe = False
            if not safe:
                raise AppError("unsafe_log_redirect", "GitHub 日志下载域未通过校验", 502)
            # Separate client: never forward API credentials to the download host.
            async with (
                httpx.AsyncClient(
                    timeout=30, follow_redirects=False, transport=self.download_transport
                ) as downloader,
                downloader.stream("GET", location) as stream,
            ):
                if stream.status_code in (401, 403):
                    raise AppError("github_log_link_expired", "日志下载链接已失效，请重新读取", 503, True)
                self.check(stream)
                if stream.is_redirect:
                    raise AppError("unsafe_log_redirect", "日志发生额外重定向，已停止下载", 502)
                content = await self.read_limited(stream, 2_000_000, "log_too_large", "日志超过 2 MB")
                return sanitize(content.decode("utf-8", errors="replace"))

        return await self.retry_read(read)
