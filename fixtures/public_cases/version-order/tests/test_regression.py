from demo.subject import sorted_versions


def test_empty_and_simple():
    assert sorted_versions([]) == []
    assert sorted_versions(['2.0', '1.0']) == ['1.0', '2.0']
