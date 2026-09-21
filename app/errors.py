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


class ProductNotFound(NotFound):
    code = "PRODUCT_NOT_FOUND"

    def __init__(self, product_id: int):
        super().__init__(f"Product {product_id} does not exist", product_id=product_id)


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
