"""Checkout orchestration.

One cart becomes at most one paid order. The whole flow runs in a single DB transaction
so partial failures leave nothing behind (see docs/design-decisions.md):

    1. Idempotency key lookup   -> return the existing order on a retry
    2. Load cart + guard        -> must exist, be ACTIVE, and be non-empty
    3. Claim the cart           -> UPDATE ... WHERE status = ACTIVE (compare-and-set);
                                   0 rows means someone else is checking it out
    4. Snapshot + total in cents
    5. (coupon step: no coupon system yet, discount is always 0)
    6. Deduct inventory         -> UPDATE ... SET inventory = inventory - q
                                   WHERE inventory >= q, per product, in id order;
                                   0 rows means oversell -> roll back, 409
    7. Create Order (PENDING) + OrderItems
    8. Charge the mock gateway  -> PAID commits; FAILED reverses inventory, keeps the
                                   order as an audit record, releases the cart to ACTIVE
    9. On PAID: clear the cart items; the cart stays CHECKED_OUT

Concurrency is optimistic: no row is locked ahead of time. Every write that could race
carries its own precondition in the WHERE clause, so the database — not application
state read a moment earlier — decides who wins. This is what keeps the invariant on
SQLite (where SELECT ... FOR UPDATE is a no-op) as well as on Postgres.

Idempotency holds even when two requests carry the *same* key and race each other. The
key is checked up front for the common retry-after-completion case, and then again at the
two points where a race actually resolves — when a cart claim is lost, and when the
UNIQUE(idempotency_key) insert is rejected — so the losing request returns the winner's
order (or its decline) instead of a spurious 409 or an unhandled 500.
"""

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import CartEmpty, CartNotActive, InsufficientInventory, PaymentFailed
from app.models import Cart, Order, OrderItem, Product
from app.models.cart import CartStatus
from app.models.order import OrderStatus
from app.money import to_cents
from app.services import payment
from app.services.cart import _require_active, get_cart

# Core UPDATEs bypass the ORM's identity map on purpose; the objects we hold are
# re-read where their values matter (error details, the returned order).
_NO_SYNC = {"synchronize_session": False}


def _find_by_key(db: Session, idempotency_key: str) -> Order | None:
    return db.scalar(
        select(Order).where(Order.idempotency_key == idempotency_key)
    )


def _resolve_existing(existing: Order) -> Order:
    """Idempotent short-circuit for an order that already exists under this key.

    Only PAID and FAILED are ever committed (PENDING lives solely inside an in-flight
    transaction), so a PAID order is returned as-is and anything else re-raises the same
    deterministic decline rather than charging a second time.
    """
    if existing.status is OrderStatus.PAID:
        return existing
    raise PaymentFailed(existing.id, existing.total_amount)


def _claim_cart(db: Session, cart: Cart) -> bool:
    """Atomically flip ACTIVE -> CHECKED_OUT. Exactly one concurrent caller can win.

    Returns True on the winning claim. On a lost claim it rolls back, refreshes the ORM
    copy so `cart.status` reflects reality, and returns False — the caller decides whether
    that is an idempotent same-key retry (return the winner's order) or a genuine
    409 CART_NOT_ACTIVE.
    """
    claimed = db.execute(
        update(Cart)
        .where(Cart.id == cart.id, Cart.status == CartStatus.ACTIVE)
        .values(status=CartStatus.CHECKED_OUT),
        execution_options=_NO_SYNC,
    ).rowcount
    if claimed != 1:
        db.rollback()
        db.refresh(cart)
        return False
    db.expire(cart, ["status"])  # the ORM copy is stale; reload on next access
    return True


def _release_cart(db: Session, cart: Cart) -> None:
    """Undo a claim after a declined payment so the customer can retry. This is the only
    CHECKED_OUT -> ACTIVE transition, and it happens inside the same transaction that
    made the claim, so no committed cart ever moves backwards."""
    db.execute(
        update(Cart).where(Cart.id == cart.id).values(status=CartStatus.ACTIVE),
        execution_options=_NO_SYNC,
    )
    db.expire(cart, ["status"])


