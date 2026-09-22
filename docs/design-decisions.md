# Design Decisions

Backend for a small ecommerce store: products, carts, checkout, and orders. FastAPI +
SQLAlchemy 2.0 + SQLite, with the correctness-critical parts written so they hold on
Postgres too. This document records what the system promises, where the brief was
ambiguous and what I chose, the material decisions and their alternatives, and what I
deliberately left out.

---

## 1. System invariants

These are the properties the system must never violate, and where each one is enforced.
The rule I followed: every invariant has one **authoritative** enforcement point (usually
the database), and any earlier application-level check is a courtesy for a better error
message, never the thing that keeps the invariant true.

| # | Invariant | Authoritative enforcement | Courtesy check |
|---|-----------|---------------------------|----------------|
| I1 | `products.inventory` is never negative. | Conditional `UPDATE ... WHERE inventory >= q` in checkout (`_deduct_inventory`); `ck_products_inventory_non_negative` as a last backstop. | `add_item` / `set_item_quantity` reject a line larger than live stock (409). |
| I2 | A cart is checked out **at most once**. | Compare-and-set `UPDATE carts SET status='CHECKED_OUT' WHERE id=? AND status='ACTIVE'` (`_claim_cart`); 0 rows ⇒ 409. | `_require_active` before the write phase. |
| I3 | One order per idempotency key. | `UNIQUE (orders.idempotency_key)`. | Lookup-and-return at the top of `checkout`. |
| I4 | A `PAID` order's inventory deduction happened exactly once; a `FAILED` order's net inventory effect is zero. | Deduction and restoration both live inside the one checkout transaction. | — |
| I5 | Order lines are immutable snapshots; a later product edit never changes a receipt. | `order_items.historical_name` / `historical_price` copied at checkout; no code path writes them afterwards. | — |
| I6 | A product referenced by a cart line or an order line cannot be deleted. | `ON DELETE RESTRICT` on both FKs. | — |
| I7 | One line per `(cart, product)`. | `uq_cart_items_cart_product`. | `add_item` merges into the existing line. |
| I8 | Quantities are positive; prices are positive; order money is non-negative. | `CHECK` constraints on `cart_items`, `order_items`, `products`, `orders`. | Pydantic `gt=0` / `ge=0` on request bodies. |
| I9 | `order.total_amount == Σ(historical_price × quantity) − discount_applied`, computed in integer cents. | Computed once in `checkout` from the same integers that are stored. | — |
| I10 | Nothing is reserved by being in a cart. Stock is only claimed by a successful checkout. | Consequence of I1 + no reservation table. | `has_insufficient_stock` on the cart view. |
| I11 | A cart never moves backwards from `CHECKED_OUT` to `ACTIVE` **as observed by anyone else**. | The only reverse transition (`_release_cart`, on payment decline) runs inside the same uncommitted transaction that made the claim. | — |

---

## 2. Ambiguities and the semantics I chose

The brief leaves several behaviours open. For each, what I chose and why.

