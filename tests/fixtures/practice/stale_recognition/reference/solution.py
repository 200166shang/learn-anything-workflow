def keep(result: dict, now: float, max_age: float) -> bool:
    """Keep results at the exact boundary; reject only strictly older ones."""
    return now - result["observed_at"] <= max_age
