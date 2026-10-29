from demo.subject import merge_config


def test_preserve_inputs():
    defaults = {'a': 1}
    assert merge_config(defaults, {'b': 2}) == {'a': 1, 'b': 2}
    assert defaults == {'a': 1}
