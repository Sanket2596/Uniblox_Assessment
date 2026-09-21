"""Cart mutation workflow.

All cart writes flow through here so the invariants live in one place:

- a cart can only be mutated while its status is ACTIVE (CartNotActive otherwise);
- a line's quantity never exceeds the product's live inventory (InsufficientInventory);
- one line per product — adding an existing product merges into the current line,
  matching the uq_cart_items_cart_product unique constraint.

Pricing is intentionally *not* snapshotted here; the cart reflects live product prices
until checkout (see docs/design-decisions.md).
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import (
    CartItemNotFound,
    CartNotActive,
    CartNotFound,
    InsufficientInventory,
    ProductNotFound,
)
from app.models import Cart, CartItem, Product
from app.models.cart import CartStatus


def create_cart(db: Session, user_id: str) -> Cart:
    cart = Cart(user_id=user_id)
    db.add(cart)
    db.commit()
    db.refresh(cart)
    return cart


def get_cart(db: Session, cart_id: int) -> Cart:
    cart = db.get(Cart, cart_id)
    if cart is None:
        raise CartNotFound(cart_id)
    return cart


def _require_active(cart: Cart) -> None:
    if cart.status is not CartStatus.ACTIVE:
        raise CartNotActive(cart.id, cart.status.value)


def _require_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise ProductNotFound(product_id)
    return product


def _find_line(db: Session, cart_id: int, product_id: int) -> CartItem | None:
    return db.scalar(
        select(CartItem).where(
            CartItem.cart_id == cart_id, CartItem.product_id == product_id
        )
    )


def add_item(db: Session, cart_id: int, product_id: int, quantity: int) -> Cart:
    """Add `quantity` of a product, merging into an existing line if one exists. The
    resulting line quantity is validated against live inventory."""
    cart = get_cart(db, cart_id)
    _require_active(cart)
    product = _require_product(db, product_id)

    line = _find_line(db, cart_id, product_id)
    new_quantity = (line.quantity if line else 0) + quantity
    if new_quantity > product.inventory:
        raise InsufficientInventory(product_id, new_quantity, product.inventory)

    if line is None:
        db.add(CartItem(cart_id=cart_id, product_id=product_id, quantity=quantity))
    else:
        line.quantity = new_quantity

    db.commit()
    db.refresh(cart)
    return cart


def set_item_quantity(
    db: Session, cart_id: int, product_id: int, quantity: int
) -> Cart:
    """Set a line's absolute quantity (must already exist and be positive)."""
    cart = get_cart(db, cart_id)
    _require_active(cart)

    line = _find_line(db, cart_id, product_id)
    if line is None:
        raise CartItemNotFound(cart_id, product_id)

    if quantity > line.product.inventory:
        raise InsufficientInventory(product_id, quantity, line.product.inventory)

    line.quantity = quantity
    db.commit()
    db.refresh(cart)
    return cart


def remove_item(db: Session, cart_id: int, product_id: int) -> Cart:
    cart = get_cart(db, cart_id)
    _require_active(cart)

    line = _find_line(db, cart_id, product_id)
    if line is None:
        raise CartItemNotFound(cart_id, product_id)

    db.delete(line)
    db.commit()
    db.refresh(cart)
    return cart
