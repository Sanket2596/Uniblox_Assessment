from decimal import Decimal

import pytest


@pytest.fixture
def products(client):
    return client.get("/products").json()


@pytest.fixture
def cart(client):
    resp = client.post("/carts", json={"user_id": "alice"})
    assert resp.status_code == 201
    return resp.json()


def _limited(products):
    """A product with small, non-zero inventory (seed guarantees one <= 2)."""
    return min((p for p in products if p["inventory"] > 0), key=lambda p: p["inventory"])


def test_create_cart_starts_active_and_empty(cart):
    assert cart["status"] == "ACTIVE"
    assert cart["items"] == []
    assert cart["subtotal"] == "0"
    assert cart["total_quantity"] == 0
    assert cart["has_insufficient_stock"] is False


def test_create_cart_requires_user_id(client):
    assert client.post("/carts", json={}).status_code == 422
    assert client.post("/carts", json={"user_id": ""}).status_code == 422


def test_add_item_computes_live_line_total(client, cart, products):
    p = _limited(products)
    resp = client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["items"]) == 1
    line = body["items"][0]
    assert line["product_id"] == p["id"]
    assert line["quantity"] == 1
    assert Decimal(line["line_total"]) == Decimal(p["price"])
    assert Decimal(body["subtotal"]) == Decimal(p["price"])
    assert body["total_quantity"] == 1


def test_adding_same_product_merges_into_one_line(client, cart, products):
    p = next(x for x in products if x["inventory"] >= 3)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    resp = client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 2})
    body = resp.json()
    lines = [i for i in body["items"] if i["product_id"] == p["id"]]
    assert len(lines) == 1
    assert lines[0]["quantity"] == 3


def test_add_beyond_inventory_is_rejected(client, cart, products):
    p = _limited(products)
    resp = client.post(
        f"/carts/{cart['id']}/items",
        json={"product_id": p["id"], "quantity": p["inventory"] + 1},
    )
    assert resp.status_code == 409
    body = resp.json()["error"]
    assert body["code"] == "INSUFFICIENT_INVENTORY"
    assert body["details"]["available"] == p["inventory"]


def test_merge_that_exceeds_inventory_is_rejected(client, cart, products):
    p = _limited(products)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": p["inventory"]})
    resp = client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    assert resp.status_code == 409


def test_add_nonexistent_product_returns_404(client, cart):
    resp = client.post(f"/carts/{cart['id']}/items", json={"product_id": 999999, "quantity": 1})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


def test_add_to_missing_cart_returns_404(client, products):
    resp = client.post("/carts/999999/items", json={"product_id": products[0]["id"], "quantity": 1})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CART_NOT_FOUND"


def test_add_rejects_non_positive_quantity(client, cart, products):
    resp = client.post(f"/carts/{cart['id']}/items", json={"product_id": products[0]["id"], "quantity": 0})
    assert resp.status_code == 422


def test_set_quantity_updates_line(client, cart, products):
    p = next(x for x in products if x["inventory"] >= 4)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    resp = client.patch(f"/carts/{cart['id']}/items/{p['id']}", json={"quantity": 4})
    assert resp.status_code == 200
    line = next(i for i in resp.json()["items"] if i["product_id"] == p["id"])
    assert line["quantity"] == 4


def test_set_quantity_beyond_inventory_is_rejected(client, cart, products):
    p = _limited(products)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    resp = client.patch(f"/carts/{cart['id']}/items/{p['id']}", json={"quantity": p["inventory"] + 1})
    assert resp.status_code == 409


def test_set_quantity_on_missing_line_returns_404(client, cart, products):
    resp = client.patch(f"/carts/{cart['id']}/items/{products[0]['id']}", json={"quantity": 1})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CART_ITEM_NOT_FOUND"


def test_set_quantity_to_zero_is_rejected(client, cart, products):
    p = products[0]
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    resp = client.patch(f"/carts/{cart['id']}/items/{p['id']}", json={"quantity": 0})
    assert resp.status_code == 422


def test_remove_item(client, cart, products):
    p = products[0]
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 1})
    resp = client.delete(f"/carts/{cart['id']}/items/{p['id']}")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_remove_missing_line_returns_404(client, cart, products):
    resp = client.delete(f"/carts/{cart['id']}/items/{products[0]['id']}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CART_ITEM_NOT_FOUND"


def test_view_cart_reflects_state(client, cart, products):
    p = next(x for x in products if x["inventory"] >= 2)
    client.post(f"/carts/{cart['id']}/items", json={"product_id": p["id"], "quantity": 2})
    resp = client.get(f"/carts/{cart['id']}")
    assert resp.status_code == 200
    body = resp.json()
    line = next(i for i in body["items"] if i["product_id"] == p["id"])
    assert line["available_inventory"] == p["inventory"]
    assert Decimal(body["subtotal"]) == Decimal(p["price"]) * 2


def test_view_missing_cart_returns_404(client):
    resp = client.get("/carts/999999")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CART_NOT_FOUND"
