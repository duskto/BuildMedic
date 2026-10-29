import csv
import io


def encode_row(values):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\n")
    writer.writerow(values)
    return buffer.getvalue()
