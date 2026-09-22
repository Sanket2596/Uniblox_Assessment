import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import (
    IntegrityError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError as PoolTimeoutError,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")


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


# Stable codes for the framework-raised HTTPExceptions we do not model ourselves, so an
# unknown route or a wrong method still returns the same {"error": {...}} envelope.
_HTTP_CODES = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    406: "NOT_ACCEPTABLE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    429: "RATE_LIMITED",
}


def _request_id() -> str:
    """Short correlation id echoed to the client and written to the log for every 5xx, so
    an operator can find the failing request from what the caller reports."""
    return uuid.uuid4().hex[:12]


def register_error_handlers(app: FastAPI) -> None:
    # --- Expected, typed application errors (4xx/402) ------------------------------------
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(_payload(exc.code, exc.message, exc.details), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse(
            _payload("VALIDATION_ERROR", "Request failed validation", {"errors": exc.errors()}),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        # Wrap framework HTTPExceptions (unknown route, 405, negotiation) in our envelope.
        code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ").title()
        return JSONResponse(
            _payload(code, message), status_code=exc.status_code, headers=getattr(exc, "headers", None)
        )

    # --- Database faults: distinguish "your fault" from "retry later" from "a bug" -------
    @app.exception_handler(IntegrityError)
    async def _integrity_error(request: Request, exc: IntegrityError):
        # A constraint the service did not translate into a friendly error itself: still a
        # conflict with current state, not a server bug. The DB message is logged, never
        # returned, so schema details do not leak.
        rid = _request_id()
        logger.warning(
            "[%s] integrity error on %s %s: %s",
            rid, request.method, request.url.path, getattr(exc, "orig", exc),
        )
        return JSONResponse(
            _payload(
                "CONSTRAINT_VIOLATION",
                "The request conflicts with the current state of a resource.",
                {"request_id": rid},
            ),
            status_code=409,
            headers={"X-Request-ID": rid},
        )

    async def _db_unavailable(request: Request, exc: Exception):
        # Lost connection, lock timeout, or an exhausted connection pool (SQLAlchemy
        # TimeoutError). None of these are the client's fault, and all are transient, so we
        # answer 503 with Retry-After rather than a 500 that says "give up".
        rid = _request_id()
        logger.error(
            "[%s] database unavailable on %s %s: %s",
            rid, request.method, request.url.path, getattr(exc, "orig", exc),
        )
        return JSONResponse(
            _payload(
                "SERVICE_UNAVAILABLE",
                "The service is temporarily unavailable; please retry.",
                {"request_id": rid},
            ),
            status_code=503,
            headers={"Retry-After": "1", "X-Request-ID": rid},
        )

    app.add_exception_handler(OperationalError, _db_unavailable)
    app.add_exception_handler(PoolTimeoutError, _db_unavailable)

    # --- Everything else is a bug: uniform 500, logged with a stack trace, no leak -------
    def _internal(request: Request, exc: Exception, kind: str):
        rid = _request_id()
        logger.error("[%s] %s on %s %s", rid, kind, request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            _payload(
                "INTERNAL_ERROR",
                "An unexpected error occurred. Contact support with the request id.",
                {"request_id": rid},
            ),
            status_code=500,
            headers={"X-Request-ID": rid},
        )

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(request: Request, exc: SQLAlchemyError):
        return _internal(request, exc, "database error")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        return _internal(request, exc, "unhandled exception")
