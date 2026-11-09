# pg-readonly-mcp

A small MCP server that lets Claude query a PostgreSQL database **read-only**, with a table allow-list, a row limit, a statement timeout, and an audit log of every query. Written in Python, runs locally with Claude Desktop over stdio.

Ask Claude *"Which country brought in the most paid revenue in March?"* against the demo database and it calls `list_tables`, `describe_table("orders")`, then `run_query` with something like:

```sql
SELECT c.country, sum(o.amount) AS revenue
FROM orders o JOIN customers c ON c.id = o.customer_id
WHERE o.status = 'paid' AND o.created_at >= '2026-03-01' AND o.created_at < '2026-04-01'
GROUP BY 1 ORDER BY 2 DESC
```

which returns `DE 4352.26, US 3500.06, FR 3089.25, IN 2564.44, GB 2529.24`. Ask it for the `api_keys` table and the server refuses, tells Claude why, and logs the attempt.

## What Claude gets

| Tool | What it does |
|---|---|
| `list_tables` | The tables and views this server may read, with approximate row counts and comments, plus the row limit and timeout. |
| `describe_table` | Columns (type, nullable, default, comment), primary key and foreign keys of one allow-listed table. |
| `run_query` | Runs one `SELECT` and returns columns and rows, at most the row limit, with a `truncated` flag. |
| `explain_query` | PostgreSQL's plan for a `SELECT` without running it (`EXPLAIN`, no `ANALYZE`). |

All four are marked `readOnlyHint` so MCP clients know they change nothing.

## How it stays read-only

Three independent walls, so a mistake in one is caught by the next:

