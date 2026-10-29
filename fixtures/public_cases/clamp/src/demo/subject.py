def clamp(value, low, high):
    if low > high:
        raise ValueError("invalid interval")
    return min(low, max(high, value))
