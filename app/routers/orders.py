from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import OrderNotFound
from app.schemas.order import OrderRead, Receipt
from app.services import checkout as checkout_service

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/{order_id}", response_model=OrderRead)
def get_order(order_id: int, db: Session = Depends(get_db)):
    order = checkout_service.get_order(db, order_id)
    if order is None:
        raise OrderNotFound(order_id)
    return order


@router.get("/{order_id}/receipt", response_model=Receipt)
def get_receipt(order_id: int, db: Session = Depends(get_db)):
    """Generate the human-facing receipt for a placed order. Derived from the order's
    frozen snapshot, so it is reproducible forever and immune to later product changes."""
    order = checkout_service.get_order(db, order_id)
    if order is None:
        raise OrderNotFound(order_id)
    return Receipt.from_order(order)
