"""Append-only audit log: one JSON line per tool call, including refused and failed queries."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("pg_readonly_mcp")


class Audit:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, **fields) -> dict:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **fields}
        line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        summary = entry.get("reason") or entry.get("error") or f"{entry.get('rows', '')} rows".strip()
        log.info("%s %s %s", entry.get("tool"), entry.get("status"), summary)
        return entry
