from demo.subject import weighted_mean


def test_contract():
    assert weighted_mean([10, 20], [1, 3]) == 17.5
