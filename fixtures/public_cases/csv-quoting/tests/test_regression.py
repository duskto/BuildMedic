from demo.subject import encode_row


def test_plain_row():
    assert encode_row(['a', 'b']) == 'a,b\n'
