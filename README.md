# Checkout & Rewards Service

Backend for an ecommerce store: products, carts, checkout, orders, and milestone-based discount coupons.

**Stack:** Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2.0, SQLite.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
```

## Run

```bash
uvicorn app.main:app --reload
```

On startup the app creates the SQLite schema (`store.db`) and seeds the product catalog from
`data/mock_data.json`. Seeding is idempotent, so restarts do not duplicate products.

To seed without starting the server:

```bash
python -m app.seed
```

Interactive docs: http://127.0.0.1:8000/docs

## Test

```bash
python -m pytest
```

Tests run against a throwaway SQLite file, never `store.db`.

## Configuration

Environment variables (or a `.env` file), all prefixed `STORE_`:

| Variable             | Default                | Purpose                     |
|----------------------|------------------------|-----------------------------|
| `STORE_DATABASE_URL` | `sqlite:///./store.db` | SQLAlchemy database URL     |
| `STORE_SEED_FILE`    | `data/mock_data.json`  | Product seed file           |

## API

All errors share one shape:

```json
{ "error": { "code": "PRODUCT_NOT_FOUND", "message": "Product 42 does not exist", "details": { "product_id": 42 } } }
```

`code` is stable and meant for programmatic handling; `message` is for humans; `details` is optional.

### Products

The catalog is read-only through the API. Inventory is changed only by checkout.

| Method | Path              | Success | Errors                       |
|--------|-------------------|---------|------------------------------|
| GET    | `/products`       | 200     | 422 `VALIDATION_ERROR`       |
| GET    | `/products/{id}`  | 200     | 404 `PRODUCT_NOT_FOUND`, 422 |

`GET /products` accepts `limit` (1–200, default 50) and `offset` (>= 0).

Response item:

```json
{ "id": 5, "name": "Limited Edition Desk Mat", "price": "19.99", "inventory": 2 }
```

`price` is always a decimal string with exactly two places. Clients should parse it with a decimal
type, not a float.

### Meta

| Method | Path      | Success |
|--------|-----------|---------|
| GET    | `/health` | 200     |
