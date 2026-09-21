import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.product import Product


class CartStatus(str, enum.Enum):
    """Cart lifecycle. A cart is mutable only while ACTIVE; checkout flips it to
    CHECKED_OUT exactly once, after which it is immutable history."""

    ACTIVE = "ACTIVE"
    CHECKED_OUT = "CHECKED_OUT"


class Cart(Base):
    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # No users table yet; user_id is the caller-supplied identifier of an assumed
    # external user system. Indexed because carts are always looked up by user.
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Idempotency guard for checkout: the atomic transition ACTIVE -> CHECKED_OUT
    # can succeed only once (see docs/design-decisions.md).
    status: Mapped[CartStatus] = mapped_column(
        Enum(CartStatus, name="cart_status", native_enum=False, length=16),
        nullable=False,
        default=CartStatus.ACTIVE,
        server_default=CartStatus.ACTIVE.value,
    )

    items: Mapped[list["CartItem"]] = relationship(
        back_populates="cart",
        cascade="all, delete-orphan",
        order_by="CartItem.id",
    )

    @property
    def subtotal(self) -> Decimal:
        """Sum of line totals at current product prices."""
        return sum((item.line_total for item in self.items), Decimal("0"))

    @property
    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items)


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (
        # One row per product per cart; adding an existing product merges quantity
        # in the service layer rather than creating a duplicate line.
        UniqueConstraint("cart_id", "product_id", name="uq_cart_items_cart_product"),
        CheckConstraint("quantity > 0", name="ck_cart_items_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cart_id: Mapped[int] = mapped_column(
        ForeignKey("carts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    cart: Mapped["Cart"] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()

    @property
    def unit_price(self) -> Decimal:
        return self.product.price

    @property
    def product_name(self) -> str:
        return self.product.name

    @property
    def line_total(self) -> Decimal:
        return self.product.price * self.quantity
