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


def _once_none_find(monkeypatch):
    """Make checkout's up-front idempotency check see nothing, so control reaches the
    race-recovery branch, while every later lookup behaves normally. This reproduces a
    same-key competitor that commits *after* our step-1 read but before our own write."""
    from app.services import checkout as co

    real_find = co._find_by_key
    seen = {"first": True}

    def fake_find(db, key):
        if seen["first"]:
            seen["first"] = False
            return None
        return real_find(db, key)

    monkeypatch.setattr(co, "_find_by_key", fake_find)


def test_same_key_that_loses_the_cart_claim_returns_the_winners_order(
    client, products, monkeypatch
):
    """A same-key request that loses the cart claim must return the winner's order
    (idempotent 201), not a spurious 409 CART_NOT_ACTIVE."""
    from decimal import Decimal

    from app.db import SessionLocal
    from app.models import Cart, Order, OrderItem, Product
    from app.models.cart import CartStatus
    from app.models.order import OrderStatus
    from app.services import checkout as co

    cart = _cart(client, "race-claim")
    p = next(x for x in products if x["inventory"] >= 2)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    key = _key()
    price = to_cents(Decimal(p["price"]))

    _once_none_find(monkeypatch)
    real_claim = co._claim_cart

    def fake_claim(db, cart_obj):
        # The winner commits its PAID order and claims the cart from an independent
        # session, exactly as a concurrent request would. Our claim then finds 0 rows.
        with SessionLocal() as other:
            other.get(Product, p["id"]).inventory -= 1
            other.add(
                Order(
                    user_id=cart_obj.user_id,
                    idempotency_key=key,
                    total_amount=price,
                    discount_applied=0,
                    status=OrderStatus.PAID,
                    items=[
                        OrderItem(
                            product_id=p["id"],
                            historical_name=p["name"],
                            historical_price=price,
                            quantity=1,
                        )
                    ],
                )
            )
            other.get(Cart, cart_obj.id).status = CartStatus.CHECKED_OUT
            other.commit()
        return real_claim(db, cart_obj)

    monkeypatch.setattr(co, "_claim_cart", fake_claim)

    with SessionLocal() as db:
        order = co.checkout(db, cart["id"], key)
        assert order.status is OrderStatus.PAID
        assert order.idempotency_key == key
        # Exactly one order exists under the key — no duplicate was placed.
        assert db.query(Order).filter(Order.idempotency_key == key).count() == 1


def test_same_key_that_loses_the_unique_insert_returns_the_winners_order(
    client, products, monkeypatch
):
    """Backstop: when the cart claim cannot serialize the two requests (a key reused
    across carts), the UNIQUE(idempotency_key) insert rejects the loser, and it must
    still resolve to the winner's order instead of raising an unhandled IntegrityError."""
    from decimal import Decimal

    from app.db import SessionLocal
    from app.models import Order, OrderItem
    from app.models.order import OrderStatus
    from app.services import checkout as co

    cart = _cart(client, "race-insert")
    p = next(x for x in products if x["inventory"] >= 1)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    key = _key()
    price = to_cents(Decimal(p["price"]))

    # A committed winner already holds this key (as if from a different cart).
    with SessionLocal() as other:
        other.add(
            Order(
                user_id="winner",
                idempotency_key=key,
                total_amount=price,
                discount_applied=0,
                status=OrderStatus.PAID,
                items=[
                    OrderItem(
                        product_id=p["id"],
                        historical_name=p["name"],
                        historical_price=price,
                        quantity=1,
                    )
                ],
            )
        )
        other.commit()

    _once_none_find(monkeypatch)  # slip past step 1; the insert then trips UNIQUE

    with SessionLocal() as db:
        order = co.checkout(db, cart["id"], key)
        assert order.status is OrderStatus.PAID
        assert order.user_id == "winner"
        assert db.query(Order).filter(Order.idempotency_key == key).count() == 1


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


def test_concurrent_checkouts_never_oversell(client, products, monkeypatch):
    """N carts each hold the *entire* remaining stock of one product and check out at the
    same moment. Exactly one may win; the rest must get a clean 409, and stock must land
    at exactly zero (never negative, never silently over-decremented).

    The gateway is slowed to ~100ms so the requests genuinely overlap inside the write
    phase; with an instant stub the race window is too small to exercise."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    from app.services import payment

    real_charge = payment.charge

    def slow_charge(amount_cents, *, idempotency_key):
        time.sleep(0.1)
        return real_charge(amount_cents, idempotency_key=idempotency_key)

    monkeypatch.setattr(payment, "charge", slow_charge)

    p = max(products, key=lambda x: x["inventory"])
    stock = client.get(f"/products/{p['id']}").json()["inventory"]
    assert stock >= 2

    carts = [_cart(client, f"racer-{i}") for i in range(4)]
    for cart in carts:
        r = client.post(
            f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": stock}
        )
        assert r.status_code == 201

    def go(cart):
        return client.post(f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()})

    with ThreadPoolExecutor(max_workers=len(carts)) as pool:
        results = list(pool.map(go, carts))

    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409, 409, 409], [r.json() for r in results]
    for r in results:
        if r.status_code == 409:
            assert r.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"
    assert client.get(f"/products/{p['id']}").json()["inventory"] == 0
