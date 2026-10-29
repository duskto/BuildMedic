from demo.subject import sorted_versions


def test_contract():
    assert sorted_versions(['1.10', '1.2', '2.0']) == ['1.2', '1.10', '2.0']
