import json

import psycopg
import pytest

from pg_readonly_mcp.database import Database, QueryError
from pg_readonly_mcp.guard import Rejected


def audit_lines(db):
    return [json.loads(line) for line in db.cfg.audit_log.read_text().splitlines()]


# ---------- queries that should work ----------

def test_simple_select(db):
    r = db.run_query("SELECT id, status, amount FROM orders ORDER BY id LIMIT 3")
    assert r["columns"] == ["id", "status", "amount"]
    assert r["row_count"] == 3 and r["truncated"] is False
    assert r["rows"][0] == [1, "paid", "42.01"]
    assert r["tables"] == ["public.orders"]


def test_join_cte_and_window_function(db):
    r = db.run_query("""
        WITH paid AS (SELECT customer_id, sum(amount) AS total FROM orders WHERE status = 'paid' GROUP BY 1)
        SELECT c.country, count(*) AS customers, round(avg(p.total), 2) AS avg_paid,
               rank() OVER (ORDER BY count(*) DESC) AS rnk
        FROM customers c JOIN paid p ON p.customer_id = c.id
        GROUP BY c.country ORDER BY rnk, c.country""")
    assert r["columns"] == ["country", "customers", "avg_paid", "rnk"]
    assert r["tables"] == ["public.customers", "public.orders"]
    assert sum(row[1] for row in r["rows"]) == 50


def test_postgres_syntax_survives_the_guard(db):
    r = db.run_query("""
        SELECT date_trunc('month', created_at)::date AS month,
               count(*) FILTER (WHERE status = 'paid') AS paid,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY amount) AS median,
               meta->>'source' AS source
        FROM orders
        WHERE created_at >= timestamptz '2026-03-01' AND meta->>'source' ILIKE 'w%'
        GROUP BY 1, 4 ORDER BY 1""")
    assert r["row_count"] >= 1 and {row[3] for row in r["rows"]} == {"web"}
    r = db.run_query("SELECT DISTINCT ON (customer_id) customer_id, id FROM orders ORDER BY customer_id, created_at DESC")
    assert r["row_count"] == 50
    r = db.run_query("SELECT g, x.n FROM generate_series(1, 3) AS g CROSS JOIN LATERAL "
                     "(SELECT count(*) AS n FROM orders o WHERE o.customer_id = g) AS x ORDER BY g")
    assert [row[0] for row in r["rows"]] == [1, 2, 3]


def test_views_and_whole_schema_entries(db):
    assert db.run_query("SELECT * FROM order_totals ORDER BY customer_id LIMIT 1")["tables"] == ["public.order_totals"]
    assert db.run_query("SELECT sum(orders) FROM analytics.daily_revenue")["rows"] == [[1000]]


def test_identifiers_fold_like_postgres(db):
    for sql in ('SELECT count(*) FROM "orders"', "SELECT count(*) FROM ORDERS", "SELECT count(*) FROM Public.Orders"):
        assert db.run_query(sql)["rows"] == [[1000]]


# ---------- queries that must be refused ----------

