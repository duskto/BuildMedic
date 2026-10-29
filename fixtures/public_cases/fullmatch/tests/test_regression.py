from demo.subject import valid_identifier


def test_basic_identifiers():
    assert valid_identifier('_ok2') is True
    assert valid_identifier('2bad') is False
    assert valid_identifier('') is False
