from demo.subject import has_cycle


def test_contract():
    assert has_cycle({'a': ['b'], 'b': ['a']}) is True
    assert has_cycle({'a': ['a']}) is True
