from pathlib import PurePosixPath


def is_within(path, root):
    candidate, base = PurePosixPath(path), PurePosixPath(root)
    if ".." in candidate.parts or ".." in base.parts:
        return False
    return str(candidate).startswith(str(base))
