from demo.subject import pair_records


def test_equal_lengths():
    assert pair_records(['a', 'b'], [1, 2]) == [('a', 1), ('b', 2)]
    assert pair_records([], []) == []
