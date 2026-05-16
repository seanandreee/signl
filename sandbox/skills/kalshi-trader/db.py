"""SQLite database manager for the signl Kalshi trading bot.

All public functions defensively wrap their work in try/except and return a
dict of the shape ``{"ok": bool, ...}`` so callers (notably ``watchlist.py``
and ``scanner.py``) can relay error text directly to Telegram.

The database lives next to this module at ``signl.db``.  Schema is created
lazily by :func:`init_db`; every public helper calls it as the first step,
so the module is safe to use after a fresh checkout with no setup script.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

DB_PATH = os.environ.get(
    "SIGNL_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "signl.db"),
)

VALID_MODES = {"auto", "manual"}
VALID_SIDES = {"yes", "no"}
VALID_ACTIONS = {"buy", "sell"}
VALID_ALERT_TYPES = {"price_above", "price_below"}


def _now() -> str:
    """Return current UTC timestamp as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection with row factory + WAL mode enabled."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
    finally:
        conn.close()


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def init_db() -> Dict[str, Any]:
    """Create all tables if they do not already exist.

    Idempotent.  Safe to call from every entrypoint.
    """
    try:
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watchlist (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker       TEXT NOT NULL UNIQUE,
                    mode         TEXT NOT NULL CHECK(mode IN ('auto', 'manual')),
                    added_at     TEXT NOT NULL,
                    last_checked TEXT,
                    is_active    INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS positions (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker        TEXT NOT NULL,
                    side          TEXT NOT NULL CHECK(side IN ('yes', 'no')),
                    num_contracts INTEGER NOT NULL,
                    entry_price   INTEGER NOT NULL,
                    current_price INTEGER,
                    status        TEXT NOT NULL CHECK(status IN ('open', 'closed')),
                    opened_at     TEXT NOT NULL,
                    closed_at     TEXT,
                    pnl           INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_positions_ticker
                    ON positions(ticker);
                CREATE INDEX IF NOT EXISTS idx_positions_status
                    ON positions(status);

                CREATE TABLE IF NOT EXISTS trade_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker          TEXT NOT NULL,
                    action          TEXT NOT NULL CHECK(action IN ('buy', 'sell')),
                    side            TEXT NOT NULL CHECK(side IN ('yes', 'no')),
                    contracts       INTEGER NOT NULL,
                    price           INTEGER NOT NULL,
                    reason          TEXT,
                    sentiment_score REAL,
                    confidence      TEXT,
                    timestamp       TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trade_log_ticker
                    ON trade_log(ticker);

                CREATE TABLE IF NOT EXISTS alerts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker     TEXT NOT NULL,
                    alert_type TEXT NOT NULL
                                  CHECK(alert_type IN ('price_above', 'price_below')),
                    threshold  INTEGER NOT NULL,
                    triggered  INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_ticker
                    ON alerts(ticker);
                """
            )
        return {"ok": True, "msg": f"db initialised at {DB_PATH}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": f"init_db failed: {exc}"}


def add_to_watchlist(ticker: str, mode: str) -> Dict[str, Any]:
    """Add a market to the watchlist, or reactivate / re-mode an existing row.

    Returns ``{"ok": True, "msg": ...}`` and ``status`` ∈ {``added``,
    ``reactivated``, ``already_exists``}.
    """
    try:
        ticker = ticker.strip().upper()
        mode = mode.strip().lower()
        if not ticker:
            return {"ok": False, "error": "ticker is required"}
        if mode not in VALID_MODES:
            return {"ok": False, "error": f"mode must be one of {sorted(VALID_MODES)}"}

        init_db()
        with _connect() as conn:
            existing = conn.execute(
                "SELECT id, is_active, mode FROM watchlist WHERE ticker = ?",
                (ticker,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO watchlist (ticker, mode, added_at, is_active) "
                    "VALUES (?, ?, ?, 1)",
                    (ticker, mode, _now()),
                )
                return {"ok": True, "status": "added",
                        "msg": f"{ticker} added in {mode} mode"}
            if existing["is_active"] == 0:
                conn.execute(
                    "UPDATE watchlist SET is_active = 1, mode = ?, added_at = ? "
                    "WHERE id = ?",
                    (mode, _now(), existing["id"]),
                )
                return {"ok": True, "status": "reactivated",
                        "msg": f"{ticker} reactivated in {mode} mode"}
            return {"ok": True, "status": "already_exists",
                    "msg": f"{ticker} is already on the watchlist "
                           f"({existing['mode']} mode)"}
    except Exception as exc:
        return {"ok": False, "error": f"add_to_watchlist failed: {exc}"}


def remove_from_watchlist(ticker: str) -> Dict[str, Any]:
    """Soft-delete the given ticker from the watchlist (sets ``is_active=0``)."""
    try:
        ticker = ticker.strip().upper()
        if not ticker:
            return {"ok": False, "error": "ticker is required"}
        init_db()
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE watchlist SET is_active = 0 "
                "WHERE ticker = ? AND is_active = 1",
                (ticker,),
            )
            if cur.rowcount == 0:
                return {"ok": False, "error": f"{ticker} not on the watchlist"}
        return {"ok": True, "msg": f"{ticker} removed from watchlist"}
    except Exception as exc:
        return {"ok": False, "error": f"remove_from_watchlist failed: {exc}"}


def get_watchlist() -> Dict[str, Any]:
    """Return all active watchlist rows.

    Result shape: ``{"ok": True, "data": [ {ticker, mode, added_at,
    last_checked, is_active}, ... ]}``.
    """
    try:
        init_db()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, ticker, mode, added_at, last_checked, is_active "
                "FROM watchlist WHERE is_active = 1 ORDER BY added_at",
            ).fetchall()
        return {"ok": True, "data": [dict(r) for r in rows]}
    except Exception as exc:
        return {"ok": False, "error": f"get_watchlist failed: {exc}"}


def update_mode(ticker: str, mode: str) -> Dict[str, Any]:
    """Switch a market between ``auto`` and ``manual`` modes."""
    try:
        ticker = ticker.strip().upper()
        mode = mode.strip().lower()
        if mode not in VALID_MODES:
            return {"ok": False, "error": f"mode must be one of {sorted(VALID_MODES)}"}
        init_db()
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE watchlist SET mode = ? "
                "WHERE ticker = ? AND is_active = 1",
                (mode, ticker),
            )
            if cur.rowcount == 0:
                return {"ok": False, "error": f"{ticker} not on the watchlist"}
        return {"ok": True, "msg": f"{ticker} set to {mode} mode"}
    except Exception as exc:
        return {"ok": False, "error": f"update_mode failed: {exc}"}


def update_last_checked(ticker: str) -> Dict[str, Any]:
    """Stamp ``last_checked`` for ``ticker``.  Used by ``scanner.check_market``."""
    try:
        ticker = ticker.strip().upper()
        init_db()
        with _connect() as conn:
            conn.execute(
                "UPDATE watchlist SET last_checked = ? WHERE ticker = ?",
                (_now(), ticker),
            )
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": f"update_last_checked failed: {exc}"}


def log_trade(
    ticker: str,
    action: str,
    side: str,
    contracts: int,
    price: int,
    reason: Optional[str] = None,
    sentiment_score: Optional[float] = None,
    confidence: Optional[str] = None,
) -> Dict[str, Any]:
    """Append a row to ``trade_log``.

    All numeric inputs are stored verbatim; price is in cents.
    """
    try:
        ticker = ticker.strip().upper()
        action = action.strip().lower()
        side = side.strip().lower()
        if action not in VALID_ACTIONS:
            return {"ok": False, "error": f"action must be one of {sorted(VALID_ACTIONS)}"}
        if side not in VALID_SIDES:
            return {"ok": False, "error": f"side must be one of {sorted(VALID_SIDES)}"}
        init_db()
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO trade_log "
                "(ticker, action, side, contracts, price, reason, "
                " sentiment_score, confidence, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker, action, side, int(contracts), int(price),
                    reason, sentiment_score, confidence, _now(),
                ),
            )
        return {"ok": True, "id": cur.lastrowid,
                "msg": f"trade logged for {ticker}"}
    except Exception as exc:
        return {"ok": False, "error": f"log_trade failed: {exc}"}


def upsert_position(
    ticker: str,
    side: str,
    num_contracts: int,
    entry_price: int,
    current_price: Optional[int] = None,
) -> Dict[str, Any]:
    """Insert a new open position, or top-up an existing open one (weighted-avg
    entry price).  Used after ``log_trade`` for a buy fill.
    """
    try:
        ticker = ticker.strip().upper()
        side = side.strip().lower()
        if side not in VALID_SIDES:
            return {"ok": False, "error": f"side must be one of {sorted(VALID_SIDES)}"}
        init_db()
        with _connect() as conn:
            existing = conn.execute(
                "SELECT id, num_contracts, entry_price FROM positions "
                "WHERE ticker = ? AND side = ? AND status = 'open'",
                (ticker, side),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO positions "
                    "(ticker, side, num_contracts, entry_price, current_price, "
                    " status, opened_at) "
                    "VALUES (?, ?, ?, ?, ?, 'open', ?)",
                    (ticker, side, int(num_contracts), int(entry_price),
                     current_price, _now()),
                )
            else:
                old_n = int(existing["num_contracts"])
                old_p = int(existing["entry_price"])
                new_n = old_n + int(num_contracts)
                if new_n <= 0:
                    return {"ok": False, "error": "resulting contracts must be > 0"}
                blended = round(
                    (old_n * old_p + int(num_contracts) * int(entry_price)) / new_n
                )
                conn.execute(
                    "UPDATE positions SET num_contracts = ?, entry_price = ?, "
                    "current_price = COALESCE(?, current_price) WHERE id = ?",
                    (new_n, blended, current_price, existing["id"]),
                )
        return {"ok": True, "msg": f"{ticker} {side} position updated"}
    except Exception as exc:
        return {"ok": False, "error": f"upsert_position failed: {exc}"}


def update_position_price(ticker: str, current_price: int) -> Dict[str, Any]:
    """Refresh ``current_price`` and unrealised pnl for any open position."""
    try:
        ticker = ticker.strip().upper()
        init_db()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, side, num_contracts, entry_price FROM positions "
                "WHERE ticker = ? AND status = 'open'",
                (ticker,),
            ).fetchall()
            for r in rows:
                if r["side"] == "yes":
                    pnl = (int(current_price) - int(r["entry_price"])) * int(r["num_contracts"])
                else:
                    pnl = ((100 - int(current_price)) - (100 - int(r["entry_price"]))) * int(r["num_contracts"])
                conn.execute(
                    "UPDATE positions SET current_price = ?, pnl = ? WHERE id = ?",
                    (int(current_price), int(pnl), r["id"]),
                )
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": f"update_position_price failed: {exc}"}


def close_position(
    ticker: str,
    side: str,
    contracts: int,
    exit_price: int,
) -> Dict[str, Any]:
    """Close (or partially close) an open position.

    PnL convention: for a YES position, pnl = (exit - entry) * contracts in
    cents; for a NO position, pnl = (entry - exit) * contracts in cents.
    """
    try:
        ticker = ticker.strip().upper()
        side = side.strip().lower()
        if side not in VALID_SIDES:
            return {"ok": False, "error": f"side must be one of {sorted(VALID_SIDES)}"}
        init_db()
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, num_contracts, entry_price FROM positions "
                "WHERE ticker = ? AND side = ? AND status = 'open' "
                "ORDER BY opened_at LIMIT 1",
                (ticker, side),
            ).fetchone()
            if row is None:
                return {"ok": False,
                        "error": f"no open {side} position for {ticker}"}
            held = int(row["num_contracts"])
            entry = int(row["entry_price"])
            qty = min(int(contracts), held)
            if qty <= 0:
                return {"ok": False, "error": "contracts must be > 0"}
            if side == "yes":
                pnl = (int(exit_price) - entry) * qty
            else:
                pnl = (entry - int(exit_price)) * qty
            remaining = held - qty
            if remaining == 0:
                conn.execute(
                    "UPDATE positions SET status = 'closed', closed_at = ?, "
                    "current_price = ?, num_contracts = 0, pnl = COALESCE(pnl, 0) + ? "
                    "WHERE id = ?",
                    (_now(), int(exit_price), int(pnl), row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE positions SET num_contracts = ?, current_price = ?, "
                    "pnl = COALESCE(pnl, 0) + ? WHERE id = ?",
                    (remaining, int(exit_price), int(pnl), row["id"]),
                )
        return {"ok": True, "pnl_cents": pnl,
                "msg": f"closed {qty} {ticker} {side} @ {exit_price}c (pnl {pnl}c)"}
    except Exception as exc:
        return {"ok": False, "error": f"close_position failed: {exc}"}


def get_positions() -> Dict[str, Any]:
    """Return all open positions across all tickers."""
    try:
        init_db()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, ticker, side, num_contracts, entry_price, "
                "       current_price, status, opened_at, closed_at, pnl "
                "FROM positions WHERE status = 'open' ORDER BY opened_at",
            ).fetchall()
        return {"ok": True, "data": [dict(r) for r in rows]}
    except Exception as exc:
        return {"ok": False, "error": f"get_positions failed: {exc}"}


def get_position(ticker: str) -> Dict[str, Any]:
    """Return the first open position for ``ticker``, or ``None`` if none."""
    try:
        ticker = ticker.strip().upper()
        init_db()
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, ticker, side, num_contracts, entry_price, "
                "       current_price, status, opened_at, closed_at, pnl "
                "FROM positions "
                "WHERE ticker = ? AND status = 'open' "
                "ORDER BY opened_at LIMIT 1",
                (ticker,),
            ).fetchone()
        return {"ok": True, "data": _row_to_dict(row)}
    except Exception as exc:
        return {"ok": False, "error": f"get_position failed: {exc}"}


def add_alert(ticker: str, alert_type: str, threshold: int) -> Dict[str, Any]:
    """Register a one-shot price alert."""
    try:
        ticker = ticker.strip().upper()
        alert_type = alert_type.strip().lower()
        if alert_type not in VALID_ALERT_TYPES:
            return {"ok": False,
                    "error": f"alert_type must be one of {sorted(VALID_ALERT_TYPES)}"}
        threshold_int = int(threshold)
        if not 1 <= threshold_int <= 99:
            return {"ok": False,
                    "error": "threshold must be between 1 and 99 cents"}
        init_db()
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO alerts (ticker, alert_type, threshold, triggered, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (ticker, alert_type, threshold_int, _now()),
            )
        return {"ok": True, "id": cur.lastrowid,
                "msg": f"alert set: {ticker} {alert_type} {threshold_int}c"}
    except Exception as exc:
        return {"ok": False, "error": f"add_alert failed: {exc}"}


def get_triggered_alerts(ticker: str, current_price: int) -> Dict[str, Any]:
    """Find every un-triggered alert for ``ticker`` whose threshold the
    ``current_price`` (cents) has now crossed, mark them triggered, and
    return them.
    """
    try:
        ticker = ticker.strip().upper()
        cp = int(current_price)
        init_db()
        triggered: List[Dict[str, Any]] = []
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, ticker, alert_type, threshold, triggered, created_at "
                "FROM alerts WHERE ticker = ? AND triggered = 0",
                (ticker,),
            ).fetchall()
            for r in rows:
                fires = (
                    (r["alert_type"] == "price_above" and cp >= int(r["threshold"]))
                    or (r["alert_type"] == "price_below" and cp <= int(r["threshold"]))
                )
                if fires:
                    conn.execute(
                        "UPDATE alerts SET triggered = 1 WHERE id = ?",
                        (r["id"],),
                    )
                    triggered.append(dict(r))
        return {"ok": True, "data": triggered}
    except Exception as exc:
        return {"ok": False, "error": f"get_triggered_alerts failed: {exc}"}


def get_trade_history(ticker: str, limit: int = 50) -> Dict[str, Any]:
    """Return the most recent trades for ``ticker`` (default last 50)."""
    try:
        ticker = ticker.strip().upper()
        init_db()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, ticker, action, side, contracts, price, reason, "
                "       sentiment_score, confidence, timestamp "
                "FROM trade_log WHERE ticker = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (ticker, int(limit)),
            ).fetchall()
        return {"ok": True, "data": [dict(r) for r in rows]}
    except Exception as exc:
        return {"ok": False, "error": f"get_trade_history failed: {exc}"}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    print(init_db())
