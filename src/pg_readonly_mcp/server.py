"""MCP server (stdio) exposing four read-only tools over one PostgreSQL database.

Run it from Claude Desktop (see the README), or check a setup from the terminal:
    pg-readonly-mcp --check
    pg-readonly-mcp --query "SELECT count(*) FROM orders"
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import __version__, guard
from .config import Config, ConfigError
from .database import Database, QueryError

INSTRUCTIONS = """Read-only access to one PostgreSQL database.
Start with list_tables, then describe_table for the columns you need. run_query takes exactly one
SELECT (CTEs, joins, window functions and aggregates are fine) and returns at most the server's row
limit, so aggregate or filter in SQL instead of pulling raw rows. Only allow-listed tables can be read;
writes, multiple statements and functions that touch files or settings are refused, and every call is
written to an audit log. If a query is refused, the error says why: fix the query rather than retrying."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def build_server(cfg: Config, db: Optional[Database] = None) -> MCPServer:
    db = db or Database(cfg)
    server = MCPServer(name="pg-readonly", title="PostgreSQL (read-only)", instructions=INSTRUCTIONS, version=__version__)

    def call(fn, *args) -> dict[str, Any]:
        try:
            return fn(*args)
        except (guard.Rejected, QueryError) as e:
            raise ToolError(str(e)) from e

    @server.tool(annotations=READ_ONLY)
    def list_tables() -> dict[str, Any]:
        """List the tables and views this server may read, with approximate row counts and comments,
        plus the row limit and timeout that apply to run_query."""
        return call(db.list_tables)

    @server.tool(annotations=READ_ONLY)
    def describe_table(table: str) -> dict[str, Any]:
        """Show the columns (name, type, nullable, default, comment), primary key and foreign keys of
        one allow-listed table or view. Pass schema.table, or just the table name for the default schema."""
        return call(db.describe_table, table)

    @server.tool(annotations=READ_ONLY)
    def run_query(sql: str, limit: Optional[int] = None) -> dict[str, Any]:
        """Run one read-only SQL SELECT (PostgreSQL dialect) and return columns and rows.
        At most `limit` rows come back (capped by the server's row limit); `truncated` says whether more
        rows matched. Writes, multiple statements and non-allow-listed tables are refused."""
        return call(db.run_query, sql, limit)

    @server.tool(annotations=READ_ONLY)
    def explain_query(sql: str) -> dict[str, Any]:
        """Show PostgreSQL's query plan for one SELECT without running it (EXPLAIN, no ANALYZE).
        Use it before a query that might be slow on a large table."""
        return call(db.explain_query, sql)

    return server


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="pg-readonly-mcp", description="Read-only PostgreSQL MCP server (stdio).")
    p.add_argument("--check", action="store_true", help="connect, print what the server can see, and exit")
    p.add_argument("--query", metavar="SQL", help="run one query through the same guard and print the result as JSON")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="pg-readonly-mcp %(levelname)s %(message)s")
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"pg-readonly-mcp: {e}", file=sys.stderr)
        return 2
    if args.check or args.query:
        db = Database(cfg)
        try:
            result = db.run_query(args.query) if args.query else {"audit_log": str(cfg.audit_log), **db.list_tables()}
        except (guard.Rejected, QueryError) as e:
            print(f"pg-readonly-mcp: {e}", file=sys.stderr)
            return 1
        finally:
            db.close()
        print(json.dumps(result, indent=2, default=str))
        return 0
    build_server(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
