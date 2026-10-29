from demo.subject import is_leap


def test_contract():
    assert is_leap(1900) is False
    assert is_leap(2000) is True
