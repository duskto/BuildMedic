from demo.subject import unique


def test_empty_and_single():
    assert unique([]) == []
    assert unique([1, 1]) == [1]
