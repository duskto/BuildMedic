import hashlib
import hmac
import re
import secrets
from pathlib import PurePosixPath
from .config import settings

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*\x07)")


def sanitize(value: str) -> str:
    value = ANSI.sub("", value)
    value = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer|token)?\s*)\S+", r"\1[REDACTED]", value)
    value = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{12,})", "[REDACTED]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    value = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[REDACTED]", value)
    for secret in (settings().model_api_key, settings().github_token, settings().runner_token):
        if secret and len(secret) >= 8:
            value = value.replace(secret, "[REDACTED]")
    return value.replace("\x00", "")


def safe_path(value: str):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value or not value:
        raise ValueError("路径越界或格式不合法")
    return path


def session_key():
    cfg = settings()
    if cfg.session_secret:
        if len(cfg.session_secret) < 32:
            raise RuntimeError("session_secret must contain at least 32 characters")
        return cfg.session_secret.encode()
    path = cfg.data_dir / "session.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x") as file:
            file.write(secrets.token_hex(32))
        path.chmod(0o600)
    except FileExistsError:
        pass
    return path.read_bytes()


def sign_owner(owner):
    return owner + "." + hmac.new(session_key(), owner.encode(), hashlib.sha256).hexdigest()


def read_owner(cookie):
    if cookie and re.fullmatch(r"[a-f0-9]{32}\.[a-f0-9]{64}", cookie):
        owner = cookie.split(".")[0]
        if hmac.compare_digest(sign_owner(owner), cookie):
            return owner
    return None
