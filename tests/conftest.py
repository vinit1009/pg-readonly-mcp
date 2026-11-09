"""Test databases. Uses TEST_PG_ADMIN_DSN (a superuser connection string) when it is set, as in CI;
otherwise starts a throwaway PostgreSQL cluster with the local initdb and pg_ctl."""
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from pg_readonly_mcp.config import Config
from pg_readonly_mcp.database import Database

SEED = Path(__file__).resolve().parents[1] / "examples" / "seed.sql"
ALLOWED = "public.customers, public.orders, public.order_totals, analytics.*"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def admin_dsn():
    if os.environ.get("TEST_PG_ADMIN_DSN"):
        yield os.environ["TEST_PG_ADMIN_DSN"]
        return
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not (initdb and pg_ctl):
        pytest.skip("PostgreSQL server binaries not found; set TEST_PG_ADMIN_DSN to a superuser connection string")
    base = Path(tempfile.mkdtemp(prefix="pgro-"))
    data = base / "data"
    subprocess.run([initdb, "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8", "--no-locale"],
                   check=True, capture_output=True)
    port = _free_port()
    subprocess.run([pg_ctl, "-D", str(data), "-l", str(base / "server.log"), "-w",
                    "-o", f"-p {port} -k {base} -c listen_addresses=''", "start"], check=True, capture_output=True)
    try:
        yield make_conninfo(host=str(base), port=port, user="postgres", dbname="postgres")
    finally:
        subprocess.run([pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"], capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="session")
def shop(admin_dsn):
    name = "pgro_test_" + uuid.uuid4().hex[:8]
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    admin = make_conninfo(admin_dsn, dbname=name)
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(SEED.read_text())
        c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp_writer') "
                  "THEN CREATE ROLE mcp_writer LOGIN PASSWORD 'writer'; END IF; END $$")
        c.execute("GRANT USAGE ON SCHEMA public TO mcp_writer")
        c.execute("GRANT SELECT, INSERT ON public.orders TO mcp_writer")
    yield {
        "admin": admin,
        "readonly": make_conninfo(admin, user="mcp_readonly", password="readonly"),
        "writer": make_conninfo(admin, user="mcp_writer", password="writer"),
    }
    with psycopg.connect(admin_dsn, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def make_cfg(shop, tmp_path):
    def make(**overrides) -> Config:
        env = {"MCP_PG_DSN": shop["readonly"], "MCP_PG_ALLOWED_TABLES": ALLOWED, "MCP_PG_ROW_LIMIT": "50",
               "MCP_PG_TIMEOUT_MS": "1500", "MCP_PG_AUDIT_LOG": str(tmp_path / "audit.jsonl")}
        env.update(overrides)
        return Config.from_env(env)
    return make


@pytest.fixture
def db(make_cfg):
    d = Database(make_cfg())
    yield d
    d.close()
