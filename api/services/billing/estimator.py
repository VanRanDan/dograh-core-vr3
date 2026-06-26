RESERVE_FLOOR_MINUTES = 1.0
RESERVE_FLOOR_CENTS = 25.0


def estimate_units(workflow=None) -> tuple[float, float]:
    """Conservative call-start reservation. Fixed floor for now; can later be
    derived from a workflow's historical average call length."""
    return RESERVE_FLOOR_MINUTES, RESERVE_FLOOR_CENTS
