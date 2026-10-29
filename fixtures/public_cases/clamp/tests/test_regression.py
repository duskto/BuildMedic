import pytest
from demo.subject import clamp


def test_interval_validation():
    assert clamp(2, 2, 2) == 2
    with pytest.raises(ValueError):
        clamp(1, 5, 2)
