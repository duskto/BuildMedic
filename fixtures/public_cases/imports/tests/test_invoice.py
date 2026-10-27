from demo.invoice import invoice_total
from demo.formatting import format_price


def test_invoice_total():
    assert invoice_total([150, 250]) == "¥4.00"


def test_empty_invoice():
    assert invoice_total([]) == "¥0.00"


def test_formatting():
    assert format_price(199) == "¥1.99"