| Ambiguity | Semantics chosen | Reasoning |
|-----------|------------------|-----------|
| Does adding an item to a cart **reserve** stock? | **No.** Two carts may both hold the last unit; the first to check out wins. | Reservations need expiry, cleanup, and a reservation table. Unreserved carts + an authoritative check at checkout is simpler and is what most stores actually do. |
| Which **price** does the customer pay — at add-to-cart or at checkout? | Live price in the cart; **frozen at checkout** into the order. | The only legally meaningful price is the one charged. Locking at add-time would show stale prices and make "the price went down, honour it" awkward. |
| What happens to the **cart after checkout**? | Status ⇒ `CHECKED_OUT`, lines cleared. The order is the durable record. | Keeps carts small, keeps a stable `cart_id` the client can still `GET`, and avoids a "carts that look active but aren't" trap. |
| If **payment is declined**, does an order exist? | **Yes**, with `status = FAILED`, as an audit record; inventory restored; cart back to `ACTIVE` so the customer can retry with a new key. | Failed attempts matter for support and fraud review. Rolling everything back would erase them. |
| **Same idempotency key** replayed after a decline? | Re-raise the same `402` for the same `order_id`. | The gateway stub is deterministic; replaying a failure as a failure is the honest answer. A new attempt needs a new key. |
| Is the idempotency key scoped **per cart** or **globally**? | Globally unique (DB constraint). A key reused on a *different* cart returns the original order. | Simpler; clients are told to generate a fresh UUIDv4 per attempt. Scoping to `(user, cart)` is listed under deferred. |
| `PATCH quantity = 0` — delete the line or reject? | **Reject (422).** Use `DELETE .../items/{product_id}`. | One operation per intent; keeps `quantity > 0` an unconditional invariant. |
| `POST items` for a product already in the cart — error, replace, or merge? | **Merge** (add to existing line). `PATCH` is the absolute setter. | Matches how "Add to cart" buttons behave; the unique constraint makes a duplicate line impossible anyway. |
| Is the checkout of an **empty cart** an error? | **Yes**, `409 CART_EMPTY`. | A zero-total paid order is meaningless and would confuse the "at most once" story. |
| Who is the **user**? | `user_id` is an opaque, caller-supplied string. No users table, no auth. | Out of scope for the brief; kept as a first-class column so auth can be bolted on without a migration of the data model. |
| Should a **read** (`GET /carts/{id}`) fix up a cart whose lines now exceed stock? | **No.** Reads are non-mutating; the view reports `available_inventory` per line and `has_insufficient_stock`. | Silently shrinking a customer's cart on a GET is surprising and races with the customer's own edits. |
| Money units on the wire? | Carts/products: decimal **dollar strings** (`"19.99"`). Orders: **integer cents** (`1999`). | Live display wants dollars; frozen money wants integers. Two representations, one explicit bridge (`app.money.to_cents`). |

---

## 3. Material decisions

### Decision: Optimistic, predicate-guarded writes instead of pessimistic row locks

**Context:** Checkout must never oversell (I1) and must never double-check-out a cart
(I2), including when several requests for the same product or cart arrive at the same
instant. The development database is SQLite; the target is Postgres.

**Options considered:**

1. *Pessimistic* — `SELECT ... FOR UPDATE` on the product rows (ordered by id to avoid
   deadlocks), validate in Python, write the new absolute quantity.
2. *Optimistic with a version column* — read `product.version`, then
   `UPDATE ... WHERE version = :seen`; retry on 0 rows.
3. *Optimistic with the value itself as the guard* — one atomic
   `UPDATE products SET inventory = inventory - :q WHERE id = :id AND inventory >= :q`
   and check `rowcount`; same pattern for the cart:
   `UPDATE carts SET status = 'CHECKED_OUT' WHERE id = :id AND status = 'ACTIVE'`.
4. *Serialize in the application* — a process-wide lock or a single-worker queue.

**Choice:** Option 3.

**Why:** I started with option 1 and rejected it after reproducing an oversell. On
SQLite `FOR UPDATE` is silently a no-op **and** pysqlite does not open a transaction until
the first `INSERT`/`UPDATE`, so the "locked" `SELECT` ran in autocommit. Four concurrent
checkouts each read `inventory = 60`, each computed `60 − 60 = 0` in Python, and each
wrote the absolute value `0`. All four returned `201`; 240 units were sold from a stock of
60; the `inventory >= 0` constraint never fired because `0` is valid. The bug is invisible
to sequential tests and leaves no trace in the data.

Option 3 fixes this at the root: the precondition travels *with* the write, so the
database evaluates it at write time regardless of what the application read earlier.
On Postgres (`READ COMMITTED`), a blocked `UPDATE` re-evaluates its `WHERE` after the
competing transaction commits, so exactly one wins. On SQLite the cart claim is the first
DML in the request and therefore acquires the single writer lock; everything after it is
serialized. The same code is correct on both engines with no dialect branches.

