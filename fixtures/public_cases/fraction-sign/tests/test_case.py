from demo.subject import reduce_fraction


def test_contract():
    assert reduce_fraction(2, -4) == (-1, 2)
    assert reduce_fraction(-2, -4) == (1, 2)
