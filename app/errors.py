from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(AppError):
    status_code = 404
    code = "NOT_FOUND"


class Conflict(AppError):
    status_code = 409
    code = "CONFLICT"


class ProductNotFound(NotFound):
    code = "PRODUCT_NOT_FOUND"

    def __init__(self, product_id: int):
        super().__init__(f"Product {product_id} does not exist", product_id=product_id)


class CartNotFound(NotFound):
    code = "CART_NOT_FOUND"

    def __init__(self, cart_id: int):
        super().__init__(f"Cart {cart_id} does not exist", cart_id=cart_id)


class OrderNotFound(NotFound):
    code = "ORDER_NOT_FOUND"

    def __init__(self, order_id: int):
        super().__init__(f"Order {order_id} does not exist", order_id=order_id)


class CartItemNotFound(NotFound):
    code = "CART_ITEM_NOT_FOUND"

    def __init__(self, cart_id: int, product_id: int):
        super().__init__(
            f"Cart {cart_id} has no line for product {product_id}",
            cart_id=cart_id,
            product_id=product_id,
        )


class CartNotActive(Conflict):
    """Raised on any attempt to mutate a cart that is no longer ACTIVE."""

    code = "CART_NOT_ACTIVE"

    def __init__(self, cart_id: int, status: str):
        super().__init__(
            f"Cart {cart_id} is {status} and can no longer be modified",
            cart_id=cart_id,
            status=status,
        )


class InsufficientInventory(AppError):
    status_code = 409
    code = "INSUFFICIENT_INVENTORY"

    def __init__(self, product_id: int, requested: int, available: int):
        super().__init__(
            f"Product {product_id} has only {available} in stock; {requested} requested",
            product_id=product_id,
            requested=requested,
            available=available,
        )


class CartEmpty(Conflict):
    """Raised on an attempt to check out a cart with no items."""

    code = "CART_EMPTY"

    def __init__(self, cart_id: int):
        super().__init__(
            f"Cart {cart_id} is empty and cannot be checked out", cart_id=cart_id
        )


class PaymentFailed(AppError):
    """Raised when the payment gateway declines the charge. The order is persisted with
    status FAILED for audit; inventory is left untouched."""

    status_code = 402
    code = "PAYMENT_FAILED"

    def __init__(self, order_id: int, total_amount: int):
        super().__init__(
            f"Payment for order {order_id} was declined",
            order_id=order_id,
            total_amount=total_amount,
        )


def _payload(code: str, message: str, details: dict | None = None) -> dict:
    body: dict[str, object] = {"code": code, "message": message}
    if details:
        body["details"] = details
    return {"error": body}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(_payload(exc.code, exc.message, exc.details), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(
            _payload("VALIDATION_ERROR", "Request failed validation", {"errors": exc.errors()}),
            status_code=422,
        )
