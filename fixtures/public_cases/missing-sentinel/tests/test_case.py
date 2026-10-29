from demo.subject import value_or_default


def test_contract():
    for value in [0, False, '', None]:
        assert value_or_default({'key': value}, 'key', 'fallback') == value
