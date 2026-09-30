"""Trial registry (``registry/runs.db``, versioned with git).

Milestone 1 only holds the period-access journal; the runs table arrives with
milestone 4. The database is append-only by design: nothing here deletes rows.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS period_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    symbol TEXT NOT NULL,
    period TEXT NOT NULL,
    requested_start TEXT,
    requested_end TEXT,
    status TEXT NOT NULL CHECK (status IN ('refused', 'granted')),
    reason TEXT,
    token TEXT,
    strategy_hash TEXT,
    family_hash TEXT,
    caller TEXT
);
CREATE INDEX IF NOT EXISTS ix_period_access_token ON period_access (token);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Registry:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        return conn

    def log_period_access(
        self,
        *,
        symbol: str,
        period: str,
        start: str | None,
        end: str | None,
        status: str,
        reason: str | None,
        token: str | None,
        caller: str = "",
        strategy_hash: str | None = None,
        family_hash: str | None = None,
    ) -> int:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO period_access (ts_utc, symbol, period, requested_start, requested_end,"
                " status, reason, token, strategy_hash, family_hash, caller)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    symbol,
                    period,
                    start,
                    end,
                    status,
                    reason,
                    token,
                    strategy_hash,
                    family_hash,
                    caller,
                ),
            )
            return int(cur.lastrowid)

    def record_grant(
        self,
        *,
        symbol: str,
        period: str,
        reason: str,
        strategy_hash: str,
        family_hash: str,
        caller: str = "",
    ) -> str:
        """Journal a granted access and return its token (used by milestone 7)."""
        token = uuid.uuid4().hex
        self.log_period_access(
            symbol=symbol,
            period=period,
            start=None,
            end=None,
            status="granted",
            reason=reason,
            token=token,
            caller=caller,
            strategy_hash=strategy_hash,
            family_hash=family_hash,
        )
        return token

    def find_grant(self, token: str) -> dict | None:
        if not self.db_path.exists():
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM period_access WHERE token = ? AND status = 'granted'", (token,)
            ).fetchone()
        return dict(row) if row else None

    def period_access_log(self, limit: int = 200) -> list[dict]:
        if not self.db_path.exists():
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM period_access ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