Option 2 works but adds a column and a retry loop for no gain: for a counter, the counter
*is* the version. Option 4 does not survive a second process.

**Consequences:** No lock ordering, no deadlock analysis, no version column, no retry
loop. The cost is that a loser learns late (after the gateway round-trip of the winner on
SQLite) and gets a `409`, not a queue position. Because the writes bypass the ORM identity
map, the service explicitly `expire()`s the stale attributes it touched; forgetting to do
so is the main foot-gun of this approach and is why `_release_cart` is a Core `UPDATE`
rather than an attribute assignment.

---

### Decision: Live-priced carts, snapshot-priced orders

**Context:** A product's price or stock can change between add-to-cart and checkout.
Which number does the customer see, and which do they pay?

**Options considered:** (a) snapshot price into `cart_items` at add-time; (b) live price
in the cart, frozen into `order_items` at checkout; (c) live everywhere, with orders
joining back to `products`.

**Choice:** (b).

**Why:** (a) shows stale prices and needs a "re-price" flow. (c) rewrites history whenever
an admin edits a product — receipts and tax records must not move. (b) keeps a single
source of truth for the live number and freezes exactly the number that mattered, at
exactly the moment it mattered.

**Consequences:** `CartItem.unit_price` / `line_total` and `Cart.subtotal` are computed
properties, not columns, so a cart read is always current. `OrderItem` carries
`historical_name` and `historical_price` and is readable without touching `products`.
Products referenced by history are undeletable (I6).

---

### Decision: Integer cents on orders, `Decimal` dollars on products and carts

**Context:** Money must add up exactly, and a receipt must be reproducible forever.

**Options considered:** `float` (rejected immediately — `0.1 + 0.2`); `Decimal`
everywhere; integer cents everywhere; a split.

**Choice:** `Numeric(12,2)` / `Decimal` for the live catalogue and cart; integer cents
for everything on an order. One conversion function, `app.money.to_cents`.

**Why:** Integer arithmetic has no rounding mode to get wrong and no scale to track;
`line_total = historical_price * quantity` is one multiplication. Keeping the catalogue in
`Decimal` matches how prices are entered and displayed and lets Pydantic enforce
`decimal_places=2` at the boundary. The split costs one explicit bridge, which is a
cheap price for never having a `Decimal("19.990000000001")` in an order.

**Consequences:** Two wire representations (see the rounding rules in §5). The order API
follows the Stripe convention (`total_amount: 1999`). A future coupon computes its
discount in cents, after the subtotal, so rounding happens at most once per order.

---

### Decision: Client-supplied idempotency key, stored on the order

**Context:** A checkout request can time out after the server has committed. The client
retries. That retry must not place a second order or charge a second time.

**Options considered:** (a) client sends a UUID in the body, stored `UNIQUE` on `orders`;
(b) server-generated token issued by a `POST /checkout-sessions` step; (c) rely on cart
status alone (`CHECKED_OUT` rejects the retry); (d) an `Idempotency-Key` HTTP header with
a response cache.

**Choice:** (a), with (c) as an independent second guard.

**Why:** (c) alone gives the retrying client a `409` for a checkout that *succeeded*, with
no way to find the order it paid for. (a) lets the retry return the original order
(`201`, same `id`). (b) adds a round-trip and a table for the same guarantee. (d) is the
"proper" HTTP shape but needs a response store; the order row *is* the response store
here, so storing the key on it is the minimal version of the same idea.

**Consequences:** The key is required (`422` without it). The gateway stub receives the
same key, which is how a real gateway would de-duplicate the charge. Two concurrent
requests with the same key can both miss the up-front lookup; the key is therefore
re-checked at the two points where such a race actually resolves — a lost cart claim, and
a rejected `UNIQUE(idempotency_key)` insert — so the loser returns the winner's order (or
re-raises its decline) instead of a spurious `409` or an unhandled `500` (see §4).

---

