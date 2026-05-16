"""SQLite persistence layer for the signl Kalshi trading bot.

All write functions return ``(ok: bool, message: str)`` so callers (and
ultimately OpenClaw / Telegram) can surface a human-readable result without
catching exceptions. Read functions return plain Python data structures and
swallow errors into an empty result with a logged warning.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
except ImportError:
    pass

log = logging.getLogger("signl.db")

DEFAULT_DB_PATH = str(
    Path(__file__).resolve().parent / "signl.db"
)


def _db_path() -> str:
    """Return the active SQLite path (env override or default)."""
    return os.environ.get("SIGNL_DB_PATH", DEFAULT_DB_PATH)


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    """Yield a sqlite3 connection with foreign keys and Row factory enabled."""
    conn = sqlite3.connect(_db_path(), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker      TEXT NOT NULL UNIQUE,
        mode        TEXT NOT NULL CHECK (mode IN ('auto','manual')),
        added_at    TEXT NOT NULL,
        last_checked TEXT,
        is_active   INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker        TEXT NOT NULL,
        side          TEXT NOT NULL CHECK (side IN ('yes','no')),
        num_contracts INTEGER NOT NULL,
        entry_price   INTEGER NOT NULL,
        current_price INTEGER,
        status        TEXT NOT NULL CHECK (status IN ('open','closed')),
        opened_at     TEXT NOT NULL,
        closed_at     TEXT,
        pnl           INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker          TEXT NOT NULL,
        action          TEXT NOT NULL CHECK (action IN ('buy','sell')),
        side            TEXT NOT NULL CHECK (side IN ('yes','no')),
        contracts       INTEGER NOT NULL,
        price           INTEGER NOT NULL,
        reason          TEXT,
        sentiment_score REAL,
        confidence      TEXT,
        timestamp       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker      TEXT NOT NULL,
        alert_type  TEXT NOT NULL CHECK (alert_type IN ('price_above','price_below')),
        threshold   INTEGER NOT NULL,
        triggered   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_watchlist_ticker ON watchlist(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_trade_log_ticker ON trade_log(ticker)",
    "CREATE INDEX IF NOT EXISTS idx_alerts_lookup ON alerts(ticker, triggered)",
)


def init_db() -> tuple[bool, str]:
    """Create every table and index if missing. Idempotent."""
    try:
        with _connect() as conn:
            for stmt in SCHEMA:
                conn.execute(stmt)
        return True, f"db ready at {_db_path()}"
    except sqlite3.Error as exc:
        return False, f"failed to init db: {exc}"


def _ensure_schema() -> None:
    """Internal helper: make sure tables exist before any read/write."""
    init_db()


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


def add_to_watchlist(ticker: str, mode: str) -> tuple[bool, str]:
    """Add a ticker (or reactivate it) with the given mode.

    Returns ``(True, "...added")`` for a fresh insert,
    ``(True, "...updated")`` if the ticker already existed.
    """
    ticker = ticker.strip().upper()
    mode = mode.strip().lower()
    if mode not in ("auto", "manual"):
        return False, f"invalid mode '{mode}', must be auto or manual"
    if not ticker:
        return False, "ticker cannot be empty"
    _ensure_schema()
    try:
        with _connect() as conn:
            existing = conn.execute(
                "SELECT id, is_active, mode FROM watchlist WHERE ticker = ?",
                (ticker,),
            ).fetchone()
            now = _utcnow()
            if existing is None:
                conn.execute(
                    """INSERT INTO watchlist (ticker, mode, added_at, is_active)
                       VALUES (?, ?, ?, 1)""",
                    (ticker, mode, now),
                )
                return True, f"added {ticker} ({mode})"
            conn.execute(
                "UPDATE watchlist SET mode = ?, is_active = 1 WHERE id = ?",
                (mode, existing["id"]),
            )
            if existing["is_active"] and existing["mode"] == mode:
                return True, f"{ticker} already in watchlist ({mode})"
            return True, f"updated {ticker} -> {mode}"
    except sqlite3.Error as exc:
        return False, f"db error adding {ticker}: {exc}"


def remove_from_watchlist(ticker: str) -> tuple[bool, str]:
    """Soft-delete a ticker by clearing its is_active flag."""
    ticker = ticker.strip().upper()
    _ensure_schema()
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE watchlist SET is_active = 0 WHERE ticker = ? AND is_active = 1",
                (ticker,),
            )
            if cur.rowcount == 0:
                return False, f"{ticker} not in watchlist"
            return True, f"removed {ticker}"
    except sqlite3.Error as exc:
        return False, f"db error removing {ticker}: {exc}"


def get_watchlist() -> list[dict[str, Any]]:
    """Return every active watchlist entry."""
    _ensure_schema()
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT ticker, mode, added_at, last_checked
                   FROM watchlist
                   WHERE is_active = 1
                   ORDER BY added_at ASC"""
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        log.warning("get_watchlist failed: %s", exc)
        return []


