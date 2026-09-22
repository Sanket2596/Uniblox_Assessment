"""API error-handling strategy: every failure the client can hit comes back in the same
`{"error": {"code", "message", "details"?}}` envelope with a status code that says how to
react — 4xx for the caller's problem, 402 for payment, 503 (retryable) for a transient DB
fault, 500 for a bug — and no internal detail (SQL, stack traces) ever leaks."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, OperationalError, TimeoutError as PoolTimeoutError


@pytest.fixture
def error_client(client):
    # raise_server_exceptions=False lets us assert on the 500 response the handler builds
    # instead of the TestClient re-raising it. Reuses the app the session `client` set up.
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _boom(exc):
    def _raise(*_a, **_k):
        raise exc

    return _raise


def test_unknown_route_returns_the_envelope(error_client):
    r = error_client.get("/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_wrong_method_returns_the_envelope(error_client):
    r = error_client.delete("/health")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_unexpected_exception_is_a_uniform_500_without_leaking(error_client, monkeypatch):
    from app.services import checkout

    monkeypatch.setattr(checkout, "get_order", _boom(RuntimeError("secret internals: boom")))
    r = error_client.get("/orders/1")

    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["details"]["request_id"]
    assert r.headers.get("X-Request-ID") == body["error"]["details"]["request_id"]
    assert "boom" not in r.text  # the exception message never reaches the client


def test_transient_db_fault_is_retryable_503(error_client, monkeypatch):
    from app.services import checkout

    err = OperationalError("SELECT 1", {}, Exception("database is locked"))
    monkeypatch.setattr(checkout, "get_order", _boom(err))
    r = error_client.get("/orders/1")

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert r.headers.get("Retry-After")  # tells the client it is safe to try again


def test_connection_pool_exhaustion_is_retryable_503(error_client, monkeypatch):
    from app.services import checkout

    monkeypatch.setattr(
        checkout, "get_order", _boom(PoolTimeoutError("QueuePool limit reached"))
    )
    r = error_client.get("/orders/1")

    assert r.status_code == 503
    assert r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert r.headers.get("Retry-After")


def test_unmapped_constraint_violation_is_409_not_500(error_client, monkeypatch):
    from app.services import checkout

    err = IntegrityError("INSERT ...", {}, Exception("UNIQUE constraint failed"))
    monkeypatch.setattr(checkout, "get_order", _boom(err))
    r = error_client.get("/orders/1")

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CONSTRAINT_VIOLATION"
    assert "UNIQUE" not in r.text  # constraint / schema detail is logged, not returned