1. **The guard** (`src/pg_readonly_mcp/guard.py`) parses each query with [sqlglot](https://github.com/tobymao/sqlglot) and refuses anything that is not exactly one plain read: no `INSERT`/`UPDATE`/`DELETE`/DDL, no `SELECT ... INTO`, no `FOR UPDATE`, no data-modifying CTEs, no semicolon batches. Every table reference is resolved by PostgreSQL itself (`to_regclass` in the same session and `search_path`), so `pg_class` is checked as `pg_catalog.pg_class` and `Orders` as `public.orders`, exactly as the database will read them, and then checked against the allow-list. Functions that read files, run SQL from a string (`query_to_xml`), reach other servers (`dblink`), sleep, change settings or touch sequences are refused, and unknown functions need to be allow-listed. A CTE may not reuse a table's name (that trick is how a query can hide a real table behind an alias). The statement that runs is regenerated from the checked parse tree, so a tokenising quirk in the original text cannot smuggle in anything the guard did not see.
2. **The transaction.** Every call runs in its own `BEGIN READ ONLY` transaction with `statement_timeout` and `lock_timeout`, then rolls back. Rows are read through a server-side cursor, so the row limit holds without PostgreSQL sending the whole result. The server refuses to run as a superuser unless you say so.
3. **The role.** Connect as a role that can only `SELECT` the tables you expose (SQL below). If the role can write to an allow-listed table, the server warns at `list_tables`.

The test suite checks each wall separately, including a test that skips the guard on purpose and confirms PostgreSQL still refuses the write.

## Quick start (about 5 minutes)

You need Python 3.10 or newer and either Docker (for the demo database) or any PostgreSQL 13+.

```bash
git clone https://github.com/vinit1009/pg-readonly-mcp.git
cd pg-readonly-mcp
python3 -m venv .venv
.venv/bin/pip install -e .

# A demo shop database on localhost:54329, with a read-only role mcp_readonly / readonly
docker compose up -d --wait
```

Try it from the terminal before involving Claude. `--check` connects and prints what the server can see; `--query` runs one query through exactly the same guard:

```bash
export MCP_PG_DSN="postgresql://mcp_readonly:readonly@localhost:54329/shop"
export MCP_PG_ALLOWED_TABLES="public.customers, public.orders, public.order_totals, analytics.*"

.venv/bin/pg-readonly-mcp --check
.venv/bin/pg-readonly-mcp --query "SELECT status, count(*) FROM orders GROUP BY 1 ORDER BY 1"
.venv/bin/pg-readonly-mcp --query "SELECT * FROM api_keys"     # refused: not in the allow-list
.venv/bin/pg-readonly-mcp --query "DELETE FROM orders"         # refused: only SELECT
```

### Connect Claude Desktop

1. Open Claude Desktop's config file (Settings → Developer → Edit Config), which lives at
   `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and
   `%APPDATA%\Claude\claude_desktop_config.json` on Windows.
2. Add the server, using the **absolute** path to the script inside your `.venv`
   (`examples/claude_desktop_config.json` has the same block):

   ```json
   {
     "mcpServers": {
       "shop-db": {
         "command": "/ABSOLUTE/PATH/TO/pg-readonly-mcp/.venv/bin/pg-readonly-mcp",
         "env": {
           "MCP_PG_DSN": "postgresql://mcp_readonly:readonly@localhost:54329/shop",
           "MCP_PG_ALLOWED_TABLES": "public.customers, public.orders, public.order_totals, analytics.*"
         }
       }
     }
   }
   ```
   On Windows the command is `C:\\path\\to\\pg-readonly-mcp\\.venv\\Scripts\\pg-readonly-mcp.exe`.
3. Quit and reopen Claude Desktop. The server and its four tools then show up in the chat's tools menu.
4. Ask something: *"Which country brought in the most paid revenue in March?"* or *"Show me the api_keys table"* (Claude will be told it is not allowed).

If the server does not appear, Claude Desktop's MCP log shows why: `~/Library/Logs/Claude/mcp-server-shop-db.log` on macOS. A missing or bad setting is reported there in one line.

## Configuration

Everything is an environment variable, so it fits in Claude Desktop's `env` block.

| Variable | Default | Meaning |
|---|---|---|
| `MCP_PG_DSN` | required | libpq connection string or URL. Prefer `~/.pgpass` or `PGPASSWORD` to a password in the string. |
| `MCP_PG_ALLOWED_TABLES` | required | Comma-separated `schema.table` entries, or `schema.*` for a whole schema. A bare name means `public.name`. Quote mixed-case names: `"Sales"."Q1 Orders"`. |
| `MCP_PG_ROW_LIMIT` | `200` | Most rows one `run_query` returns (hard maximum 10,000). Claude can ask for fewer. |
| `MCP_PG_TIMEOUT_MS` | `10000` | Statement timeout for each query. |
| `MCP_PG_AUDIT_LOG` | `~/.pg-readonly-mcp/audit.jsonl` | Where the audit log goes (created with mode 600). |
| `MCP_PG_SEARCH_PATH` | `public` | Schemas searched for unqualified table names. |
| `MCP_PG_MAX_CELL_CHARS` | `2000` | Long text values are cut to this length so one cell cannot flood the context. |
| `MCP_PG_EXTRA_FUNCTIONS` | empty | Extra function names to allow, e.g. `similarity, unaccent`. |
| `MCP_PG_ALLOW_SUPERUSER` | off | Set to `1` only for a throwaway local database. |

## A read-only role for your own database

```sql
CREATE ROLE mcp_readonly LOGIN PASSWORD 'choose-a-password';
ALTER ROLE mcp_readonly SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE yourdb TO mcp_readonly;
GRANT USAGE ON SCHEMA public TO mcp_readonly;
GRANT SELECT ON public.customers, public.orders TO mcp_readonly;   -- only what Claude should see
```

To hide columns (emails, notes), create a view with the columns you want, grant `SELECT` on the view instead of the table, and allow-list the view. Row-level security policies apply as usual; the server warns if the role bypasses them.

## The audit log

One JSON line per tool call, whatever the outcome: refused queries keep the reason, failed queries keep PostgreSQL's error, and successful ones keep the tables read, row count, whether the result was truncated, the time taken and the exact statement that ran.

```json
{"at": "2026-11-09T04:00:27.835+00:00", "tool": "run_query", "sql": "SELECT c.country, sum(o.amount) AS revenue FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.status = 'paid' AND o.created_at >= '2026-03-01' AND o.created_at < '2026-04-01' GROUP BY 1 ORDER BY 2 DESC", "tables": ["public.customers", "public.orders"], "executed_sql": "SELECT c.country, SUM(o.amount) AS revenue FROM orders AS o JOIN customers AS c ON c.id = o.customer_id WHERE o.status = 'paid' AND o.created_at >= '2026-03-01' AND o.created_at < '2026-04-01' GROUP BY 1 ORDER BY 2 DESC", "status": "ok", "rows": 5, "truncated": false, "ms": 15}
{"at": "2026-11-09T04:00:28.167+00:00", "tool": "run_query", "sql": "SELECT * FROM api_keys", "status": "rejected", "reason": "public.api_keys is not in this server's allow-list. Call list_tables to see what you can query.", "ms": 12}
```

`jq` makes it easy to review: `jq -c 'select(.status != "ok")' ~/.pg-readonly-mcp/audit.jsonl`.

## Tests

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

The tests start a throwaway PostgreSQL cluster with the local `initdb` and `pg_ctl`, or use `TEST_PG_ADMIN_DSN` (a superuser connection string) when it is set, as in CI. They load `examples/seed.sql`, then cover:

- queries that must work: joins, CTEs, window functions, `FILTER`, `percentile_cont`, `DISTINCT ON`, `LATERAL`, JSON operators, views, materialized views, `schema.*` entries, identifier case folding;
- 35 queries that must be refused, from plain `DELETE` to the less obvious ones: a CTE named after the hidden table, a backslash-quote string trick, unqualified `pg_class`, `query_to_xml`, `pg_read_file`, `set_config`, a `DELETE` inside a CTE, `FOR UPDATE`, `SELECT INTO`, semicolon batches;
- the row limit, the statement timeout (and that the session recovers), JSON-safe cell values and clipping;
- the database wall on its own: a superuser is refused, and a role that can write is warned about and still cannot write;
- the audit log for ok, refused and failed calls;
- a full MCP session over stdio, the transport Claude Desktop uses.

## Limits

- One database per server process. For two databases, add two entries to the Claude Desktop config.
- The guard decides what is *read*; the database role decides what *can* be read. Keep the role narrow and treat the allow-list as the second lock, not the only one.
- Column-level rules are done with views (above), not in the server.

## Licence

MIT
