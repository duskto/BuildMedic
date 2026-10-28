"""Bounded failure indexes and source-preserving views of sanitized log artifacts."""

import hashlib
import re
from typing import Literal

from pydantic import Field

from .errors import AppError
from .schemas import StrictModel

LOG_KINDS = {"ci_log", "baseline_log", "test_log"}
TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s*")
MARKERS = (
    ("traceback", 100, re.compile(r"Traceback \(most recent call last\):")),
    (
        "exception",
        95,
        re.compile(
            r"\b(?:AssertionError|ModuleNotFoundError|ImportError|TypeError|ValueError|AttributeError|RuntimeError|SyntaxError|NameError|KeyError|IndexError|ZeroDivisionError)\b"
        ),
    ),
    (
        "test_failure",
        90,
        re.compile(r"(?:^|\s)(?:FAILED\s|FAIL:|ERROR collecting\s|={2,}\s*(?:FAILURES|ERRORS))"),
    ),
    (
        "dependency",
        85,
        re.compile(
            r"(?:Could not find a version|No matching distribution|ResolutionImpossible|Cannot find module|npm ERR!)",
            re.IGNORECASE,
        ),
    ),
    (
        "ci_error",
        80,
        re.compile(
            r"(?:##\[error\]|Process completed with exit code [1-9]|(?:^|\s)error(?:\[[^\]]+\])?:)",
            re.IGNORECASE,
        ),
    ),
)


class LogSection(StrictModel):
    kind: Literal["traceback", "exception", "test_failure", "dependency", "ci_error"]
    line_start: int
    line_end: int
    marker_line: int
    priority: int
    step: str | None = None
    timestamp: str | None = None