REFUSED = [
    ("SELECT * FROM api_keys", "public.api_keys is not in this server's allow-list"),
    ("SELECT c.name, k.secret FROM customers c JOIN api_keys k ON k.customer_id = c.id", "public.api_keys"),
    ("SELECT * FROM orders WHERE customer_id IN (SELECT customer_id FROM public.api_keys)", "public.api_keys"),
    ("SELECT (SELECT secret FROM api_keys LIMIT 1) AS s FROM orders", "public.api_keys"),
    ("WITH api_keys AS (SELECT * FROM api_keys) SELECT * FROM api_keys", "also the name of a table"),
    ("WITH orders AS (SELECT secret FROM api_keys) SELECT * FROM orders", "also the name of a table"),
    (r"SELECT 'a\' AS c, (SELECT secret FROM api_keys LIMIT 1) AS s --'", "public.api_keys"),
    ("SELECT * FROM pg_roles", "pg_catalog.pg_roles is not in this server's allow-list"),
    ("SELECT relname FROM pg_class", "pg_catalog.pg_class"),
    ("SELECT * FROM information_schema.tables", "information_schema.tables"),
    ("SELECT * FROM nope", "no table or view called nope"),
    ("SELECT query_to_xml('select * from api_keys', true, false, '')", "query_to_xml() is not allowed"),
    ("SELECT pg_read_file('/etc/passwd')", "pg_read_file() is not allowed"),
    ("SELECT * FROM pg_catalog.pg_read_file('/etc/passwd')", "pg_read_file() is not allowed"),
    ("SELECT set_config('search_path', 'pg_catalog', false)", "set_config() is not allowed"),
    ("SELECT pg_sleep(5)", "pg_sleep() is not allowed"),
    ("SELECT dblink('host=elsewhere', 'select 1')", "dblink() is not allowed"),
    ("SELECT nextval('orders_id_seq')", "nextval() is not allowed"),
    ("SELECT public.my_function(1)", "without a schema prefix"),
    ("SELECT some_udf(id) FROM orders", "not on this server's list of allowed functions"),
    ("INSERT INTO orders (customer_id, status, amount, created_at) VALUES (1, 'paid', 1, now())", "Only SELECT"),
    ("UPDATE orders SET amount = 0", "Only SELECT"),
    ("DELETE FROM orders", "Only SELECT"),
    ("DROP TABLE orders", "Only SELECT"),
    ("CREATE TABLE t AS SELECT * FROM orders", "Only SELECT"),
    ("TRUNCATE orders", "Only SELECT"),
    ("COPY orders TO '/tmp/orders.csv'", "Only SELECT"),
    ("SET search_path = pg_catalog", "Only SELECT"),
    ("BEGIN", "Only SELECT"),
    ("SELECT * INTO copy_of_orders FROM orders", "SELECT ... INTO is not allowed"),
    ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "DELETE is not allowed"),
    ("SELECT * FROM orders FOR UPDATE", "FOR UPDATE / FOR SHARE locks is not allowed"),
    ("SELECT 1; DELETE FROM orders", "exactly one statement"),
    ("SELECT 1; SELECT 2", "exactly one statement"),
    ("   ", "empty"),
]


@pytest.mark.parametrize("sql, reason", REFUSED, ids=[r[0][:40] for r in REFUSED])
def test_refused(db, sql, reason):
    with pytest.raises(Rejected) as e:
        db.run_query(sql)
    assert reason in str(e.value)


# ---------- limits ----------

def test_row_limit(db):
    r = db.run_query("SELECT id FROM orders ORDER BY id")
    assert r["row_count"] == 50 and r["truncated"] is True and "first 50 rows" in r["note"]
    assert db.run_query("SELECT id FROM orders", limit=5)["row_count"] == 5
    assert db.run_query("SELECT id FROM orders", limit=100_000)["row_count"] == 50  # the server's cap wins


def test_statement_timeout_and_recovery(db):
    with pytest.raises(QueryError, match="statement timeout"):
        db.run_query("SELECT count(*) FROM orders a, orders b, orders c")  # a billion-row cross join
    assert db.run_query("SELECT 1 AS ok")["rows"] == [[1]]  # the session is usable afterwards


def test_cells_are_json_safe_and_clipped(make_cfg):
    d = Database(make_cfg(MCP_PG_MAX_CELL_CHARS="20"))
    r = d.run_query("SELECT amount, created_at, meta, repeat('x', 100) AS long, NULL::int AS nothing, "
                    "ARRAY[1, 2] AS arr, decode('00ff', 'hex') AS raw FROM orders ORDER BY id LIMIT 1")
    d.close()
    amount, created, meta, long, nothing, arr, raw = r["rows"][0]
    assert amount == "42.01" and created.startswith("2026-03-01T03:00")
    assert meta == {"source": "app"}
    assert long == "x" * 20 + "... [80 more characters]"
    assert nothing is None and arr == [1, 2] and raw == "base64:AP8="
    json.dumps(r)


