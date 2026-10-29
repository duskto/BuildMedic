from demo.subject import merge_config


def test_contract():
    assert merge_config({'port': 80}, {'port': 443}) == {'port': 443}
