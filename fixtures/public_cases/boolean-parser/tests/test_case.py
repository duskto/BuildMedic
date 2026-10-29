from demo.subject import parse_boolean


def test_contract():
    assert parse_boolean('false') is False
    assert parse_boolean(' FALSE ') is False
