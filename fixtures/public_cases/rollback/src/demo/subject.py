from contextlib import contextmanager


@contextmanager
def transaction(mapping):
    snapshot = mapping.copy()
    try:
        yield mapping
    except Exception:
        mapping.update(snapshot)
        raise
