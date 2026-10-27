from demo.utils import format_price


def invoice_total(items):
    return format_price(sum(items))
