"""Strict unified-diff application: source-only, no rename, creation or deletion."""
import re
from pathlib import Path
from .security import safe_path

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


def apply_patch(root: Path, patch: str):
    if not patch or len(patch.encode()) > 40000 or "\x00" in patch:
        raise ValueError("补丁为空或超出大小限制")
    lines = patch.splitlines(keepends=True)
    changes = {}
    changed_lines = 0
    i = 0
    while i < len(lines):
        if lines[i].startswith("diff --git ") or lines[i].startswith("index "):
            i += 1
            continue
        if not lines[i].startswith("--- a/") or i + 1 >= len(lines) or not lines[i+1].startswith("+++ b/"):
            raise ValueError("只接受标准 a/ 与 b/ 路径的 unified diff")
        old = lines[i][6:].strip()
        new = lines[i+1][6:].strip()
        path = safe_path(new)
        if old != new or path.parts[0] != "src" or path.suffix != ".py" or str(path) in changes:
            raise ValueError("仅允许修改 src/ 下已有 Python 文件，不能重命名或修改测试")
        target = root / path
        if any(p.is_symlink() for p in [target, *target.parents]) or not target.is_file():
            raise ValueError("不能修改符号链接或不存在的文件")
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("路径越界")
        original = target.read_text().splitlines(keepends=True)
        result, cursor = [], 0
        i += 2
        hunk_count = 0
        while i < len(lines) and lines[i].startswith("@@ "):
            header = HUNK.match(lines[i].rstrip("\n"))
            if not header:
                raise ValueError("hunk 头格式错误")
            start, count, new_start, new_count = [int(x) if x is not None else 1 for x in header.groups()]
            index = start - 1 if start else 0
            if index < cursor or index > len(original):
                raise ValueError("hunk 位置无效")
            result.extend(original[cursor:index])
            if new_start != len(result) + 1:
                raise ValueError("新文件行号不一致")
            cursor = index
            removed = added = 0
            i += 1
            while i < len(lines) and lines[i][:1] in {" ", "+", "-"} and not lines[i].startswith("--- a/"):
                line = lines[i]
                if line[0] in " -":
                    if cursor >= len(original) or original[cursor] != line[1:]:
                        raise ValueError("补丁上下文与固定快照不一致")
                    cursor += 1
                    removed += 1
                if line[0] in " +":
                    result.append(line[1:])
                    added += 1
                if line[0] in "+-":
                    changed_lines += 1
                i += 1
            if (removed, added) != (count, new_count):
                raise ValueError("hunk 行数不匹配")
            hunk_count += 1
        if not hunk_count:
            raise ValueError("补丁没有修改块")
        result.extend(original[cursor:])
        changes[str(path)] = "".join(result)
    if not changes or len(changes) > 5 or changed_lines > 200:
        raise ValueError("补丁修改范围超出限制")
    # Validate every file before writing any file.
    for path, content in changes.items():
        (root / path).write_text(content)
    return list(changes)
