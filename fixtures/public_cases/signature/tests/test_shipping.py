import pytest
from demo.shipping import checkout, quote_order, shipping_cost

@pytest.mark.parametrize("fn", [checkout, quote_order])
@pytest.mark.parametrize("region,expected", [("local", 6), ("remote", 15)])
def test_region_propagation(fn, region, expected):
    assert fn(3, region) == expected


def test_direct_call():
    assert shipping_cost(1, region="local") == 2


def test_unknown_region():
    with pytest.raises(ValueError):
        checkout(1, "invalid")
