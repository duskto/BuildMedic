def shipping_cost(weight, *, region):
    rates = {"local": 2, "remote": 5}
    if region not in rates:
        raise ValueError("unknown region")
    return weight * rates[region]


def checkout(weight, region):
    return shipping_cost(weight)


def quote_order(weight, region):
    return shipping_cost(weight)
