from demo.subject import has_cycle


def test_acyclic_graphs():
    assert has_cycle({}) is False
    assert has_cycle({'a': ['c'], 'b': ['c'], 'c': []}) is False
