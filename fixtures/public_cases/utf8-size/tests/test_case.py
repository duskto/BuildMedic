from demo.subject import fits_utf8


def test_contract():
    assert fits_utf8('你好', 4) is False
    assert fits_utf8('你好', 6) is True
