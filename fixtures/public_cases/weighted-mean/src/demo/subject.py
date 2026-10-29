def weighted_mean(values, weights):
    if len(values) != len(weights) or not weights or sum(weights) <= 0:
        raise ValueError("invalid weights")
    return sum(v * w for v, w in zip(values, weights)) / len(weights)
