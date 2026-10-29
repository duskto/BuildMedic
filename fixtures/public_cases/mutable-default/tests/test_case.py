from demo.subject import append_value


def test_contract():
    assert append_value(1) == [1]
    assert append_value(2) == [2]