# ---------- the database itself is a second wall ----------

def test_superuser_is_refused_unless_explicitly_allowed(shop, make_cfg):
    d = Database(make_cfg(MCP_PG_DSN=shop["admin"]))
    with pytest.raises(QueryError, match="Refusing to run as superuser"):
        d.run_query("SELECT 1")
    d = Database(make_cfg(MCP_PG_DSN=shop["admin"], MCP_PG_ALLOW_SUPERUSER="1"))
    assert any("superuser" in w for w in d.list_tables()["warnings"])
    d.close()


def test_role_that_can_write_gets_a_warning_and_still_cannot_write(shop, make_cfg):
    d = Database(make_cfg(MCP_PG_DSN=shop["writer"], MCP_PG_ALLOWED_TABLES="public.orders"))
    out = d.list_tables()
    assert [t["table"] for t in out["tables"]] == ["public.orders"]
    assert any("public.orders" in w for w in out["warnings"])
    # Skip the guard on purpose: PostgreSQL refuses the write because the transaction is READ ONLY.
    conn = d._connection()
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute("INSERT INTO orders (customer_id, status, amount, created_at) VALUES (1, 'paid', 1, now())")
    conn.rollback()
    d.close()


# ---------- the other tools ----------

def test_list_tables_shows_only_the_allow_list(db):
    out = db.list_tables()
    names = [t["table"] for t in out["tables"]]
    assert names == ["analytics.daily_revenue", "public.customers", "public.order_totals", "public.orders"]
    orders = next(t for t in out["tables"] if t["table"] == "public.orders")
    assert orders["approx_rows"] == 1000 and "GBP" in orders["comment"] and orders["kind"] == "table"
    assert out["row_limit"] == 50 and "warnings" not in out


def test_describe_table(db):
    d = db.describe_table("orders")
    assert d["table"] == "public.orders" and d["primary_key"] == ["id"]
    cols = {c["name"]: c for c in d["columns"]}
    assert cols["amount"]["type"] == "numeric(10,2)" and cols["amount"]["nullable"] is False
    assert cols["meta"]["default"].startswith("'{}'")
    assert d["foreign_keys"] == ["FOREIGN KEY (customer_id) REFERENCES customers(id)"]
    assert db.describe_table("analytics.daily_revenue")["kind"] == "materialized view"
    with pytest.raises(Rejected, match="allow-list"):
        db.describe_table("api_keys")
    with pytest.raises(Rejected, match="no table or view"):
        db.describe_table("nope")


def test_explain_does_not_run_the_query(db):
    out = db.explain_query("SELECT * FROM orders WHERE customer_id = 7")
    assert "orders" in out["plan"] and out["tables"] == ["public.orders"]
    with pytest.raises(Rejected):
        db.explain_query("SELECT * FROM api_keys")


def test_audit_log_records_every_outcome(db):
    db.run_query("SELECT count(*) FROM customers")
    with pytest.raises(Rejected):
        db.run_query("SELECT * FROM api_keys")
    with pytest.raises(QueryError):
        db.run_query("SELECT no_such_column FROM orders")
    ok, rejected, error = audit_lines(db)[-3:]
    assert (ok["status"], rejected["status"], error["status"]) == ("ok", "rejected", "error")
    assert ok["tables"] == ["public.customers"] and ok["rows"] == 1 and ok["executed_sql"].startswith("SELECT COUNT(*)")
    assert rejected["sql"] == "SELECT * FROM api_keys" and "api_keys" in rejected["reason"]
    assert "no_such_column" in error["error"]
    assert db.cfg.audit_log.stat().st_mode & 0o777 == 0o600
