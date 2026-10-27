from decimal import ROUND_HALF_EVEN, Decimal


def to_cents(amount: str) -> int:
    """A decimal amount as integer cents, rounded half to even."""
    return int((Decimal(amount) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
