"""Receipt generation: a placed order projects to a human-facing receipt with an itemized
breakdown and totals in both cents and formatted strings. The receipt is *derived* from the
order's frozen snapshot, so it reproduces forever and never drifts when the underlying
product changes — the whole point of snapshotting the order (docs/design-decisions.md §5)."""

import uuid
from decimal import Decimal

from app.money import format_cents, to_cents


def _cart(client, user_id="rcpt"):
    return client.post("/carts", json={"user_id": user_id}).json()


def _key():
    return str(uuid.uuid4())


def test_receipt_itemizes_and_totals_a_paid_order(client):
    products = client.get("/products").json()
    p = next(x for x in products if x["inventory"] >= 2)
    cart = _cart(client)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 2})
    order = client.post(
        f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()}
    ).json()

    r = client.get(f"/orders/{order['id']}/receipt")
    assert r.status_code == 200
    body = r.json()

    unit = to_cents(Decimal(p["price"]))
    assert body["message"] == f"Payment successful — order #{order['id']} confirmed."
    assert body["status"] == "PAID"
    assert body["currency"] == "USD"

    assert len(body["line_items"]) == 1
    line = body["line_items"][0]
    assert line["name"] == p["name"]
    assert line["quantity"] == 2
    assert line["unit_price"] == unit
    assert line["line_total"] == unit * 2
    assert line["unit_price_display"] == format_cents(unit)
    assert line["line_total_display"] == format_cents(unit * 2)

    assert body["subtotal"] == unit * 2
    assert body["discount"] == 0
    assert body["total"] == unit * 2
    assert body["total_display"] == format_cents(unit * 2)
    # No coupon system yet: total equals subtotal.
    assert body["subtotal_display"] == body["total_display"]


def test_receipt_is_immune_to_later_product_changes(client):
    """Rename and reprice the product *after* purchase; the receipt must not move."""
    from app.db import SessionLocal
    from app.models import Product

    products = client.get("/products").json()
    p = next(x for x in products if x["inventory"] >= 1)
    cart = _cart(client)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    order = client.post(
        f"/carts/{cart['id']}/checkout", json={"idempotency_key": _key()}
    ).json()

    before = client.get(f"/orders/{order['id']}/receipt").json()

    with SessionLocal() as db:
        prod = db.get(Product, p["id"])
        original_name, original_price = prod.name, prod.price
        prod.name = "TOTALLY RENAMED WIDGET"
        prod.price = Decimal("999.99")
        db.commit()
    try:
        after = client.get(f"/orders/{order['id']}/receipt").json()
        # The snapshot fields are frozen; the receipt is byte-for-byte the same.
        assert after["line_items"][0]["name"] == p["name"] != "TOTALLY RENAMED WIDGET"
        assert after["line_items"][0]["unit_price"] == before["line_items"][0]["unit_price"]
        assert after["total"] == before["total"]
    finally:
        with SessionLocal() as db:  # restore shared state for other tests
            prod = db.get(Product, p["id"])
            prod.name, prod.price = original_name, original_price
            db.commit()


def test_receipt_for_missing_order_is_404(client):
    r = client.get("/orders/999999/receipt")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "ORDER_NOT_FOUND"
