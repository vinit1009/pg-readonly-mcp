"""Decides whether one SQL statement may run: a single read-only query over allow-listed tables.

The guard parses the query with sqlglot (PostgreSQL dialect), refuses anything that is not a plain
read, resolves every table reference through the database's own search_path (so `pg_class` means
`pg_catalog.pg_class`, exactly as PostgreSQL would read it), and checks the result against the
allow-list. The statement that runs is regenerated from the checked tree, so a quirk in how the
original text is tokenised cannot smuggle in anything the guard did not see.

The guard is one of three layers. The others are a READ ONLY transaction with a statement timeout,
and a database role that can only SELECT (see the README).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


class Rejected(Exception):
    """The query is not allowed. The message is written for the model, so it can fix the query."""


@dataclass(frozen=True)
class Relation:
    schema: str
    name: str
    kind: str  # pg_class.relkind
    oid: int = 0

    @property
    def qualified(self) -> str:
        return f"{quote_ident(self.schema)}.{quote_ident(self.name)}"

    def __str__(self) -> str:
        return f"{self.schema}.{self.name}"


# (schema or None, name) -> the relation PostgreSQL would read, or None if there is none.
Resolver = Callable[[Optional[str], str], Optional[Relation]]

SELECTABLE_KINDS = {"r": "table", "p": "table", "v": "view", "m": "materialized view", "f": "foreign table"}

ALLOWED_ROOTS = (exp.Select, exp.SetOperation, exp.Values, exp.Subquery)

_FORBIDDEN_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command", "Into", "Lock", "Set",
    "Transaction", "Commit", "Rollback", "Copy", "Grant", "Revoke", "TruncateTable", "Analyze", "Use",
    "LoadData", "Pragma", "Cache", "Uncache", "Refresh", "Kill", "Execute",
)
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))

# Functions that read files, run SQL from a string, reach other servers, sleep, change settings,
# or touch sequences and locks. Checked against every function name, known to sqlglot or not.
DENIED_PREFIXES = (
    "pg_read", "pg_ls_", "pg_stat_file", "pg_file", "pg_log", "lo_", "dblink", "query_to_", "table_to_",
    "cursor_to_", "schema_to_", "database_to_", "set_config", "pg_sleep", "pg_terminate", "pg_cancel",
    "pg_reload", "pg_rotate", "pg_promote", "pg_switch", "pg_create", "pg_drop", "pg_replication",
    "pg_logical", "pg_advisory", "pg_try_advisory", "txid_", "pg_current_xact", "nextval", "setval",
    "currval", "lastval", "pg_stat_reset", "pg_import", "pg_export", "pg_backup", "pg_wal",
)

# Ordinary PostgreSQL functions that sqlglot does not model as typed expressions. Functions sqlglot
# does model (COUNT, SUM, DATE_TRUNC, COALESCE, ...) are allowed without being listed here.
SAFE_FUNCTIONS = frozenset("""
    age date_part date_bin make_date make_time make_timestamp make_timestamptz make_interval justify_days
    justify_hours justify_interval to_char to_date to_timestamp to_number isfinite timezone clock_timestamp
    statement_timestamp transaction_timestamp
    initcap lpad rpad btrim ltrim rtrim split_part strpos left right reverse repeat translate format
    regexp_replace regexp_match regexp_matches regexp_count regexp_instr regexp_like regexp_substr
    regexp_split_to_array regexp_split_to_table string_to_array string_to_table array_to_string md5
    char_length character_length octet_length bit_length chr ascii starts_with concat_ws overlay
    quote_literal quote_ident quote_nullable encode decode to_hex
    width_bucket div mod power sqrt cbrt ln log log10 exp sign trunc pi degrees radians gcd lcm factorial
    scale min_scale trim_scale random
    array_length cardinality array_position array_positions array_remove array_replace array_append
    array_prepend array_cat array_upper array_lower array_ndims array_dims generate_subscripts
    json_agg jsonb_agg json_object_agg jsonb_object_agg json_build_object jsonb_build_object
    json_build_array jsonb_build_array to_json to_jsonb row_to_json array_to_json json_each jsonb_each
    json_each_text jsonb_each_text json_array_elements jsonb_array_elements json_array_elements_text
    jsonb_array_elements_text json_object_keys jsonb_object_keys json_typeof jsonb_typeof
    json_array_length jsonb_array_length json_extract_path json_extract_path_text jsonb_extract_path
    jsonb_extract_path_text json_to_record jsonb_to_record json_to_recordset jsonb_to_recordset
    json_populate_record jsonb_populate_record jsonb_path_query jsonb_path_query_array
    jsonb_path_query_first jsonb_path_exists jsonb_strip_nulls jsonb_pretty jsonb_set jsonb_insert
    mode percentile_disc stddev_pop stddev_samp var_pop var_samp corr covar_pop covar_samp regr_slope
    regr_intercept regr_r2 regr_count bool_and bool_or every bit_and bit_or string_agg array_agg
    rank dense_rank ntile cume_dist percent_rank lag lead first_value last_value nth_value
    pg_typeof format_type num_nulls num_nonnulls
