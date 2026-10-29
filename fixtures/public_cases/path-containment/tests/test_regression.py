from demo.subject import is_within


def test_nested_and_parent():
    assert is_within('/safe/a', '/safe') is True
    assert is_within('/safe', '/safe') is True
    assert is_within('/safe/../other', '/safe') is False
