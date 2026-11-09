"""PostgreSQL access: every call runs in its own READ ONLY transaction with a statement timeout,
reads through a server-side cursor so the row limit holds without loading the whole result,
and is written to the audit log whatever the outcome."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

import psycopg
from psycopg import errors
from sqlglot import exp
from sqlglot.errors import SqlglotError

from . import guard
from .audit import Audit
from .config import Config

log = logging.getLogger("pg_readonly_mcp")


class QueryError(Exception):
    """The database refused or failed the query; the message is safe to show the model."""


class Database:
    def __init__(self, cfg: Config, audit: Optional[Audit] = None):
        self.cfg = cfg
        self.audit = audit or Audit(cfg.audit_log)
        self._conn: Optional[psycopg.Connection] = None
        self._lock = threading.Lock()
        self.warnings: list[str] = []

    # ---------- connection ----------
    def _connection(self) -> psycopg.Connection:
        if self._conn is not None and not self._conn.closed and not self._conn.broken:
            return self._conn
        try:
            conn = psycopg.connect(self.cfg.dsn, autocommit=False, application_name="pg-readonly-mcp", connect_timeout=10)
        except psycopg.Error as e:
            raise QueryError(f"Could not connect to PostgreSQL: {_message(e)}") from e
        conn.read_only = True  # every transaction psycopg opens is BEGIN READ ONLY
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('search_path', %s, false), set_config('default_transaction_read_only', 'on', false)",
                            [self.cfg.search_path])
                cur.execute("SELECT rolsuper, rolbypassrls, current_user FROM pg_roles WHERE rolname = current_user")
                row = cur.fetchone()
            conn.commit()
        except psycopg.Error as e:
            conn.close()
            raise QueryError(f"Could not prepare the session: {_message(e)}") from e
        superuser, bypassrls, user = row if row else (False, False, "?")
        if superuser and not self.cfg.allow_superuser:
            conn.close()
            raise QueryError(f"Refusing to run as superuser {user!r}. Connect as a role that can only SELECT "
                             "(see the README), or set MCP_PG_ALLOW_SUPERUSER=1 for a throwaway local database.")
        self.warnings = []
        if superuser:
            self.warnings.append(f"Connected as superuser {user!r}; only the read-only transaction and the guard stand in the way of writes.")
        if bypassrls:
            self.warnings.append(f"Role {user!r} bypasses row-level security.")
        self._conn = conn
        writable = [t["table"] for t in self._allowed_relations() if t.get("writable")]
        if writable:
            self.warnings.append("The role can also write to: " + ", ".join(writable) +
                                 ". The server never writes, but a SELECT-only role is the safer setup.")
        for w in self.warnings:
            log.warning(w)
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _resolver(self, cur: psycopg.Cursor):
        def resolve(schema: Optional[str], name: str) -> Optional[guard.Relation]:
            text = f"{guard.quote_ident(schema)}.{guard.quote_ident(name)}" if schema else guard.quote_ident(name)
            cur.execute("SELECT c.oid, n.nspname, c.relname, c.relkind::text FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = to_regclass(%s)", [text])
            row = cur.fetchone()
            return guard.Relation(schema=row[1], name=row[2], kind=row[3], oid=row[0]) if row else None
        return resolve

    def _begin(self, cur: psycopg.Cursor) -> None:
        t = str(self.cfg.timeout_ms)
        cur.execute("SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true), "
                    "set_config('idle_in_transaction_session_timeout', %s, true)", [t, t, str(self.cfg.timeout_ms * 3)])

    # ---------- tools ----------
    def list_tables(self) -> dict:
        start = time.monotonic()
        with self._lock:
            try:
                rels = self._allowed_relations()
            except QueryError as e:
                self.audit.write(tool="list_tables", status="error", error=str(e))
                raise
            finally:
                self._rollback()
        tables = [{k: v for k, v in r.items() if k != "writable"} for r in rels]
        self.audit.write(tool="list_tables", status="ok", rows=len(tables), ms=_ms(start))
        out = {"tables": tables, "row_limit": self.cfg.row_limit, "timeout_ms": self.cfg.timeout_ms,
               "allow_list": self.cfg.allowed.describe()}
        if self.warnings:
            out["warnings"] = self.warnings
        return out

    def _allowed_relations(self) -> list[dict]:
        conn = self._connection()
        try:
            with conn.cursor() as cur:
                self._begin(cur)
                cur.execute("""
                    SELECT n.nspname, c.relname, c.relkind::text,
                           CASE WHEN c.relkind IN ('r','p','m') AND c.reltuples >= 0 THEN c.reltuples::bigint END,
                           obj_description(c.oid, 'pg_class'),
                           has_table_privilege(c.oid, 'INSERT') OR has_table_privilege(c.oid, 'UPDATE')
                             OR has_table_privilege(c.oid, 'DELETE') OR has_table_privilege(c.oid, 'TRUNCATE')
                    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r','p','v','m','f') AND has_table_privilege(c.oid, 'SELECT')
                      AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%%'
                    ORDER BY 1, 2""")
                rows = cur.fetchall()
        except psycopg.Error as e:
            raise QueryError(_message(e)) from e
        out = []
        for schema, name, kind, approx, comment, writable in rows:
            if self.cfg.allowed.allows(schema, name):
                item = {"table": f"{schema}.{name}", "kind": guard.SELECTABLE_KINDS.get(kind, kind), "approx_rows": approx,
                        "comment": comment, "writable": writable}
                out.append({k: v for k, v in item.items() if v is not None})
        return sorted(out, key=lambda t: t["table"])

    def describe_table(self, table: str) -> dict:
        start = time.monotonic()
        with self._lock:
            try:
                conn = self._connection()
                with conn.cursor() as cur:
                    self._begin(cur)
                    rel = self._resolve_name(cur, table)
                    cur.execute("""
                        SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
                               pg_get_expr(d.adbin, d.adrelid), col_description(a.attrelid, a.attnum)
                        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                        WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""", [rel.oid])
                    columns = []
                    for name, typ, nullable, default, comment in cur.fetchall():
                        col = {"name": name, "type": typ, "nullable": nullable, "default": default, "comment": comment}
                        columns.append({k: v for k, v in col.items() if v is not None})
                    cur.execute("""
                        SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_index i
                        CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
                        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                        WHERE i.indrelid = %s AND i.indisprimary""", [rel.oid])
                    pk = (cur.fetchone() or [None])[0]
                    cur.execute("""
                        SELECT pg_get_constraintdef(k.oid), n.nspname, c.relname FROM pg_constraint k
                        JOIN pg_class c ON c.oid = k.confrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE k.conrelid = %s AND k.contype = 'f' ORDER BY k.conname""", [rel.oid])
                    fks = [d for d, s, n in cur.fetchall() if self.cfg.allowed.allows(s, n)]
                    cur.execute("SELECT obj_description(%s, 'pg_class')", [rel.oid])
                    comment = cur.fetchone()[0]
            except guard.Rejected as e:
                self.audit.write(tool="describe_table", table=table, status="rejected", reason=str(e))
                raise
            except psycopg.Error as e:
                self.audit.write(tool="describe_table", table=table, status="error", error=_message(e))
                raise QueryError(_message(e)) from e
            finally:
                self._rollback()
        self.audit.write(tool="describe_table", table=str(rel), status="ok", ms=_ms(start))
        out = {"table": str(rel), "kind": guard.SELECTABLE_KINDS.get(rel.kind, rel.kind), "comment": comment,
               "columns": columns, "primary_key": list(pk) if pk else None, "foreign_keys": fks or None}
        return {k: v for k, v in out.items() if v is not None}

    def _resolve_name(self, cur: psycopg.Cursor, table: str) -> guard.Relation:
        try:
            t = exp.to_table(table.strip(), dialect="postgres")
        except SqlglotError as e:
            raise guard.Rejected(f"{table!r} is not a table name.") from e
        schema = guard.fold(t.args["db"]) if t.args.get("db") else None
        rel = self._resolver(cur)(schema, guard.fold(t.this))
        if rel is None or rel.kind not in guard.SELECTABLE_KINDS:
            raise guard.Rejected(f"There is no table or view called {table}. Call list_tables to see what you can query.")
        if not self.cfg.allowed.allows(rel.schema, rel.name):
            raise guard.Rejected(f"{rel} is not in this server's allow-list. Call list_tables to see what you can query.")
        return rel

    def run_query(self, sql: str, limit: Optional[int] = None) -> dict:
        start = time.monotonic()
        limit = max(1, min(limit or self.cfg.row_limit, self.cfg.row_limit))
        entry: dict[str, Any] = {"tool": "run_query", "sql": sql}
        with self._lock:
            try:
                conn = self._connection()
                with conn.cursor() as cur:
                    self._begin(cur)
                    checked = guard.check(sql, self._resolver(cur), self.cfg.allowed, self.cfg.extra_functions)
                entry.update(tables=checked.tables, executed_sql=checked.sql)
                with conn.cursor(name=f"pgro_{uuid.uuid4().hex[:12]}") as cur:
                    cur.execute(checked.sql)
                    rows = cur.fetchmany(limit + 1)
                    columns = [d.name for d in (cur.description or [])]
            except guard.Rejected as e:
                self.audit.write(**entry, status="rejected", reason=str(e), ms=_ms(start))
                raise
            except psycopg.Error as e:
                msg = self._explain_error(e)
                self.audit.write(**entry, status="error", error=msg, ms=_ms(start))
                raise QueryError(msg) from e
            finally:
                self._rollback()
        truncated = len(rows) > limit
        rows = rows[:limit]
        self.audit.write(**entry, status="ok", rows=len(rows), truncated=truncated, ms=_ms(start))
        out = {"columns": columns, "rows": [[self._cell(v) for v in r] for r in rows], "row_count": len(rows),
               "truncated": truncated, "tables": checked.tables, "elapsed_ms": _ms(start)}
        if truncated:
            out["note"] = f"Only the first {limit} rows are shown. Aggregate, filter or add ORDER BY ... LIMIT to see the rows that matter."
        return out

    def explain_query(self, sql: str) -> dict:
        start = time.monotonic()
        entry: dict[str, Any] = {"tool": "explain_query", "sql": sql}
        with self._lock:
            try:
                conn = self._connection()
                with conn.cursor() as cur:
                    self._begin(cur)
                    checked = guard.check(sql, self._resolver(cur), self.cfg.allowed, self.cfg.extra_functions)
                    entry["tables"] = checked.tables
                    cur.execute("EXPLAIN (FORMAT TEXT) " + checked.sql)
                    plan = "\n".join(r[0] for r in cur.fetchall())
            except guard.Rejected as e:
                self.audit.write(**entry, status="rejected", reason=str(e), ms=_ms(start))
                raise
            except psycopg.Error as e:
                msg = self._explain_error(e)
                self.audit.write(**entry, status="error", error=msg, ms=_ms(start))
                raise QueryError(msg) from e
            finally:
                self._rollback()
        self.audit.write(**entry, status="ok", ms=_ms(start))
        return {"plan": plan[:20_000], "tables": checked.tables}

    # ---------- helpers ----------
    def _rollback(self) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            conn.rollback()
        except psycopg.Error:
            conn.close()
            self._conn = None

    def _explain_error(self, e: psycopg.Error) -> str:
        if isinstance(e, errors.QueryCanceled):
            return (f"The query was stopped after {self.cfg.timeout_ms} ms (statement timeout). "
                    "Filter on indexed columns, aggregate, or check the plan with explain_query.")
        if isinstance(e, errors.ReadOnlySqlTransaction):
            return "The database refused a write: this server only reads."
        if isinstance(e, psycopg.OperationalError):
            self.close()
            return f"Lost the connection to PostgreSQL: {_message(e)}"
        return f"PostgreSQL error: {_message(e)}"

    def _cell(self, v: Any) -> Any:
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, str):
            return _clip(v, self.cfg.max_cell_chars)
        if isinstance(v, decimal.Decimal):
            # Whole numbers (sum of an integer column, counts) come back as numbers; values with a scale,
            # such as numeric(10,2) money, stay exact strings so 42.10 is not turned into 42.1.
            return int(v) if v.is_finite() and v.as_tuple().exponent >= 0 else str(v)
        if isinstance(v, (dt.datetime, dt.date, dt.time)):
            return v.isoformat()
        if isinstance(v, (bytes, bytearray, memoryview)):
            b = bytes(v)
            return f"<{len(b)} bytes>" if len(b) > 64 else "base64:" + base64.b64encode(b).decode()
        if isinstance(v, (list, tuple)):
            return [self._cell(x) for x in v]
        if isinstance(v, dict):
            text = json.dumps(v, default=str, ensure_ascii=False)
            return v if len(text) <= self.cfg.max_cell_chars else _clip(text, self.cfg.max_cell_chars)
        return _clip(str(v), self.cfg.max_cell_chars)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"... [{len(s) - n} more characters]"


def _message(e: BaseException) -> str:
    diag = getattr(e, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag else None
    hint = getattr(diag, "message_hint", None) if diag else None
    raw = str(e).strip()
    text = primary or (raw.splitlines()[0] if raw else type(e).__name__)
    return f"{text} (hint: {hint})" if hint else text


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
