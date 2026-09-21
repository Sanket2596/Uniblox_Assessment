import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OrderStatus(str, enum.Enum):
    """Payment state machine for an order.

    PENDING -> PAID on a successful charge, PENDING -> FAILED on a declined one. There
    is no path out of a terminal state; a new checkout produces a new order.
    """

    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("total_amount >= 0", name="ck_orders_total_non_negative"),
        CheckConstraint("discount_applied >= 0", name="ck_orders_discount_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Matches Cart.user_id: the caller-supplied id of an assumed external user system.
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Money is stored in integer cents (see app.money / docs/design-decisions.md).
    total_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_applied: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Client-generated idempotency key (typically a UUIDv4). The unique constraint is
    # what makes a retried checkout return the existing order instead of a duplicate.
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )

    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", native_enum=False, length=16),
        nullable=False,
        default=OrderStatus.PENDING,
        server_default=OrderStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItem.id",
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint(
            "historical_price >= 0", name="ck_order_items_price_non_negative"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # RESTRICT mirrors cart_items: a product referenced by history cannot be deleted.
    # The snapshot columns below make the line readable without touching products at all.
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Snapshots frozen at purchase time — immune to later product edits or deletion.
    historical_name: Mapped[str] = mapped_column(String(200), nullable=False)
    historical_price: Mapped[int] = mapped_column(Integer, nullable=False)  # cents
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped["Order"] = relationship(back_populates="items")

    @property
    def line_total(self) -> int:
        """Line total in cents, from the frozen snapshot (never live product price)."""
        return self.historical_price * self.quantity
