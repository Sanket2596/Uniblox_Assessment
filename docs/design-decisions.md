# Design Decisions

## Cart lifecycle

A cart has a `status` (`app.models.cart.CartStatus`):

- **`ACTIVE`** — the user is viewing, adding, or editing items. The cart is mutable.
- **`CHECKED_OUT`** — checkout succeeded. The cart is immutable history; its line
  quantities and the prices they were charged at are frozen by the order snapshot.

New carts are created `ACTIVE` (column default). There is no path back from
`CHECKED_OUT` to `ACTIVE`.

## Price or availability changes after add-to-cart, before checkout

A cart holds live references to products, not price/stock snapshots. So between adding
an item and checking out, a product's `price` or `inventory` can change. The policy:

**A cart always reflects live prices and live availability. Nothing is reserved by being
in a cart.** Concretely:

- **Price changed** — the cart's `unit_price`, `line_total`, and `subtotal` are computed
  from the *current* `product.price` every time the cart is read
  (`CartItem.line_total`, `Cart.subtotal`). The user always sees today's price; there is
  no "price locked when added." The price the customer is actually charged is fixed only
  at checkout, when it is snapshotted into the order.

- **Stock dropped below the requested quantity** — this is *not* corrected at read time by
  the model layer; the cart row keeps the requested quantity. It is surfaced and handled
  at two points:
  - **View cart** (`GET /carts/{id}`): each line reports live `available_inventory`, and
    the cart reports `has_insufficient_stock` (true if any line's quantity exceeds live
    stock). A read is non-mutating — it does *not* silently adjust quantities on a `GET` —
    so the client can surface the shortfall and soft-disable checkout. Write endpoints
    (`add_item` / `set_item_quantity`) reject any change that would push a line past live
    inventory (`409 INSUFFICIENT_INVENTORY`).
  - **Checkout**: re-check inside the checkout transaction, under a pessimistic product
    lock, and abort if a line can no longer be fulfilled. The frontend view is never
    trusted (see the Checkout section below).

- **Product deleted** — disallowed while referenced by a cart item
  (`cart_items.product_id` FK is `ON DELETE RESTRICT`).

### Why not snapshot price into the cart?

Snapshotting price at add-time would show stale prices and complicate "the price went
down, honor the new one." Live pricing keeps a single source of truth (`products.price`)
and defers the one moment that legally matters — the price charged — to checkout, where it
is snapshotted into the order.

## Checkout

Checkout is `POST /carts/{cart_id}/checkout`. It turns one cart into at most one paid
`Order` and runs entirely inside a single DB transaction (`app.services.checkout`).

### Money is stored in integer cents

Products and carts speak live `Decimal` dollars (`Numeric(12,2)`). The moment money is
*frozen* — on an order — it is stored as **integer cents** (`Order.total_amount`,
`Order.discount_applied`, `OrderItem.historical_price`). Integer arithmetic has no
float/binary-rounding error, so a receipt total is exact and reproducible. `app.money.to_cents`
is the single conversion point. The order API therefore returns money in **cents** (the
Stripe convention); the cart API keeps returning `Decimal` dollars for live display.

### Snapshot pricing on the order

`OrderItem` copies `historical_name` and `historical_price` from the product at purchase
time rather than linking to live product fields. A later admin price change or product
edit must not rewrite past receipts or tax records. The cart stays live-priced (see above);
the order is the immutable record of what was actually charged.

### Idempotency — "a cart is checked out at most once"

Two independent guards:

1. **Client idempotency key.** The client sends a unique `idempotency_key` (a UUIDv4) in
   the request body. It is stored `UNIQUE` on `orders`. On a retry, checkout finds the
   existing order by key and returns it instead of placing a second one — safe against
   network timeouts and double-submits. A previously *failed* charge is deterministic, so
   the retry re-raises the same `402`.
2. **Cart status.** Even with a *new* key, a cart that is already `CHECKED_OUT` is refused
   (`409 CART_NOT_ACTIVE`) by the same `_require_active` guard the cart mutations use.

### Overselling & concurrency

Inside the transaction the referenced product rows are locked pessimistically
(`SELECT ... FOR UPDATE`, ordered by id to avoid deadlocks), the cart's ACTIVE status is
re-checked under the lock, and each line's quantity is re-validated against live inventory
before `product.inventory` is decremented. The `ck_products_inventory_non_negative`
constraint is the final backstop. On SQLite the row lock is a no-op, but WAL +
`busy_timeout` serialize writers, so the invariant still holds in tests; on Postgres the
lock is real.

### Payment stub & failure handling

`app.services.payment.charge` is a deterministic stub (a real gateway would sit here). It
is called *after* the order and inventory changes are staged but *before* commit, so the
checkout orchestration is cleanly decoupled from the DB mutations. The order moves through
a `PENDING -> PAID | FAILED` state machine:

- **PAID** — the cart is emptied and flipped to `CHECKED_OUT`; the transaction commits.
- **FAILED** — the staged inventory deduction is reversed, the cart is left `ACTIVE` (the
  customer can retry), and the order is committed with status `FAILED` as an audit record.
  The endpoint returns `402 PAYMENT_FAILED`.

### Coupons / discounts — deferred

The flow reserves a coupon step and `Order.discount_applied`, but there is no coupon table
yet, so `discount_applied` is always `0` and `total_amount == subtotal`. When a rewards
system is added, coupon validation slots in between inventory validation and total
calculation (validate + lock the coupon row, then subtract from the cents subtotal).
