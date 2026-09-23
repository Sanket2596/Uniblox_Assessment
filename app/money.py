"""Money conversion helpers.

Products and carts speak live `Decimal` dollars (Numeric(12,2)). Orders, once placed,
store money as integer cents so the frozen receipt is free of any float/rounding drift
(see docs/design-decisions.md). `to_cents` is the single bridge between the two.
"""

from decimal import ROUND_HALF_UP, Decimal

_CENTS = Decimal("0.01")


def to_cents(amount: Decimal) -> int:
    """Convert a Decimal dollar amount to an integer number of cents.

    Product prices are already Numeric(12,2), so quantize is a safety net rather than a
    lossy step. Uses exact Decimal arithmetic — never float — so 19.99 -> 1999 exactly.
    """
    return int(amount.quantize(_CENTS, rounding=ROUND_HALF_UP) * 100)


def from_cents(cents: int) -> Decimal:
    """Inverse of `to_cents`: integer cents back to a two-place `Decimal` for display."""
    return (Decimal(cents) / 100).quantize(_CENTS)


def format_cents(cents: int) -> str:
    """Human-facing two-place string for a receipt, e.g. 1999 -> "19.99". Currency-neutral;
    the caller adds the symbol so money.py stays free of currency policy."""
    return f"{from_cents(cents):.2f}"
