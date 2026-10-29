from demo.subject import fits_utf8


def test_ascii_boundaries():
    assert fits_utf8('ab', 2) is True
    assert fits_utf8('ab', 1) is False
    assert fits_utf8('', 0) is True
