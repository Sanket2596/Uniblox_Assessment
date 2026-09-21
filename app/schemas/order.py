from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.order import OrderStatus


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
