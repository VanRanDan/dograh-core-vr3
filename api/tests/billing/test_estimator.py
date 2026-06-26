from api.services.billing.estimator import estimate_units


def test_default_floor():
    minutes, cents = estimate_units(None)
    assert minutes == 1.0          # reserve at least one minute
    assert cents == 25.0           # reserve at least 25 cents of AI cost
