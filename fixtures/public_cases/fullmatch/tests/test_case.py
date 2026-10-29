from demo.subject import valid_identifier


def test_contract():
    assert valid_identifier('abc!') is False
    assert valid_identifier('abc\n') is False
