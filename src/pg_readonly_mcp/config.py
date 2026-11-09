"""Settings, read from environment variables so Claude Desktop's `env` block can carry them."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .guard import AllowList

HARD_ROW_LIMIT = 10_000


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    dsn: str
    allowed: AllowList
    row_limit: int = 200
    timeout_ms: int = 10_000
    audit_log: Path = Path("~/.pg-readonly-mcp/audit.jsonl").expanduser()
    search_path: str = "public"
    max_cell_chars: int = 2_000
    extra_functions: frozenset = field(default_factory=frozenset)
    allow_superuser: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        dsn = env.get("MCP_PG_DSN", "").strip()
        if not dsn:
            raise ConfigError("Set MCP_PG_DSN to a PostgreSQL connection string, e.g. "
                              "postgresql://mcp_readonly@localhost:5432/shop")
        try:
            allowed = AllowList.parse(env.get("MCP_PG_ALLOWED_TABLES", ""))
        except ValueError as e:
            raise ConfigError(f"MCP_PG_ALLOWED_TABLES: {e}") from e
        return cls(
            dsn=dsn,
            allowed=allowed,
            row_limit=_int(env, "MCP_PG_ROW_LIMIT", 200, 1, HARD_ROW_LIMIT),
            timeout_ms=_int(env, "MCP_PG_TIMEOUT_MS", 10_000, 100, 600_000),
            audit_log=Path(env.get("MCP_PG_AUDIT_LOG") or "~/.pg-readonly-mcp/audit.jsonl").expanduser(),
            search_path=env.get("MCP_PG_SEARCH_PATH", "public").strip() or "public",
            max_cell_chars=_int(env, "MCP_PG_MAX_CELL_CHARS", 2_000, 20, 1_000_000),
            extra_functions=frozenset(x.lower() for x in re.split(r"[,\s]+", env.get("MCP_PG_EXTRA_FUNCTIONS", "")) if x),
            allow_superuser=env.get("MCP_PG_ALLOW_SUPERUSER", "").strip().lower() in ("1", "true", "yes"),
        )


def _int(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{key} must be a whole number, got {raw!r}") from e
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}, got {value}")
    return value
