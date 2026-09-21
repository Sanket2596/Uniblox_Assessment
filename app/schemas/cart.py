from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.cart import CartStatus


class CartCreate(BaseModel):
    """Payload for creating a cart. `user_id` identifies the caller in an assumed
    external user system; there is no users table (see app.models.cart.Cart)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    user_id: str = Field(min_length=1, max_length=64)


class CartItemCreate(BaseModel):
    """Payload for adding a product to a cart.

    Structural guards only: a non-existent product and insufficient inventory are
    DB-state checks the service layer enforces (ProductNotFound / InsufficientInventory).
    """

    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0)


class CartItemQuantityUpdate(BaseModel):
    """Payload for setting a line's absolute quantity. `quantity` must be positive; to
    drop a line to zero, delete it (DELETE .../items/{product_id})."""

    quantity: int = Field(gt=0)


class CartItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    product_name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal
    available_inventory: int


class CartRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    status: CartStatus
    created_at: datetime
    items: list[CartItemRead]
    subtotal: Decimal
    total_quantity: int
    has_insufficient_stock: bool