""".split())


@dataclass(frozen=True)
class AllowList:
    """Tables the server may read: exact `schema.table` entries and whole schemas (`schema.*`)."""

    tables: frozenset = field(default_factory=frozenset)  # {(schema, name)}
    schemas: frozenset = field(default_factory=frozenset)

    @classmethod
    def parse(cls, text: str) -> "AllowList":
        tables, schemas = set(), set()
        for raw in re.split(r"[,\s]+", text or ""):
            entry = raw.strip()
            if not entry:
                continue
            if entry.endswith(".*"):
                schemas.add(_fold_text(entry[:-2]))
                continue
            try:
                t = exp.to_table(entry, dialect="postgres")
            except SqlglotError as e:
                raise ValueError(f"Bad allow-list entry {entry!r}: {e}") from e
            if t.args.get("catalog"):
                raise ValueError(f"Bad allow-list entry {entry!r}: use schema.table")
            schema = fold(t.args["db"]) if t.args.get("db") else "public"
            tables.add((schema, fold(t.this)))
        if not tables and not schemas:
            raise ValueError("The allow-list is empty. List tables as schema.table or whole schemas as schema.*")
        return cls(frozenset(tables), frozenset(schemas))

    def allows(self, schema: str, name: str) -> bool:
        return (schema, name) in self.tables or schema in self.schemas

    def describe(self) -> list[str]:
        return sorted([f"{s}.*" for s in self.schemas] + [f"{s}.{n}" for s, n in self.tables])


@dataclass
class Checked:
    sql: str  # the statement that will run, regenerated from the checked tree
    relations: list = field(default_factory=list)  # [Relation] the query reads

    @property
    def tables(self) -> list[str]:
        return sorted({str(r) for r in self.relations})


def fold(ident: exp.Identifier) -> str:
    """PostgreSQL folds unquoted identifiers to lower case and keeps quoted ones as written."""
    return ident.this if ident.quoted else ident.this.lower()


def _fold_text(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] == '"':
        return name[1:-1].replace('""', '"')
    return name.lower()


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def check(sql: str, resolve: Resolver, allowed: AllowList, extra_functions: frozenset = frozenset(),
          max_length: int = 20_000) -> Checked:
    """Returns the statement to run, or raises Rejected with a reason the model can act on."""
    if not sql or not sql.strip():
        raise Rejected("The query is empty.")
    if len(sql) > max_length:
        raise Rejected(f"The query is {len(sql)} characters; the limit is {max_length}.")
    try:
        trees = [t for t in sqlglot.parse(sql, read="postgres") if t is not None]
    except SqlglotError as e:
        first = str(e).splitlines()[0][:200]
        raise Rejected(f"Could not parse the query ({first}). Send one plain PostgreSQL SELECT.") from e
    if len(trees) != 1:
        raise Rejected("Send exactly one statement; batches separated by semicolons are not allowed.")
    tree = trees[0]
    if not isinstance(tree, ALLOWED_ROOTS):
        raise Rejected(f"Only SELECT queries are allowed (this is {tree.key.upper()}).")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            what = {"into": "SELECT ... INTO", "lock": "FOR UPDATE / FOR SHARE locks"}.get(node.key, node.key.upper())
            raise Rejected(f"{what} is not allowed: this server only reads.")

    _check_functions(tree, extra_functions)

    ctes = {}
    for cte in tree.find_all(exp.CTE):
        alias = cte.args.get("alias")
        if alias is not None and isinstance(alias.this, exp.Identifier):
            ctes[fold(alias.this)] = cte
    for name in ctes:
        if resolve(None, name) is not None:
            raise Rejected(f"The CTE name {name!r} is also the name of a table. Rename the CTE.")

    relations: list[Relation] = []
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue  # a function in FROM, such as generate_series(); checked with the other functions
        if table.args.get("catalog"):
            raise Rejected(f"Use schema.table names, not database.schema.table ({table.sql(dialect='postgres')}).")
        name = fold(table.this)
        schema = fold(table.args["db"]) if table.args.get("db") else None
        if schema is None and name in ctes:
            continue
        rel = resolve(schema, name)
        shown = f"{schema}.{name}" if schema else name
        if rel is None:
            raise Rejected(f"There is no table or view called {shown}. Call list_tables to see what you can query.")
        if rel.kind not in SELECTABLE_KINDS:
            raise Rejected(f"{rel} is not a table or view.")
        if not allowed.allows(rel.schema, rel.name):
            raise Rejected(f"{rel} is not in this server's allow-list. Call list_tables to see what you can query.")
        relations.append(rel)

    return Checked(sql=tree.sql(dialect="postgres", comments=False), relations=relations)


def _check_functions(tree: exp.Expression, extra: frozenset) -> None:
    for f in tree.find_all(exp.Func):
        name = (f.name if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
        if name.startswith(DENIED_PREFIXES):
            raise Rejected(f"The function {name}() is not allowed on this server.")
        parent = f.parent
        if isinstance(parent, exp.Dot) and parent.expression is f:
            raise Rejected(f"Call {name}() without a schema prefix.")
        if isinstance(parent, exp.Table) and parent.this is f and parent.args.get("db"):
            raise Rejected(f"Call {name}() without a schema prefix.")
        if isinstance(f, exp.Anonymous) and name not in SAFE_FUNCTIONS and name not in extra:
            raise Rejected(f"The function {name}() is not on this server's list of allowed functions. "
                           "Use standard SQL functions, or ask the admin to add it to MCP_PG_EXTRA_FUNCTIONS.")
