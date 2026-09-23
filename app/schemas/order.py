from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.order import Order, OrderStatus
from app.money import format_cents


class CheckoutRequest(BaseModel):
    """Payload for POST /carts/{cart_id}/checkout.

    `idempotency_key` is client-generated (typically a UUIDv4). Sending the same key on a
    retry returns the original order instead of placing a second one.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=255)


class OrderItemRead(BaseModel):
    """A purchased line. All money is in integer cents, frozen at purchase time."""

    model_config = ConfigDict(from_attributes=True)

    product_id: int
    historical_name: str
    historical_price: int  # cents, per unit, at purchase time
    quantity: int
    line_total: int  # cents


class OrderRead(BaseModel):
    """A placed order (receipt). All money is in integer cents (see app.money)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    status: OrderStatus
    total_amount: int  # cents
    discount_applied: int  # cents
    idempotency_key: str
    created_at: datetime
    items: list[OrderItemRead]


class ReceiptLine(BaseModel):
    """One itemized line on the human-facing receipt, from the order's frozen snapshot."""

    product_id: int
    name: str  # historical_name — what it was called at purchase time
    quantity: int
    unit_price: int  # cents
    line_total: int  # cents
    unit_price_display: str  # e.g. "19.99"
    line_total_display: str


class Receipt(BaseModel):
    """A generated receipt: a human-facing projection of a placed `Order`.

    It is *derived*, never stored — the durable record is the order and its snapshot lines,
    so the same receipt can be regenerated at any time and never drifts when a product is
    later renamed, repriced, or deleted (see docs/design-decisions.md, §5). Money is given
    both in integer cents (machine) and as a two-place string (human).
    """

    message: str
    currency: str
    order_id: int
    status: OrderStatus
    user_id: str
    placed_at: datetime
    line_items: list[ReceiptLine]
    subtotal: int  # cents — sum of line totals
    discount: int  # cents
    total: int  # cents
    subtotal_display: str
    discount_display: str
    total_display: str

    @classmethod
    def from_order(cls, order: Order, *, currency: str = "USD") -> "Receipt":
        lines = [
            ReceiptLine(
                product_id=item.product_id,
                name=item.historical_name,
                quantity=item.quantity,
                unit_price=item.historical_price,
                line_total=item.line_total,
                unit_price_display=format_cents(item.historical_price),
                line_total_display=format_cents(item.line_total),
            )
            for item in order.items
        ]
        subtotal = sum(item.line_total for item in order.items)
        if order.status is OrderStatus.PAID:
            message = f"Payment successful — order #{order.id} confirmed."
        else:
            message = f"Payment failed — order #{order.id} was not charged."
        return cls(
            message=message,
            currency=currency,
            order_id=order.id,
            status=order.status,
            user_id=order.user_id,
            placed_at=order.created_at,
            line_items=lines,
            subtotal=subtotal,
            discount=order.discount_applied,
            total=order.total_amount,
            subtotal_display=format_cents(subtotal),
            discount_display=format_cents(order.discount_applied),
            total_display=format_cents(order.total_amount),
        )
