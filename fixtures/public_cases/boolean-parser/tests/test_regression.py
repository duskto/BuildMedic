import pytest
from demo.subject import parse_boolean


def test_true_and_rejection():
    assert parse_boolean('true') is True
    with pytest.raises(ValueError):
        parse_boolean('maybe')
