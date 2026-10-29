import pytest
from demo.subject import reduce_fraction


def test_positive_and_zero():
    assert reduce_fraction(6, 9) == (2, 3)
    assert reduce_fraction(0, 4) == (0, 1)
    with pytest.raises(ValueError):
        reduce_fraction(1, 0)
