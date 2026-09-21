from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.cart import (
    CartCreate,
    CartItemCreate,
    CartItemQuantityUpdate,
    CartRead,
)
from app.schemas.order import CheckoutRequest, OrderRead
from app.services import cart as cart_service
from app.services import checkout as checkout_service

router = APIRouter(prefix="/carts", tags=["carts"])


@router.post("", response_model=CartRead, status_code=status.HTTP_201_CREATED)
def create_cart(payload: CartCreate, db: Session = Depends(get_db)):
    return cart_service.create_cart(db, payload.user_id)


@router.get("/{cart_id}", response_model=CartRead)
def get_cart(cart_id: int, db: Session = Depends(get_db)):
    return cart_service.get_cart(db, cart_id)


@router.post(
    "/{cart_id}/items",
    response_model=CartRead,
    status_code=status.HTTP_201_CREATED,
)
def add_item(cart_id: int, payload: CartItemCreate, db: Session = Depends(get_db)):
    return cart_service.add_item(db, cart_id, payload.product_id, payload.quantity)


@router.patch("/{cart_id}/items/{product_id}", response_model=CartRead)
def set_item_quantity(
    cart_id: int,
    product_id: int,
    payload: CartItemQuantityUpdate,
    db: Session = Depends(get_db),
):
    return cart_service.set_item_quantity(db, cart_id, product_id, payload.quantity)


@router.delete("/{cart_id}/items/{product_id}", response_model=CartRead)
def remove_item(cart_id: int, product_id: int, db: Session = Depends(get_db)):
    return cart_service.remove_item(db, cart_id, product_id)


@router.post(
    "/{cart_id}/checkout",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
)
def checkout(cart_id: int, payload: CheckoutRequest, db: Session = Depends(get_db)):
    return checkout_service.checkout(db, cart_id, payload.idempotency_key)
