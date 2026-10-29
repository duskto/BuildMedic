import csv
import io
from demo.subject import encode_row


def test_contract():
    values = ['a,b', 'a"b', 'plain']
    assert next(csv.reader(io.StringIO(encode_row(values)))) == values
