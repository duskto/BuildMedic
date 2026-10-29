from demo.subject import is_within


def test_contract():
    assert is_within('/safe-other/file', '/safe') is False
