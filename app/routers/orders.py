from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import OrderNotFound
from app.schemas.order import OrderRead
from app.services import checkout as checkout_service

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/{order_id}", response_model=OrderRead)
def get_order(order_id: int, db: Session = Depends(get_db)):
    order = checkout_service.get_order(db, order_id)
    if order is None:
        raise OrderNotFound(order_id)
    return order
