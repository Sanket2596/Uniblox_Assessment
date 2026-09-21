from decimal import Decimal

from app.seed import load_seed_products


def test_list_products_returns_seeded_catalog(client):
    resp = client.get("/products")
    assert resp.status_code == 200
    body = resp.json()
    seed = load_seed_products()
    assert len(body) == len(seed) >= 5
    assert {p["name"] for p in body} == {p.name for p in seed}
    assert any(p["inventory"] <= 2 for p in body), "seed must include a limited-inventory product"


def test_price_is_serialized_as_exact_decimal_string(client):
    resp = client.get("/products")
    for p in resp.json():
        assert isinstance(p["price"], str)
        assert Decimal(p["price"]) == Decimal(p["price"]).quantize(Decimal("0.01"))


def test_get_product_by_id(client):
    first = client.get("/products").json()[0]
    resp = client.get(f"/products/{first['id']}")
    assert resp.status_code == 200
    assert resp.json() == first


def test_get_missing_product_returns_structured_404(client):
    resp = client.get("/products/999999")
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {
            "code": "PRODUCT_NOT_FOUND",
            "message": "Product 999999 does not exist",
            "details": {"product_id": 999999},
        }
    }


def test_invalid_product_id_returns_validation_error(client):
    resp = client.get("/products/not-an-int")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_pagination(client):
    page1 = client.get("/products", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/products", params={"limit": 2, "offset": 2}).json()
    assert len(page1) == 2
    assert {p["id"] for p in page1}.isdisjoint({p["id"] for p in page2})

    assert client.get("/products", params={"limit": 0}).status_code == 422


def test_seed_is_idempotent(client):
    before = client.get("/products").json()

    from app.db import SessionLocal
    from app.seed import seed_products

    with SessionLocal() as db:
        assert seed_products(db) == 0

    assert client.get("/products").json() == before
