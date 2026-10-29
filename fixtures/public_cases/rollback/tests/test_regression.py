from demo.subject import transaction


def test_commit():
    data = {'a': 1}
    with transaction(data):
        data['a'] = 2
    assert data == {'a': 2}
