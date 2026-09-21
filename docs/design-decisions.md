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
  - **Checkout** (order step, not yet built): re-check inside the checkout transaction and
    abort if a line can no longer be fulfilled. The frontend view is never trusted.

- **Product deleted** — disallowed while referenced by a cart item
  (`cart_items.product_id` FK is `ON DELETE RESTRICT`).

### Why not snapshot price into the cart?

Snapshotting price at add-time would show stale prices and complicate "the price went
down, honor the new one." Live pricing keeps a single source of truth (`products.price`)
and defers the one moment that legally matters — the price charged — to checkout, where it
is snapshotted into the order.

## Checkout idempotency — "a cart is checked out at most once"

Enforced by an atomic conditional update at checkout (order step):

```sql
UPDATE carts SET status = 'CHECKED_OUT'
WHERE id = :cart_id AND user_id = :user_id AND status = 'ACTIVE';
```

The first request matches one row and proceeds; a concurrent or repeated request matches
**zero** rows (status is already `CHECKED_OUT`) and is rejected with `409 Conflict`. This
guards against double-clicks and racing requests without a separate lock. Overselling is
prevented in the same transaction by re-checking and decrementing `product.inventory`,
with the `ck_products_inventory_non_negative` constraint as the final backstop.