def _deduct_inventory(db: Session, cart: Cart) -> None:
    """Conditional decrement per line, in product-id order. The `inventory >= q`
    predicate is the authoritative oversell check; a 0-row update means the stock was
    taken by someone else between our read and our write."""
    for item in sorted(cart.items, key=lambda i: i.product_id):
        updated = db.execute(
            update(Product)
            .where(Product.id == item.product_id, Product.inventory >= item.quantity)
            .values(inventory=Product.inventory - item.quantity),
            execution_options=_NO_SYNC,
        ).rowcount
        if updated != 1:
            db.rollback()  # undo the cart claim and any earlier decrements
            available = db.scalar(
                select(Product.inventory).where(Product.id == item.product_id)
            )
            raise InsufficientInventory(item.product_id, item.quantity, available or 0)


def _restore_inventory(db: Session, cart: Cart) -> None:
    for item in cart.items:
        db.execute(
            update(Product)
            .where(Product.id == item.product_id)
            .values(inventory=Product.inventory + item.quantity),
            execution_options=_NO_SYNC,
        )


def checkout(db: Session, cart_id: int, idempotency_key: str) -> Order:
    # 1. Idempotency: a retry with the same key never places a second order.
    existing = _find_by_key(db, idempotency_key)
    if existing is not None:
        return _resolve_existing(existing)

    # 2. Load and guard the cart. `_require_active` gives a fast, friendly 409; the
    #    compare-and-set in step 3 is the one that is actually race-proof.
    cart = get_cart(db, cart_id)
    _require_active(cart)
    if not cart.items:
        raise CartEmpty(cart_id)

    # 3. Claim the cart. From here on we are inside the write transaction.
    if not _claim_cart(db, cart):
        # Lost the claim. If a same-key request beat us here, it has committed its order
        # under this key by the time our conditional UPDATE saw 0 rows (the claim
        # serializes both requests), so return that order rather than a spurious 409.
        winner = _find_by_key(db, idempotency_key)
        if winner is not None:
            return _resolve_existing(winner)
        raise CartNotActive(cart.id, cart.status.value)

    # 4. Snapshot prices/names and total everything in integer cents.
    products = {
        p.id: p
        for p in db.scalars(
            select(Product).where(Product.id.in_([i.product_id for i in cart.items]))
        )
    }
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

    # 5. Coupon seam — no coupon system yet.
    discount_cents = 0
    total_cents = subtotal_cents - discount_cents

    # 6. Deduct inventory. Raises (and rolls back) on oversell.
    _deduct_inventory(db, cart)

    # 7. Stage the order.
    order = Order(
        user_id=cart.user_id,
        idempotency_key=idempotency_key,
        total_amount=total_cents,
        discount_applied=discount_cents,
        status=OrderStatus.PENDING,
        items=order_items,
    )
    db.add(order)

    # The UNIQUE(idempotency_key) insert is the last line of defence for a same-key race
    # the cart claim could not serialize (e.g. the key reused across two carts). Whoever
    # commits second is rejected here; we roll back and return the winner's order.
    try:
        db.flush()  # assign order.id within the transaction

        # 8. Charge. The gateway is called before commit so a decline can be undone.
        outcome = payment.charge(total_cents, idempotency_key=idempotency_key)

        if outcome is not OrderStatus.PAID:
            # Give the stock back, release the cart, keep the FAILED order for audit.
            _restore_inventory(db, cart)
            _release_cart(db, cart)
            order.status = OrderStatus.FAILED
            db.commit()
            db.refresh(order)
            raise PaymentFailed(order.id, total_cents)

        # 9. Success: the cart is already CHECKED_OUT; clearing its lines keeps it tidy
        #    while the order holds the durable record of what was bought.
        order.status = OrderStatus.PAID
        for item in list(cart.items):
            db.delete(item)

        db.commit()
        db.refresh(order)
        return order
    except IntegrityError:
        db.rollback()
        winner = _find_by_key(db, idempotency_key)
        if winner is not None:
            return _resolve_existing(winner)
        raise


def get_order(db: Session, order_id: int) -> Order | None:
    return db.get(Order, order_id)
