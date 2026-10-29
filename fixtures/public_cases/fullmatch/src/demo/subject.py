import re


def valid_identifier(value):
    return re.match(r"[A-Za-z_][A-Za-z_0-9]*", value) is not None
