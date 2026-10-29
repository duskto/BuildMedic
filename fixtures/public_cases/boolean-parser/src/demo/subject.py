def parse_boolean(value):
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError("expected true or false")
    return bool(normalized)
