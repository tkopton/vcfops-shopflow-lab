-- ShopFlow lab database schema
-- Target: PostgreSQL 14+ running on vcf-db01
--
-- Run as: psql -U postgres -d shopflow -f schema.sql

CREATE TABLE IF NOT EXISTS products (
    product_id      SERIAL PRIMARY KEY,
    sku             TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    unit_price      NUMERIC(10,2) NOT NULL,
    is_featured     BOOLEAN NOT NULL DEFAULT FALSE,
    is_clearance    BOOLEAN NOT NULL DEFAULT FALSE,
    stock_qty       INTEGER NOT NULL DEFAULT 1000,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS promo_codes (
    promo_code      TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS orders (
    order_id            BIGSERIAL PRIMARY KEY,
    customer_id         INTEGER NOT NULL,
    product_id          INTEGER NOT NULL REFERENCES products(product_id),
    quantity            INTEGER NOT NULL,
    promo_code          TEXT REFERENCES promo_codes(promo_code),
    subtotal            NUMERIC(10,2) NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING -> INVOICED / FAILED
    invoice_total       NUMERIC(10,2),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    invoiced_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);

-- Trending/"featured deals" aggregation intentionally does a moderately
-- expensive join+aggregate so that a cache miss has a real, measurable cost
-- on vcf-db01. This is used by GET /api/catalog/featured on the web tier.
CREATE OR REPLACE VIEW trending_products AS
SELECT p.product_id,
       p.sku,
       p.name,
       p.unit_price,
       COUNT(o.order_id)                AS order_count,
       COALESCE(SUM(o.quantity), 0)     AS units_sold,
       COALESCE(SUM(o.subtotal), 0)     AS revenue
FROM products p
LEFT JOIN orders o
       ON o.product_id = p.product_id
      AND o.created_at > now() - interval '7 days'
WHERE p.is_featured = TRUE
GROUP BY p.product_id, p.sku, p.name, p.unit_price
ORDER BY units_sold DESC;

-- Lab-appropriate connection ceiling. Deliberately modest so the
-- exercise plays out in tens of minutes instead of hours on typical
-- lab-sized VMs. See INSTRUCTOR_GUIDE.md for the sizing math.
-- ALTER SYSTEM SET max_connections = 40;
-- ALTER SYSTEM SET idle_in_transaction_session_timeout = '900s';  -- 15 min
-- (also uncomment/apply via postgresql.conf, then restart postgresql)
