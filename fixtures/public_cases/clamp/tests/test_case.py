from demo.subject import clamp


def test_contract():
    assert clamp(5, 1, 9) == 5
    assert clamp(20, 1, 9) == 9
