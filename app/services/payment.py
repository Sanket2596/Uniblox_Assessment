"""Mock payment gateway.

A deterministic stub that decouples checkout orchestration from the database. Checkout
calls `charge` after it has staged the order and inventory changes but before committing,
so a decline rolls the transaction's effects back cleanly (see app.services.checkout).

The stub approves every non-zero charge. Tests force the decline branch by monkeypatching
`charge`; there is intentionally no real network call.
"""

from app.models.order import OrderStatus


def charge(amount_cents: int, *, idempotency_key: str) -> OrderStatus:
    """Attempt to charge `amount_cents`. Returns OrderStatus.PAID or OrderStatus.FAILED.

    Deterministic: the same inputs always yield the same result, which is what makes an
    idempotent retry safe to short-circuit on the stored order's terminal status.
    """
    if amount_cents <= 0:
        return OrderStatus.FAILED
    return OrderStatus.PAID
