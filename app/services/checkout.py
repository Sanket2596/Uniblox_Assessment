"""Checkout orchestration.

One cart becomes at most one paid order. The whole flow runs in a single DB transaction
so partial failures leave nothing behind (see docs/design-decisions.md):

    1. Idempotency key lookup   -> return the existing order on a retry
    2. Load cart + guard        -> must exist, be ACTIVE, and be non-empty
    3. Lock product rows        -> SELECT ... FOR UPDATE, ordered by id (no deadlock)
    4. (coupon step: no coupon system yet, discount is always 0)
    5. Re-validate inventory    -> under the lock, so no oversell
    6. Snapshot + total in cents
    7. Deduct inventory, create Order (PENDING) + OrderItems
    8. Charge the mock gateway  -> PAID commits; FAILED reverses inventory, keeps the
                                   order as an audit record, leaves the cart ACTIVE
    9. On PAID: clear the cart and flip it to CHECKED_OUT
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import CartEmpty, InsufficientInventory, PaymentFailed
from app.models import Cart, Order, OrderItem, Product
from app.models.cart import CartStatus
from app.models.order import OrderStatus
from app.money import to_cents
from app.services import payment
from app.services.cart import _require_active, get_cart


def _find_by_key(db: Session, idempotency_key: str) -> Order | None:
    return db.scalar(
        select(Order).where(Order.idempotency_key == idempotency_key)
    )


def checkout(db: Session, cart_id: int, idempotency_key: str) -> Order:
    # 1. Idempotency: a retry with the same key never places a second order.
    existing = _find_by_key(db, idempotency_key)
    if existing is not None:
        if existing.status is OrderStatus.PAID:
            return existing
        # Payment is deterministic, so a previously-failed charge fails again.
        raise PaymentFailed(existing.id, existing.total_amount)

    # 2. Load and guard the cart.
    cart = get_cart(db, cart_id)
    _require_active(cart)
    if not cart.items:
        raise CartEmpty(cart_id)

    # 3. Pessimistically lock every referenced product, in id order to avoid deadlocks.
    product_ids = sorted(item.product_id for item in cart.items)
    products = {
        product.id: product
        for product in db.scalars(
            select(Product)
            .where(Product.id.in_(product_ids))
            .order_by(Product.id)
            .with_for_update()
        )
    }

    # Re-check the cart is still ACTIVE now that we hold the locks. A concurrent checkout
    # of the same cart would have committed CHECKED_OUT before releasing these locks.
    db.refresh(cart)
    _require_active(cart)

    # 5. Re-validate inventory under the lock — the authoritative oversell check.
    for item in cart.items:
        product = products[item.product_id]
        if item.quantity > product.inventory:
            raise InsufficientInventory(product.id, item.quantity, product.inventory)

    # 6. Snapshot prices/names and total everything in integer cents.
    order_items: list[OrderItem] = []
    subtotal_cents = 0
    for item in cart.items:
        product = products[item.product_id]
        unit_cents = to_cents(product.price)
        subtotal_cents += unit_cents * item.quantity
        order_items.append(
            OrderItem(
                product_id=product.id,
                historical_name=product.name,
                historical_price=unit_cents,
                quantity=item.quantity,
            )
        )

    discount_cents = 0  # no coupon system yet; seam for a future rewards step
    total_cents = subtotal_cents - discount_cents

    order = Order(
        user_id=cart.user_id,
        idempotency_key=idempotency_key,
        total_amount=total_cents,
        discount_applied=discount_cents,
        status=OrderStatus.PENDING,
        items=order_items,
    )
    db.add(order)

    # 7. Deduct inventory (reversed below if payment declines).
    for item in cart.items:
        products[item.product_id].inventory -= item.quantity

    db.flush()  # assign order.id within the transaction

    # 8. Charge. The gateway is called before commit so a decline can be undone.
    outcome = payment.charge(total_cents, idempotency_key=idempotency_key)

    if outcome is not OrderStatus.PAID:
        # Reverse the deduction, keep the FAILED order for audit, leave the cart ACTIVE.
        for item in cart.items:
            products[item.product_id].inventory += item.quantity
        order.status = OrderStatus.FAILED
        db.commit()
        db.refresh(order)
        raise PaymentFailed(order.id, total_cents)

    # 9. Success: freeze the cart. Clearing items keeps the checked-out cart tidy while
    # the order holds the durable record of what was bought.
    order.status = OrderStatus.PAID
    for item in list(cart.items):
        db.delete(item)
    cart.status = CartStatus.CHECKED_OUT

    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, order_id: int) -> Order | None:
    return db.get(Order, order_id)