### Decision: A declined payment persists a `FAILED` order and releases the cart

**Context:** The gateway says no. What state is left behind?

**Options considered:** (a) roll back everything, return `402`, nothing persisted;
(b) persist the order as `FAILED`, restore inventory, cart back to `ACTIVE`; (c) persist
`FAILED` and leave the cart `CHECKED_OUT` (customer starts a new cart).

**Choice:** (b).

**Why:** Support and fraud review need to see failed attempts; (a) erases them. (c)
punishes the customer for a declined card. (b) is the only option where a retry with a
fresh key is a normal checkout of the same cart.

**Consequences:** The cart briefly becomes `CHECKED_OUT` and then `ACTIVE` again *within
one uncommitted transaction*; no other session ever observes the reversal (I11).
`OrderStatus` is a real state machine (`PENDING → PAID | FAILED`) with no exits from a
terminal state. A replay of the failed key returns the same `402` and `order_id`.

---

### Decision: The gateway is called inside the transaction, before commit

**Context:** Where in the flow does the (side-effecting, slow, fallible) payment call sit?

**Options considered:** (a) charge first, then write; (b) write everything, commit, then
charge, then update status (two transactions); (c) stage all writes, charge, then commit
or compensate — one transaction.

**Choice:** (c).

**Why:** (a) charges before we know the stock is there. (b) is the production-grade shape
but needs a reconciliation job for the crash-between-commit-and-charge window, which is
more machinery than this brief warrants. (c) keeps the atomicity story trivially true —
either the order, the deduction, and the charge all happened, or none did — at the cost
of holding a write transaction open across the gateway's latency.

**Consequences:** On SQLite the single writer lock is held for the duration of the
charge, which serializes all checkouts behind the slowest gateway call. This is the
first thing I would change for production (§8, §10). The stub is deterministic and has no
network, so the cost is invisible here; the seam (`app.services.payment.charge`) is where
a real adapter goes.

---

### Decision: Cart lifecycle as a status column, not delete-on-checkout

**Context:** After checkout, what is a cart?

**Options considered:** delete the row; keep it and add `cart_id` to `orders`; keep it
with a `status` enum.

**Choice:** `status ∈ {ACTIVE, CHECKED_OUT}`, lines cleared on success.

**Why:** Deleting breaks any client holding the id. A status column is the natural place
to hang the atomic "at most once" claim (I2) — the compare-and-set needs a column to
compare. Clearing the lines keeps checked-out carts from carrying a stale, live-priced
view of what was bought; the order is that record.

**Consequences:** `GET /carts/{id}` on a checked-out cart returns `status: CHECKED_OUT`
and an empty `items` list, which is a slightly odd read; a `cart_id` on `orders` would
let the client hop to the receipt and is listed as deferred.

---

### Decision: Thin routers, one service layer that owns invariants, DB constraints as backstop

**Context:** Where do rules live?

**Choice:** Every write goes through `app.services.*`; routers translate HTTP ↔ service
calls and nothing else. The database carries `CHECK`/`UNIQUE`/FK constraints that mirror
the service rules.

**Why:** One place to read to understand what the system permits, and one place to test
it without HTTP. The constraints exist so that a bug in the service layer produces an
error rather than bad data.

**Consequences:** Some checks appear twice (Python and SQL). That duplication is
deliberate and cheap. A constraint violation that reaches the client is a `500` — it is a
bug signal, not a user error, so it is not mapped to a friendly code.

---

## 4. Transaction, concurrency, and idempotency strategy

**Transactions.** One SQLAlchemy `Session` per request (`get_db`), `expire_on_commit=False`
so returned objects are readable after commit. Cart mutations are single-statement
transactions. Checkout is one transaction whose write phase begins at the cart claim and
ends at `commit()`; every early exit before that point has touched nothing, and every
early exit after it (`_claim_cart` losing, `_deduct_inventory` losing) calls
`db.rollback()` explicitly before raising so the session is clean for the error handler.
The `with SessionLocal()` context also rolls back on any unhandled exception.