class LogIndex(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    total_lines: int
    size_bytes: int
    sections: list[LogSection] = Field(max_length=24)
    sections_truncated: bool


class LogLine(StrictModel):
    number: int
    content: str
    column: int
    complete: bool


class LogWindow(StrictModel):
    artifact_id: str
    sha256: str
    total_lines: int
    lines: list[LogLine]
    next_line: int | None
    next_column: int | None


def index_log(text):
    """Line numbers refer to the complete sanitized artifact, never the excerpt."""
    lines = text.splitlines(keepends=True)
    sections, step = [], None
    for at, line in enumerate(lines):
        timestamp = TIMESTAMP.match(line)
        content = line[timestamp.end() :] if timestamp else line
        if "##[group]" in content:
            step = content.split("##[group]", 1)[1].strip()[:200] or None
        marker = next(
            ((kind, priority) for kind, priority, pattern in MARKERS if pattern.search(content)), None
        )
        if marker is None:
            continue
        kind, priority = marker
        section = {
            "kind": kind,
            "priority": priority,
            "marker_line": at + 1,
            "line_start": max(1, at - 3),
            "line_end": min(len(lines), at + 21),
            "step": step,
            "timestamp": timestamp.group(1) if timestamp else None,
        }
        if (
            sections
            and section["step"] == sections[-1]["step"]
            and section["line_start"] <= sections[-1]["line_end"] + 1
            and section["line_end"] - sections[-1]["line_start"] < 80
        ):
            previous = sections[-1]
            if priority > previous["priority"]:
                previous.update(
                    {k: section[k] for k in ("kind", "priority", "marker_line", "step", "timestamp")}
                )
            previous["line_end"] = max(previous["line_end"], section["line_end"])
        else:
            sections.append(section)
    truncated = len(sections) > 24
    if truncated:
        # Retain the earliest and latest failures instead of dropping the end of a large job.
        sections = sections[:12] + sections[-12:]
    return LogIndex(
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        total_lines=len(lines),
        size_bytes=len(text.encode()),
        sections=sections,
        sections_truncated=truncated,
    ).model_dump()


def context_log(text, budget=10000):
    """Keep short logs verbatim; select failure windows and record all omissions for long logs."""
    if budget < 1024:
        raise ValueError("日志上下文预算至少需要 1024 字节")
    index = index_log(text)
    lines = text.splitlines(keepends=True)
    ranges, truncated_lines = [], []
    if index["size_bytes"] <= budget:
        selected, content = set(range(1, len(lines) + 1)), text
        if lines:
            ranges.append({"line_start": 1, "line_end": len(lines)})
    else:
        sections = sorted(index["sections"], key=lambda s: (-s["priority"], s["marker_line"]))[:4]
        # Include both the job setup and its final outcome, even without recognized failures.
        windows = [(s["line_start"], s["line_end"], s["marker_line"]) for s in sections]
        windows.extend(
            [(1, min(len(lines), 3), 1), (max(1, len(lines) - 7), len(lines), max(1, len(lines) - 7))]
        )
        selected, fragments = set(), {}
        allowance = (budget - 512) // len(windows)
        for start, end, anchor in windows:
            prefix = sum(len(line.encode()) for line in lines[start - 1 : anchor - 1])
            if prefix > allowance // 3:
                start = anchor
            remaining = allowance
            for number in range(start, end + 1):
                if number in selected:
                    continue
                raw = lines[number - 1].encode()
                piece = raw[:remaining].decode("utf-8", errors="ignore")
                if not piece:
                    break
                selected.add(number)
                fragments[number] = piece
                remaining -= len(piece.encode())
                if len(piece.encode()) != len(raw):
                    truncated_lines.append(number)
                    break
                if remaining == 0:
                    break
        for number in sorted(selected):
            if ranges and number == ranges[-1]["line_end"] + 1:
                ranges[-1]["line_end"] = number
            else:
                ranges.append({"line_start": number, "line_end": number})
        # Headers identify original lines and are not part of the source content.
        content = "\n".join(
            f"[原日志 L{r['line_start']}–L{r['line_end']}]\n"
            + "".join(fragments[n] for n in range(r["line_start"], r["line_end"] + 1))
            for r in ranges
        )
    omitted, after = [], 1
    for item in ranges:
        if after < item["line_start"]:
            omitted.append({"line_start": after, "line_end": item["line_start"] - 1})
        after = item["line_end"] + 1
    if after <= len(lines):
        omitted.append({"line_start": after, "line_end": len(lines)})
    return {
        "text": content,
        "selection": {
            "sha256": index["sha256"],
            "total_lines": len(lines),
            "size_bytes": index["size_bytes"],
            "selected_ranges": ranges,
            "omitted_ranges": omitted,
            "truncated_lines": sorted(truncated_lines),
            "truncated": bool(omitted or truncated_lines),
        },
    }


def log_window(artifact, start_line=1, start_column=0, max_lines=100):
    if artifact["kind"] not in LOG_KINDS:
        raise AppError("not_log_artifact", "此产物不支持日志分页", 422)
    text = artifact["content"]
    if hashlib.sha256(text.encode()).hexdigest() != artifact["sha256"]:
        raise AppError("artifact_integrity", "日志产物完整性校验失败", 409)
    lines = text.splitlines(keepends=True)
    if not lines:
        if start_line != 1 or start_column:
            raise AppError("invalid_log_range", "请求位置超出空日志范围", 422)
        return LogWindow(
            artifact_id=artifact["id"],
            sha256=artifact["sha256"],
            total_lines=0,
            lines=[],
            next_line=None,
            next_column=None,
        )
    if not 1 <= start_line <= len(lines) or start_column < 0 or start_column >= len(lines[start_line - 1]):
        raise AppError("invalid_log_range", "请求位置超出日志范围", 422)
    result, remaining, next_line, next_column = [], 16000, None, None
    for number in range(start_line, min(len(lines), start_line + max_lines - 1) + 1):
        column = start_column if number == start_line else 0
        raw = lines[number - 1][column:].encode()
        piece = raw[:remaining].decode("utf-8", errors="ignore")
        if not piece:
            next_line, next_column = number, column
            break
        complete = len(piece.encode()) == len(raw)
        result.append(LogLine(number=number, content=piece, column=column, complete=complete))
        remaining -= len(piece.encode())
        if not complete:
            next_line, next_column = number, column + len(piece)
            break
        if number < len(lines):
            next_line, next_column = number + 1, 0
        else:
            next_line, next_column = None, None
    return LogWindow(
        artifact_id=artifact["id"],
        sha256=artifact["sha256"],
        total_lines=len(lines),
        lines=result,
        next_line=next_line,
        next_column=next_column,
    )
