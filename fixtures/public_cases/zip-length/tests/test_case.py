import pytest
from demo.subject import pair_records


def test_contract():
    with pytest.raises(ValueError):
        pair_records(['a', 'b'], [1])
    with pytest.raises(ValueError):
        pair_records(['a'], [1, 2])
