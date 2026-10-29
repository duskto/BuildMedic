from demo.subject import unique


def test_contract():
    assert unique([3, 1, 3, 2, 1]) == [3, 1, 2]
