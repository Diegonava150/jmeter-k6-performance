-- Hermetic dataset for the perf stack. Seeded once at container init.
-- Shapes matter here. 400k products is not for the row count itself, it is so
-- that `search_products` costs something real: `products.name` has no index that
-- can serve LIKE '%widget%', and the query sorts by price, so Postgres must scan
-- and sort every match rather than stopping at the LIMIT. Measured on this data
-- that is a ~25ms sequential scan against ~2ms for the indexed browse.
--
-- The workload needs a fast path and a slow path because a load test where every
-- request costs the same measures nothing: the percentile has nothing to be a
-- percentile OF.

CREATE SCHEMA api;

CREATE TABLE api.products (
    id          serial PRIMARY KEY,
    name        text        NOT NULL,
    sku         text        NOT NULL,
    price_cents integer     NOT NULL,
    in_stock    boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api.orders (
    id           serial PRIMARY KEY,
    customer_id  integer     NOT NULL,
    total_cents  integer     NOT NULL,
    status       text        NOT NULL,
    placed_at    timestamptz NOT NULL DEFAULT now()
);

INSERT INTO api.products (name, sku, price_cents, in_stock)
SELECT
    CASE WHEN i % 7 = 0 THEN 'widget ' || i ELSE 'gadget ' || i END,
    'SKU-' || lpad(i::text, 8, '0'),
    (random() * 50000)::int + 100,
    (i % 11) <> 0
FROM generate_series(1, 400000) AS i;

INSERT INTO api.orders (customer_id, total_cents, status)
SELECT
    (random() * 5000)::int + 1,
    (random() * 250000)::int + 500,
    (ARRAY['placed','shipped','delivered','cancelled'])[(i % 4) + 1]
FROM generate_series(1, 20000) AS i;

-- Indexed: browse_products and read_orders are the fast path.
CREATE INDEX ON api.products (id);
CREATE INDEX ON api.orders (id);
ANALYZE api.products;
ANALYZE api.orders;

-- PostgREST roles: `authenticator` logs in and switches to the anon role.
CREATE ROLE perf_anon NOLOGIN;
GRANT USAGE ON SCHEMA api TO perf_anon;
GRANT SELECT ON api.products, api.orders TO perf_anon;

CREATE ROLE authenticator LOGIN NOINHERIT PASSWORD 'perfpass';
GRANT perf_anon TO authenticator;
