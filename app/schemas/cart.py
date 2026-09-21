from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.cart import CartStatus


class CartItemCreate(BaseModel):
    """Payload for adding a product to a cart.

    Structural guards only: a non-existent product and insufficient inventory are
    DB-state checks the service layer enforces (ProductNotFound / InsufficientInventory).
    """

    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)


class CartItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    product_name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal


class CartRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    status: CartStatus
    created_at: datetime
    items: list[CartItemRead]
    subtotal: Decimal
    total_quantity: int
