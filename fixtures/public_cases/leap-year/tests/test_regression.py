from demo.subject import is_leap


def test_ordinary_years():
    assert is_leap(2024) is True
    assert is_leap(2023) is False
