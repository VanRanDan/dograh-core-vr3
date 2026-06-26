from decimal import Decimal


def compute_meters(cost_info: dict | None) -> tuple[float, float]:
    """Derive billing meters from a workflow-run cost_info.

    Returns (voice_minutes, ai_cost_cents). voice_minutes is FRACTIONAL
    (rounding happens in Lago at charge time). ai_cost_cents excludes
    telephony (carrier pass-through already covered by the minutes meter).
    """
    if not cost_info:
        return 0.0, 0.0

    breakdown = cost_info.get("cost_breakdown") or {}
    ai_usd = (
        Decimal(str(breakdown.get("llm_cost", 0)))
        + Decimal(str(breakdown.get("tts_cost", 0)))
        + Decimal(str(breakdown.get("stt_cost", 0)))
    )
    ai_cost_cents = float(ai_usd * Decimal("100"))

    duration = cost_info.get("call_duration_seconds", 0) or 0
    voice_minutes = float(Decimal(str(duration)) / Decimal("60"))

    return voice_minutes, ai_cost_cents
