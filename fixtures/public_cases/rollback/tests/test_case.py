import pytest
from demo.subject import transaction


def test_contract():
    data = {'a': 1}
    with pytest.raises(RuntimeError):
        with transaction(data):
            data['new'] = 2
            raise RuntimeError('rollback')
    assert data == {'a': 1}
