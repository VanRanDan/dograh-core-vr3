from api.services.billing.meters import compute_meters

def test_ai_cost_excludes_telephony():
    cost_info = {
        "cost_breakdown": {"llm_cost": 0.10, "tts_cost": 0.05, "stt_cost": 0.05,
                            "telephony_call": 0.20, "total": 0.40},
        "call_duration_seconds": 90,
    }
    minutes, cents = compute_meters(cost_info)
    assert minutes == 1.5                 # 90/60, fractional, no ceil
    assert round(cents, 6) == 20.0        # (0.10+0.05+0.05)*100, telephony excluded

def test_zero_when_missing():
    minutes, cents = compute_meters({})
    assert minutes == 0.0 and cents == 0.0
