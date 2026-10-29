import pytest
from demo.subject import weighted_mean


def test_unit_and_invalid_weights():
    assert weighted_mean([2, 4], [1, 1]) == 3
    with pytest.raises(ValueError):
        weighted_mean([2], [0])
