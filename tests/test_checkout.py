import uuid
from decimal import Decimal

import pytest

from app.money import to_cents


@pytest.fixture
def products(client):
    return client.get("/products").json()


def _cart(client, user_id="alice"):
    resp = client.post("/carts", json={"user_id": user_id})
    assert resp.status_code == 201
    return resp.json()


def _limited(products):
    return min((p for p in products if p["inventory"] > 0), key=lambda p: p["inventory"])


def _key():
    return str(uuid.uuid4())


def test_checkout_succeeds_and_freezes_the_order(client, products):
    cart = _cart(client)
    p = next(x for x in products if x["inventory"] >= 2)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 2})

    resp = client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()})
    assert resp.status_code == 201
    order = resp.json()

    assert order["status"] == "PAID"
    assert order["discount_applied"] == 0
    assert order["total_amount"] == to_cents(Decimal(p["price"])) * 2
    assert len(order["items"]) == 1
    line = order["items"][0]
    assert line["product_id"] == p["id"]
    assert line["historical_name"] == p["name"]
    assert line["historical_price"] == to_cents(Decimal(p["price"]))
    assert line["quantity"] == 2
    assert line["line_total"] == to_cents(Decimal(p["price"])) * 2


def test_checkout_marks_cart_checked_out_and_deducts_inventory(client, products):
    cart = _cart(client)
    p = next(x for x in products if x["inventory"] >= 3)
    before = client.get(f"/products/{p['id']}").json()["inventory"]
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 3})

    client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()})

    cart_after = client.get(f"/carts/{cart['id']}").json()
    assert cart_after["status"] == "CHECKED_OUT"
    assert cart_after["items"] == []
    after = client.get(f"/products/{p['id']}").json()["inventory"]
    assert after == before - 3


def test_checkout_is_rejected_on_an_already_checked_out_cart(client, products):
    cart = _cart(client)
    p = next(x for x in products if x["inventory"] >= 1)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()})

    # A different key on the same (now CHECKED_OUT) cart must be refused.
    resp = client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CART_NOT_ACTIVE"


def test_replaying_the_same_idempotency_key_returns_the_same_order(client, products):
    cart = _cart(client)
    p = next(x for x in products if x["inventory"] >= 1)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    key = _key()

    first = client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": key})
    second = client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": key})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_checkout_prevents_overselling_across_carts(client, products):
    p = _limited(products)  # seed guarantees inventory <= 2
    qty = p["inventory"]

    cart_a = _cart(client, "a")
    cart_b = _cart(client, "b")
    # Both carts can hold the full stock while the item is unreserved.
    client.post(f"/carts/{cart_a['id']}/items", json={"product_id": p["id"], "quantity": qty})
    client.post(f"/carts/{cart_b['id']}/items", json={"product_id": p["id"], "quantity": qty})

    ok = client.post(f"/carts/{cart_a['id']}/checkout", json={"idempotency_key": _key()})
    assert ok.status_code == 201

    # Stock is now exhausted; the second checkout must fail the re-check.
    fail = client.post(f"/carts/{cart_b['id']}/checkout", json={"idempotency_key": _key()})
    assert fail.status_code == 409
    assert fail.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"


def test_checkout_of_empty_cart_is_rejected(client):
    cart = _cart(client)
    resp = client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CART_EMPTY"


def test_checkout_missing_cart_returns_404(client):
    resp = client.post("/carts/999999/checkout", json={"idempotency_key": _key()})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CART_NOT_FOUND"


def test_checkout_requires_idempotency_key(client, products):
    cart = _cart(client)
    p = products[0]
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    assert client.post(f"/carts/{cart['id']}/checkout", json={}).status_code == 422
    assert (
        client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": ""}).status_code
        == 422
    )


def test_payment_decline_reverses_inventory_and_keeps_cart_active(client, products, monkeypatch):
    from app.models.order import OrderStatus
    from app.services import payment

    monkeypatch.setattr(payment, "charge", lambda *a, **k: OrderStatus.FAILED)

    cart = _cart(client)
    p = next(x for x in products if x["inventory"] >= 2)
    before = client.get(f"/products/{p['id']}").json()["inventory"]
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 2})

    resp = client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()})
    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "PAYMENT_FAILED"

    # Inventory untouched, cart still usable.
    assert client.get(f"/products/{p['id']}").json()["inventory"] == before
    cart_after = client.get(f"/carts/{cart['id']}").json()
    assert cart_after["status"] == "ACTIVE"
    assert cart_after["total_quantity"] == 2


def test_get_order_returns_the_receipt(client, products):
    cart = _cart(client)
    p = products[0]
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    order_id = client.post(
        f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()}
    ).json()["id"]

    resp = client.get(f"/orders/{order_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == order_id
    assert resp.json()["status"] == "PAID"


def test_get_missing_order_returns_404(client):
    resp = client.get("/orders/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ORDER_NOT_FOUND"
