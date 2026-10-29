import math


def reduce_fraction(numerator, denominator):
    if denominator == 0:
        raise ValueError("zero denominator")
    divisor = math.gcd(numerator, denominator)
    return numerator // divisor, denominator // divisor
