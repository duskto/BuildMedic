import difflib
import shutil

import pytest
from sqlalchemy import update

from buildmedic import db, store
from buildmedic.cases import case_dir, metadata
from buildmedic.patching import apply_patch, patch_from_edits
from buildmedic.schemas import CreateTask, Source, SourceEdit
from buildmedic.security import safe_path, sanitize


def test_patch_scope_atomicity_and_traversal(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(case_dir("pagination"), root)
    path = root / "src/demo/pagination.py"
    original = path.read_text()
    fixed = original.replace("start + page_size - 1", "start + page_size")
    patch = "".join(
        difflib.unified_diff(
            original.splitlines(True),
            fixed.splitlines(True),
            "a/src/demo/pagination.py",
            "b/src/demo/pagination.py",
        )
    )
    bad_test = (
        "--- a/tests/test_pagination.py\n+++ b/tests/test_pagination.py\n@@ -1 +1 @@\n-import pytest\n+pass\n"
    )
    with pytest.raises(ValueError):
        apply_patch(root, patch + bad_test)
    assert path.read_text() == original
    for unsafe in ("../escape.py", "/tmp/escape.py", "src/../../escape.py", "src\\escape.py"):
        with pytest.raises(ValueError):
            safe_path(unsafe)
    (root / "src/demo/link.py").symlink_to(path)
    with pytest.raises(ValueError):
        apply_patch(root, patch.replace("pagination.py", "link.py"))
    assert apply_patch(root, patch) == ["src/demo/pagination.py"]
    assert path.read_text() == fixed


def test_redaction():
    result = sanitize(
        "\x1b[31mAuthorization: Bearer top-secret\x1b[0m\nAPI_KEY=secret-value https://example.com/log?token=private\n"
    )
    for secret in ("top-secret", "secret-value", "token=private", "\x1b"):
        assert secret not in result


def test_exact_edit_generates_strict_diff_without_mutating_snapshot(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(case_dir("pagination"), root)
    path = root / "src/demo/pagination.py"
    original = path.read_text()
    patch = patch_from_edits(
        root,
        [
            SourceEdit(
                path="src/demo/pagination.py", before="start + page_size - 1", after="start + page_size"
            )
        ],
    )
    assert path.read_text() == original
    apply_patch(root, patch)
    assert path.read_text() == original.replace("start + page_size - 1", "start + page_size")


@pytest.mark.parametrize(
    "path,before,after",
    [
        ("../escape.py", "x", "y"),
        ("tests/test_pagination.py", "import pytest", "pass"),
        ("src/demo/pagination.py", "missing unique context", "x"),
        ("src/demo/pagination.py", "page", "renamed"),
        ("src/demo/pagination.py", "start", "start"),
    ],
)
def test_exact_edits_keep_scope_and_exact_context_guards(tmp_path, path, before, after):
    root = tmp_path / "repo"
    shutil.copytree(case_dir("pagination"), root)
    original = (root / "src/demo/pagination.py").read_text()
    with pytest.raises(ValueError):
        patch_from_edits(root, [SourceEdit(path=path, before=before, after=after)])
    assert (root / "src/demo/pagination.py").read_text() == original


def test_duplicate_claim_and_expired_fence():
    body = CreateTask(source=Source(case_id="pagination")).model_dump()
    tid = store.create("owner", "claim-key", body, metadata("pagination"))
    token, *_ = store.claim(tid)
    assert store.claim(tid) is None
    with db.transaction() as conn:
        conn.execute(update(db.tasks).where(db.tasks.c.id == tid).values(heartbeat_at=0))
    with pytest.raises(store.LostLease):
        store.mutate(tid, "phase.changed", {"phase": "report"}, token=token)
    with pytest.raises(store.LostLease):
        store.artifact(tid, "log", "late bytes", token)
    with pytest.raises(store.LostLease):
        store.heartbeat(tid, token)


def test_repaired_requires_independent_evidence():
    tid = store.create(
        "owner",
        "repair-key",
        CreateTask(source=Source(case_id="pagination")).model_dump(),
        metadata("pagination"),
    )
    token, *_ = store.claim(tid)
    with pytest.raises(ValueError, match="independent"):
        store.finish(tid, token, "completed", "repaired")
