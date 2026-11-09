-- Demo database for pg-readonly-mcp: a small shop, plus one table the server must never expose.
-- docker compose runs this on first start; the tests run it in a throwaway database.

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE customers (
  id serial PRIMARY KEY,
  name text NOT NULL,
  email text NOT NULL UNIQUE,
  country text NOT NULL,
  created_at timestamptz NOT NULL
);
COMMENT ON TABLE customers IS 'One row per customer account.';
COMMENT ON COLUMN customers.country IS 'ISO 3166-1 alpha-2 country code';

CREATE TABLE orders (
  id serial PRIMARY KEY,
  customer_id int NOT NULL REFERENCES customers (id),
  status text NOT NULL CHECK (status IN ('paid', 'refunded', 'pending')),
  amount numeric(10, 2) NOT NULL,
  meta jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL
);
COMMENT ON TABLE orders IS 'One row per order; amount is in GBP.';
CREATE INDEX orders_customer_id_idx ON orders (customer_id);
CREATE INDEX orders_created_at_idx ON orders (created_at);

CREATE TABLE api_keys (
  id serial PRIMARY KEY,
  customer_id int NOT NULL REFERENCES customers (id),
  secret text NOT NULL
);
COMMENT ON TABLE api_keys IS 'Credentials: deliberately left out of the allow-list.';

INSERT INTO customers (name, email, country, created_at)
SELECT 'Customer ' || i, 'customer' || i || '@example.com', (ARRAY['GB', 'US', 'DE', 'FR', 'IN'])[1 + i % 5],
       timestamptz '2026-01-01' + make_interval(days => i)
FROM generate_series(1, 50) AS i;

INSERT INTO orders (customer_id, status, amount, meta, created_at)
SELECT 1 + (i / 20) % 50,
       (ARRAY['paid', 'paid', 'paid', 'refunded', 'pending'])[1 + i % 5],
       round(5 + (i * 37) % 200 + (i % 100) / 100.0, 2),
       jsonb_build_object('source', (ARRAY['web', 'app', 'email'])[1 + i % 3]),
       timestamptz '2026-03-01' + make_interval(hours => i * 3)
FROM generate_series(1, 1000) AS i;

INSERT INTO api_keys (customer_id, secret)
SELECT i, md5('secret-' || i) FROM generate_series(1, 50) AS i;

CREATE VIEW order_totals AS
SELECT customer_id, count(*) AS orders, sum(amount) FILTER (WHERE status = 'paid') AS paid_amount
FROM orders GROUP BY customer_id;
COMMENT ON VIEW order_totals IS 'Orders and paid revenue per customer.';

CREATE MATERIALIZED VIEW analytics.daily_revenue AS
SELECT created_at::date AS day, count(*) AS orders, sum(amount) FILTER (WHERE status = 'paid') AS revenue
FROM orders GROUP BY 1;

ANALYZE;

-- The role the MCP server connects as: it can log in and read, nothing else. SELECT is granted on
-- every table here, api_keys included, so you can watch the server's allow-list refuse api_keys on
-- its own. In production, grant SELECT only on the tables you expose.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_readonly') THEN
    CREATE ROLE mcp_readonly LOGIN PASSWORD 'readonly';
  END IF;
END
$$;
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public, analytics TO mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public, analytics TO mcp_readonly;