def update_mode(ticker: str, mode: str) -> tuple[bool, str]:
    """Switch a ticker between auto and manual."""
    ticker = ticker.strip().upper()
    mode = mode.strip().lower()
    if mode not in ("auto", "manual"):
        return False, f"invalid mode '{mode}', must be auto or manual"
    _ensure_schema()
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE watchlist SET mode = ? WHERE ticker = ? AND is_active = 1",
                (mode, ticker),
            )
            if cur.rowcount == 0:
                return False, f"{ticker} not in watchlist"
            return True, f"{ticker} mode set to {mode}"
    except sqlite3.Error as exc:
        return False, f"db error updating {ticker}: {exc}"


def touch_last_checked(ticker: str) -> None:
    """Update ``last_checked`` to now for a watchlist row."""
    _ensure_schema()
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE watchlist SET last_checked = ? WHERE ticker = ?",
                (_utcnow(), ticker.upper()),
            )
    except sqlite3.Error as exc:
        log.warning("touch_last_checked %s failed: %s", ticker, exc)


# ---------------------------------------------------------------------------
# Trades & positions
# ---------------------------------------------------------------------------


def log_trade(
    ticker: str,
    action: str,
    side: str,
    contracts: int,
    price: int,
    reason: str | None = None,
    sentiment_score: float | None = None,
    confidence: str | None = None,
) -> tuple[bool, str]:
    """Append a row to ``trade_log`` and update ``positions`` accordingly.

    A buy opens (or appends to) a position; a sell closes contracts and
    realises PnL using the recorded entry price.
    """
    ticker = ticker.strip().upper()
    action = action.strip().lower()
    side = side.strip().lower()
    if action not in ("buy", "sell"):
        return False, f"invalid action '{action}'"
    if side not in ("yes", "no"):
        return False, f"invalid side '{side}'"
    if contracts <= 0:
        return False, "contracts must be positive"
    if not 1 <= price <= 99:
        return False, f"price {price} out of range (1-99 cents)"
    _ensure_schema()
    now = _utcnow()
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO trade_log
                   (ticker, action, side, contracts, price, reason,
                    sentiment_score, confidence, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ticker,
                    action,
                    side,
                    contracts,
                    price,
                    reason,
                    sentiment_score,
                    confidence,
                    now,
                ),
            )
            if action == "buy":
                conn.execute(
                    """INSERT INTO positions
                       (ticker, side, num_contracts, entry_price,
                        current_price, status, opened_at)
                       VALUES (?, ?, ?, ?, ?, 'open', ?)""",
                    (ticker, side, contracts, price, price, now),
                )
                return True, f"logged BUY {contracts}x {ticker} {side.upper()} @ {price}c"
            # sell -> reduce open positions FIFO on the same side
            remaining = contracts
            realised_pnl = 0
            open_rows = conn.execute(
                """SELECT id, num_contracts, entry_price
                   FROM positions
                   WHERE ticker = ? AND side = ? AND status = 'open'
                   ORDER BY opened_at ASC""",
                (ticker, side),
            ).fetchall()
            for row in open_rows:
                if remaining <= 0:
                    break
                take = min(remaining, row["num_contracts"])
                pnl_per = price - row["entry_price"]
                realised_pnl += pnl_per * take
                if take == row["num_contracts"]:
                    conn.execute(
                        """UPDATE positions
                           SET status='closed', closed_at=?, current_price=?, pnl=?
                           WHERE id=?""",
                        (now, price, pnl_per * take, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE positions SET num_contracts = num_contracts - ? WHERE id = ?",
                        (take, row["id"]),
                    )
                remaining -= take
            msg = (
                f"logged SELL {contracts}x {ticker} {side.upper()} @ {price}c "
                f"(realised PnL {realised_pnl:+d}c)"
            )
            if remaining > 0:
                msg += f" — warning: {remaining} contracts sold short of open inventory"
            return True, msg
    except sqlite3.Error as exc:
        return False, f"db error logging trade: {exc}"


def update_position_marks(prices: dict[str, int]) -> None:
    """Refresh ``current_price`` on every open position from a price snapshot."""
    if not prices:
        return
    _ensure_schema()
    try:
        with _connect() as conn:
            for ticker, price in prices.items():
                conn.execute(
                    "UPDATE positions SET current_price = ? WHERE ticker = ? AND status = 'open'",
                    (int(price), ticker.upper()),
                )
    except sqlite3.Error as exc:
        log.warning("update_position_marks failed: %s", exc)


def get_positions() -> list[dict[str, Any]]:
    """Return every open position, oldest first."""
    _ensure_schema()
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT ticker, side, num_contracts, entry_price,
                          current_price, opened_at
                   FROM positions
                   WHERE status = 'open'
                   ORDER BY opened_at ASC"""
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            entry = d["entry_price"]
            current = d["current_price"] or entry
            d["unrealised_pnl_cents"] = (current - entry) * d["num_contracts"]
            out.append(d)
        return out
    except sqlite3.Error as exc:
        log.warning("get_positions failed: %s", exc)
        return []


def get_position(ticker: str) -> dict[str, Any] | None:
    """Return aggregate open exposure for one ticker, or ``None``."""
    ticker = ticker.strip().upper()
    _ensure_schema()
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT side, SUM(num_contracts) AS num_contracts,
                          SUM(num_contracts * entry_price) AS notional,
                          MAX(current_price) AS current_price
                   FROM positions
                   WHERE ticker = ? AND status = 'open'
                   GROUP BY side""",
                (ticker,),
            ).fetchall()
        if not rows:
            return None
        agg = {"ticker": ticker, "legs": []}
        total_contracts = 0
        for r in rows:
            contracts = int(r["num_contracts"])
            if contracts <= 0:
                continue
            avg_entry = int(r["notional"]) // contracts
            current = int(r["current_price"] or avg_entry)
            agg["legs"].append(
                {
                    "side": r["side"],
                    "contracts": contracts,
                    "avg_entry": avg_entry,
                    "current_price": current,
                    "unrealised_pnl_cents": (current - avg_entry) * contracts,
                }
            )
            total_contracts += contracts
        if total_contracts == 0:
            return None
        return agg
    except sqlite3.Error as exc:
        log.warning("get_position %s failed: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def add_alert(ticker: str, alert_type: str, threshold: int) -> tuple[bool, str]:
    """Create a price-crossing alert. ``alert_type`` is price_above|price_below."""
    ticker = ticker.strip().upper()
    alert_type = alert_type.strip().lower()
    if alert_type not in ("price_above", "price_below"):
        return False, f"invalid alert_type '{alert_type}'"
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        return False, f"threshold must be an integer, got {threshold!r}"
    if not 1 <= threshold <= 99:
        return False, f"threshold {threshold} out of range (1-99 cents)"
    _ensure_schema()
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO alerts (ticker, alert_type, threshold, triggered, created_at)
                   VALUES (?, ?, ?, 0, ?)""",
                (ticker, alert_type, threshold, _utcnow()),
            )
        direction = "above" if alert_type == "price_above" else "below"
        return True, f"alert set: {ticker} {direction} {threshold}c"
    except sqlite3.Error as exc:
        return False, f"db error adding alert: {exc}"


def get_triggered_alerts(current_prices: dict[str, int]) -> list[dict[str, Any]]:
    """Return alerts whose threshold was just crossed and mark them triggered.

    ``current_prices`` maps ticker -> last YES price (cents). Untriggered
    alerts whose condition is now true are returned and updated in the same
    transaction so the caller never sees a duplicate.
    """
    if not current_prices:
        return []
    _ensure_schema()
    fired: list[dict[str, Any]] = []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, ticker, alert_type, threshold FROM alerts WHERE triggered = 0"
            ).fetchall()
            for row in rows:
                price = current_prices.get(row["ticker"].upper())
                if price is None:
                    continue
                hit = (
                    row["alert_type"] == "price_above" and price >= row["threshold"]
                ) or (
                    row["alert_type"] == "price_below" and price <= row["threshold"]
                )
                if not hit:
                    continue
                conn.execute("UPDATE alerts SET triggered = 1 WHERE id = ?", (row["id"],))
                fired.append(
                    {
                        "id": row["id"],
                        "ticker": row["ticker"],
                        "alert_type": row["alert_type"],
                        "threshold": row["threshold"],
                        "current_price": int(price),
                    }
                )
    except sqlite3.Error as exc:
        log.warning("get_triggered_alerts failed: %s", exc)
    return fired


def get_open_alerts(ticker: str | None = None) -> list[dict[str, Any]]:
    """Return alerts that haven't fired yet, optionally filtered by ticker."""
    _ensure_schema()
    try:
        with _connect() as conn:
            if ticker:
                rows = conn.execute(
                    """SELECT id, ticker, alert_type, threshold, created_at
                       FROM alerts WHERE triggered = 0 AND ticker = ?""",
                    (ticker.upper(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, ticker, alert_type, threshold, created_at
                       FROM alerts WHERE triggered = 0"""
                ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        log.warning("get_open_alerts failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def get_trade_history(ticker: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent ``trade_log`` rows for ``ticker``, newest first."""
    _ensure_schema()
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT ticker, action, side, contracts, price, reason,
                          sentiment_score, confidence, timestamp
                   FROM trade_log
                   WHERE ticker = ?
                   ORDER BY id DESC
                   LIMIT ?""",
                (ticker.strip().upper(), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        log.warning("get_trade_history %s failed: %s", ticker, exc)
        return []


def _to_json(rows: Iterable[Any]) -> str:
    return json.dumps(list(rows), default=str, indent=2)


if __name__ == "__main__":
    ok, msg = init_db()
    print(json.dumps({"ok": ok, "message": msg}))
