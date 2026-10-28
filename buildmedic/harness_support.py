"""Read package provenance without importing upstream runtimes in the API process."""

import json
from importlib.metadata import PackageNotFoundError, distribution

from .config import HARNESS_SHA, settings
from .errors import Blocked


def capability():
    try:
        dist = distribution("harness2")
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        verified = (
            direct.get("vcs_info", {}).get("commit_id") == HARNESS_SHA
            and direct.get("url", "").removesuffix(".git").rstrip("/")
            == "https://github.com/google-research/harness2"
        )
    except (PackageNotFoundError, ValueError):
        verified = False
    cfg = settings()
    ready = verified and cfg.adaptive_enabled and cfg.model_ready
    if not verified:
        message = "请运行 uv sync --extra adaptive，安装指南固定版本的 Harness²"
    elif not cfg.adaptive_enabled:
        message = "Harness² 已安装；请在服务端启用 BUILDMEDIC_ADAPTIVE_ENABLED"
    elif not cfg.model_ready:
        message = "Harness² 已安装，尚未配置真实模型服务"
    else:
        message = "Harness² k=1 顺序优化已就绪：4 次独立执行与 3 次策略提议，共享任务预算"
    return {"ready": ready, "installed": verified, "sha": HARNESS_SHA, "message": message}


def require_adaptive():
    value = capability()
    if not value["ready"]:
        raise Blocked(value["message"])
