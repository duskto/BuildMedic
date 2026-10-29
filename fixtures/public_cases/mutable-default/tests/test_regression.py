from demo.subject import append_value


def test_explicit_bucket():
    bucket = []
    assert append_value(3, bucket) is bucket
    assert bucket == [3]
