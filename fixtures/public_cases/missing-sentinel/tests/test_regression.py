from demo.subject import value_or_default


def test_absent_and_present():
    assert value_or_default({}, 'key', 'fallback') == 'fallback'
    assert value_or_default({'key': 7}, 'key', 3) == 7