**Concurrency.** Optimistic: no lock is taken ahead of time; every racy write carries its
precondition in its `WHERE` clause and the code inspects `rowcount`.

| Race | Guard | Loser sees |
|------|-------|-----------|
| Two carts, last unit of a product | `UPDATE products ... WHERE inventory >= q` | `409 INSUFFICIENT_INVENTORY` with live `available` |
| Same cart, two different keys | `UPDATE carts ... WHERE status = 'ACTIVE'` | `409 CART_NOT_ACTIVE` |
| Same cart, same key, both miss the lookup | Cart claim above, then `_find_by_key` again | **The winner's order** (`201`, same `id`) — idempotency survives the race |
| Same key, different carts (client misuse) | `UNIQUE(idempotency_key)` rejects the loser's insert; `IntegrityError` ⇒ `rollback()` ⇒ re-lookup | The winner's order, not a `500` |
| Multi-line cart, partial deduction then a loss | `rollback()` undoes the earlier decrements and the cart claim | `409 INSUFFICIENT_INVENTORY` |

Decrements are issued in `product_id` order. That is not for deadlock avoidance (there
are no held locks to deadlock on) but so that two multi-line carts contending for the same
products fail fast on the same line.

Engine notes: on Postgres this is standard `READ COMMITTED` behaviour — a blocked
`UPDATE` re-checks its predicate once the blocker commits. On SQLite, `PRAGMA
journal_mode=WAL` lets readers proceed during a checkout and `busy_timeout=5000` makes a
second writer wait rather than fail; the cart claim is the first DML, so it acquires the
writer lock and serializes the rest of the checkout. `tests/test_checkout.py::
test_concurrent_checkouts_never_oversell` fires four full-stock checkouts through a
deliberately slow gateway and asserts `[201, 409, 409, 409]` and a final stock of exactly
zero. It fails against the earlier pessimistic implementation.

**Idempotency.** Two independent guards. (1) A required client-generated key stored
`UNIQUE` on `orders`: a replay returns the original `PAID` order or re-raises the original
`402`. The key is checked three times: up front (the common retry-after-completion case),
after a lost cart claim, and on a rejected unique insert — the last two are what make the
guarantee hold when two same-key requests are genuinely simultaneous. (2) The cart status
claim: even with a new key, a `CHECKED_OUT` cart is refused.
Cart mutations (`POST/PATCH/DELETE items`) are not keyed; `PATCH` and `DELETE` are
naturally idempotent, and `POST items` merges, so a doubled request over-adds rather than
duplicating a line — acceptable for a cart, and correctable by the client.

---

## 5. Money and rounding rules

1. **Catalogue and cart:** `Numeric(12, 2)` in the DB, `Decimal` in Python, serialized as
   a two-place **string** (`"19.99"`). Clients must parse with a decimal type. Pydantic
   enforces `decimal_places=2, gt=0` on the way in.
2. **Orders:** integer **cents** everywhere — `total_amount`, `discount_applied`,
   `historical_price`, `line_total`. No `Decimal`, no float, no scale.
3. **The bridge:** `to_cents(Decimal) -> int` quantizes to `0.01` with `ROUND_HALF_UP`
   then multiplies by 100 in exact `Decimal` arithmetic. Because prices are already
   two-place, the quantize is a safety net; it never changes a value in practice.
4. **Line totals:** `unit_cents × quantity` — integer multiplication, no rounding.
5. **Subtotal:** integer sum of line totals.
6. **Discounts (future):** computed in cents from the cents subtotal, rounded
   `ROUND_HALF_UP` **once, at the order level**, never per line. Stored as a non-negative
   integer (`ck_orders_discount_non_negative`). `total = subtotal − discount`, and
   `ck_orders_total_non_negative` forbids a discount larger than the subtotal.
7. **Currency:** single, implicit (USD). No currency column; adding one is a migration,
   not a redesign.

---

## 6. Error model

**Shape.** Every error is `{"error": {"code", "message", "details"?}}`. `code` is a stable
string for programmatic handling; `message` is for humans; `details` carries the ids and
numbers the client needs to react (`product_id`, `requested`, `available`, `order_id`, …).
Pydantic validation failures are wrapped into the same envelope as `422 VALIDATION_ERROR`.

**Mechanism.** Four tiers of handler in `app/errors.py`, so *no* failure escapes the
envelope:

1. **Typed application errors.** A small hierarchy (`AppError → NotFound | Conflict | …`).
   Services raise these and know nothing about HTTP; one handler maps them. Adding an
   error is one class.
2. **Framework HTTPExceptions.** Unknown route, wrong method, and content negotiation are
   re-wrapped from FastAPI's `{"detail": …}` into the same envelope with a stable `code`.
3. **Database faults.** `IntegrityError → 409 CONSTRAINT_VIOLATION` (a conflict the service
   didn't translate itself); `OperationalError` and pool `TimeoutError → 503
   SERVICE_UNAVAILABLE` (transient). The raw DB message is logged, never returned.
4. **Catch-all.** Any other `SQLAlchemyError` or `Exception → 500 INTERNAL_ERROR`, logged
   with a stack trace.

Every `5xx` carries a `request_id` in `details` and the `X-Request-ID` header, and the
same id is written to the log line — the client reports it, the operator greps it. Internal
detail (SQL, stack traces, exception messages) is logged, never serialized to the client.

**Status code choices.**

| Situation | Code | Why this and not another |
|-----------|------|--------------------------|
| Missing cart / product / order / line; unknown route | `404` | Resource identity problem. |
| Wrong HTTP method on a known path | `405` | Route exists, verb doesn't. |
| Body fails shape validation | `422` | FastAPI convention; the *request* is malformed. |
| Cart is `CHECKED_OUT`; cart is empty; stock insufficient; unmapped constraint violation | `409 CONFLICT` | The request is well-formed but conflicts with **current state**. Not `400` (nothing wrong with the request) and not `422` (nothing wrong with its shape). |
| Payment declined | `402 PAYMENT_REQUIRED` | The one status that says "your money didn't go through". The order id is in `details` so the client can show it. |
| Lost DB connection, lock timeout, **connection-pool exhaustion** | `503 SERVICE_UNAVAILABLE` | Transient and not the client's fault. Carries `Retry-After`; the client backs off and retries the same request. `pool_pre_ping` + `pool_timeout=5` on non-SQLite engines make these surface promptly instead of hanging. |
| Any other unexpected exception | `500 INTERNAL_ERROR` | A bug. Loud in the logs (stack trace + `request_id`), opaque to the client. |

**Retry guidance (what the codes tell a client).**

- Network timeout / `5xx` on checkout → **retry with the same key**. Safe: you get the
  original order or the original failure.
- `503 SERVICE_UNAVAILABLE` → **retry after `Retry-After`**; the DB was momentarily busy or
  saturated. Reuse the same idempotency key on checkout.
- `402` → **do not retry the same key**; it will fail identically. Fix payment, use a new
  key.
- `409 INSUFFICIENT_INVENTORY` → not retryable as-is; `details.available` says what to
  reduce to.
- `409 CART_NOT_ACTIVE` → never retry; the cart is done (or someone else is finishing it).
- `404` / `405` / `422` → client bug.

---

## 7. Implemented vs. intentionally deferred

**Implemented**

- Product catalogue (read-only API, idempotent JSON seed, pagination).
- Cart lifecycle with `ACTIVE / CHECKED_OUT`, add-merge / set / remove lines, live pricing
  and live availability on the read model.
- Checkout: idempotency key, cart claim, conditional inventory deduction, snapshot
  order lines in integer cents, deterministic gateway stub, `PENDING → PAID | FAILED`.
- Uniform error envelope with stable codes.
- Same-key race recovery: a request that loses the cart claim or the unique insert to a
  competitor with the same key returns that competitor's order.
- 38 tests, including a genuine concurrency test that fails on the naïve implementation
  and two race-recovery tests that drive checkout into the lost-claim and lost-insert
  branches.

**Deferred (and what the seam looks like)**

| Item | Why deferred | Seam |
|------|--------------|------|
| Coupons / rewards | No coupon table yet; the brief's rules were not needed to make checkout correct. | `discount_cents = 0` in `checkout` step 5; `Order.discount_applied` already stored. A coupon step slots between snapshot and deduction: validate + atomically consume the coupon (`UPDATE coupons SET used_at = now() WHERE code = ? AND used_at IS NULL`) using the same rowcount pattern. |
| `cart_id` on `orders` | Not needed for correctness; the idempotency key already links a retry to its order. | One nullable FK column + index. |
| Idempotency key scoped to `(user_id, cart_id)` | Global uniqueness is simpler and safe if clients generate UUIDs; a key reused across carts already resolves to the first order rather than erroring. | Composite unique constraint; return `409` on a key reused across carts. |
| Authentication / ownership | Out of scope. `user_id` is trusted from the body. | Replace the body field with an auth dependency; nothing else changes. |
| Alembic migrations | `create_all` is fine for a single SQLite file. | Standard Alembic setup; the models already are the schema. |
| Product write API / inventory restock | Brief did not ask for it. | Restock must be `inventory = inventory + n`, never an absolute write — same lesson as §3. |
| Cart expiry / abandoned-cart cleanup | No reservation means abandoned carts cost nothing but rows. | A `last_touched_at` column and a sweep. |

---

## 8. Evolution to multiple service instances and production scale

The design already assumes nothing in process memory matters: every guard is a database
predicate, so N instances against one Postgres behave exactly like one instance. What
changes as scale grows:

1. **Postgres.** SQLite's single writer becomes the bottleneck at the first concurrent
   checkout. The code needs no changes: conditional `UPDATE`s and `rowcount` are
   standard SQL, and the test suite is the acceptance check. Running it against Postgres
   is the first thing in §10.
2. **Move the gateway call out of the write transaction.** Holding row locks across a
   network call caps checkout throughput at gateway latency per hot product. The
   production shape is: (a) claim cart + deduct inventory + insert `PENDING` order,
   **commit**; (b) call the gateway with the same idempotency key; (c) new transaction:
   flip to `PAID`, or `FAILED` + restore + release. A crash between (a) and (c) leaves a
   `PENDING` order that a reconciliation worker resolves by asking the gateway what
   happened for that key. This is a transactional-outbox pattern and is the largest
   single change from the current code.
3. **Same-key concurrency across instances.** Already handled: the loser of the cart
   claim or of the unique insert re-reads by key and returns the winner's order. Nothing
   about this depends on being in one process. On Postgres the `IntegrityError` path is
   the one that fires (a blocked `INSERT` on a unique index waits for the competing
   transaction and is then rejected), so it deserves a Postgres-backed test.
4. **Hot rows.** A flash-sale product turns `UPDATE products WHERE id = ?` into a
   serialization point. Mitigations, in order of cost: shorter transactions (item 2);
   splitting inventory into buckets that are summed; or a Redis-side reservation with a
   TTL that fronts the database and lets the DB decrement be a formality.
5. **Reads.** `GET /products` and `GET /carts/{id}` are read-only and go to replicas; cart
   reads compute `subtotal` in Python from at most a handful of rows, which is fine, but
   a product cache in front of the join would be the next step.
6. **Operational.** Alembic; structured logs with `idempotency_key` as the correlation id;
   metrics on `409` rates per product (an oversell-attempt counter is the leading
   indicator that the reservation model is wrong for the business); rate limiting on
   checkout; auth.

---

## 9. How I used AI tools

I used Claude Code throughout as a pair: for scaffolding models and routers, drafting
tests, and stress-testing the design by asking it to argue against my choices. Every
generated change was read and run before it was committed. Two examples of where the
output was wrong or where I redirected it materially:

**Rejected: the pessimistic locking implementation, and the doc that defended it.**
The first checkout implementation used `SELECT ... FOR UPDATE` on the product rows and
decremented inventory in Python. The accompanying explanation claimed that "on SQLite the
row lock is a no-op, but WAL + `busy_timeout` serialize writers, so the invariant still
holds." That sentence was plausible and wrong. I had already decided I wanted optimistic
semantics — a predicate on the write, not a lock before it — and asked for the switch.
Before changing anything we reproduced the failure: four concurrent checkouts of a
60-unit product through a 300 ms gateway all returned `201`, 240 units were sold, and the
product ended at `inventory: 0` with nothing to show anything was wrong. The root cause
was two-fold: `FOR UPDATE` is ignored by SQLite, and pysqlite defers `BEGIN` until the
first DML, so the "locked" read ran in autocommit and each request then wrote a Python-
computed absolute value. The replacement (§3, first decision) puts the precondition in
the `UPDATE` itself and uses a compare-and-set for the cart claim. The concurrency test
that now guards this was written first, confirmed red against the old code, then green
against the new.

**Redirected: cart release on payment decline.** The first draft of the optimistic
version assigned `cart.status = ACTIVE` in Python after a decline. Because the claim had
been made with a Core `UPDATE` that bypassed the ORM, the ORM's own snapshot still said
`ACTIVE`, so the assignment was a no-op and the cart would have stayed `CHECKED_OUT`
after every decline. I replaced it with an explicit `UPDATE` and an `expire()` so the
ORM object cannot lie. This was caught in review before the code was run; the existing
decline test (`test_payment_decline_reverses_inventory_and_keeps_cart_active`) would have
caught it on the first run, which is the argument for having written it before the
refactor.

**Rejected: "tests run against a throwaway SQLite file, never `store.db`."** That
sentence, in the README and in `conftest.py`, was also plausible and wrong. The fixture
set `STORE_DATABASE_URL` and *then* imported the app — but `tests/test_products.py`
imports `app.seed` at collection time, which imports `app.db`, which builds the engine
from `settings` before any fixture runs. Every test run had been writing into the real
development database. It surfaced only because the new concurrency test sells a
product's entire stock on each run: after a few runs the seed catalogue was at zero and
seventeen unrelated tests failed. The fix moves the override to conftest import time and
adds an assertion that the engine URL is the test URL, so the suite refuses to run
against the wrong database rather than silently doing so.

One smaller fix of the same kind in the same pass: `Product.id.in_(...)` was handed a
generator instead of a list.

---

## 10. What I would examine first with another two hours

1. **Run the suite against Postgres.** Every concurrency claim in this document is
   "standard SQL" — verify it with a `STORE_DATABASE_URL=postgresql://…` run, in
   particular that the `UPDATE ... WHERE status = 'ACTIVE'` loser gets `rowcount == 0`
   after the blocker commits, and that `db.refresh(cart)` after `rollback()` sees the
   committed value.
2. **The commit-vs-charge window.** With the gateway inside the transaction, a process
   crash after `charge()` returns `PAID` but before `commit()` charges the customer and
   records nothing. Move to the two-phase shape in §8 item 2, with a reconciliation path
   keyed on `idempotency_key`.
3. **Same-key race at the HTTP layer.** The two race-recovery tests reach the
   lost-claim and lost-insert branches by monkeypatching the first `_find_by_key`. A
   black-box version — two threads, one key, a slow gateway, assert both responses are
   `201` with the same `order_id` — would close the gap between "the branch works" and
   "the branch is reached".
4. **`cart_id` on `orders`** plus `GET /carts/{id}/order`, so a client that only has a cart
   id can reach the receipt.
5. **Coupons**, using the same `UPDATE ... WHERE used_at IS NULL` rowcount pattern so a
   single-use code is single-use under concurrency for the same reason inventory is.
