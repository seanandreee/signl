"""User-facing command surface.

OpenClaw invokes ``python3 watchlist.py "<telegram message>"`` for every
inbound DM. ``handle_command`` dispatches to one of the action functions
below based on a fixed regex table. Each action returns ``(ok, message,
data)`` and the CLI wrapper prints a single JSON object so OpenClaw can
forward ``message`` straight to Telegram.

Supported commands (case-insensitive, whitespace-tolerant):

* ``add <TICKER> auto|manual``
* ``remove <TICKER>``
* ``set <TICKER> to auto|manual``
* ``watchlist`` or ``show watchlist``
* ``positions``
* ``buy <N> <TICKER> yes|no at <PRICE>``
* ``sell <N> <TICKER> at <PRICE>``
* ``alert <TICKER> above|below <PRICE>``
* ``balance``
* ``analyze <TICKER>``
* ``scan``
* ``help`` (extra convenience)
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Callable

# Load ~/kalshi-signl/.env into the process env BEFORE importing modules
# that read os.environ at import time (kalshi_client, sentiment).
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
except ImportError:  # python-dotenv not installed yet; env must be exported
    pass

import db
import kalshi_client
import scanner

log = logging.getLogger("signl.watchlist")

ActionResult = tuple[bool, str, Any]
Handler = Callable[..., ActionResult]


# ---------------------------------------------------------------------------
# Action functions
# ---------------------------------------------------------------------------


def add_market(ticker: str, mode: str) -> ActionResult:
    """Validate the ticker on Kalshi and add it to the watchlist."""
    ticker = ticker.strip().upper()
    mode = mode.strip().lower()
    market, err = kalshi_client.get_market(ticker)
    if err:
        if "not found" in err.lower():
            return False, f"ticker {ticker} not found on Kalshi", None
        return False, f"could not validate {ticker}: {err}", None
    ok, msg = db.add_to_watchlist(ticker, mode)
    if not ok:
        return False, msg, None
    title = market.get("title") if market else ticker
    return True, f"{msg} — {title}", {"ticker": ticker, "mode": mode}


def remove_market(ticker: str) -> ActionResult:
    """Soft-delete a ticker from the watchlist."""
    ok, msg = db.remove_from_watchlist(ticker)
    return ok, msg, None


def switch_mode(ticker: str, mode: str) -> ActionResult:
    """Flip an existing watchlist row between auto and manual."""
    ok, msg = db.update_mode(ticker, mode)
    return ok, msg, None


def show_watchlist() -> ActionResult:
    """Return the formatted watchlist table."""
    return True, scanner.format_watchlist(), None


def show_positions() -> ActionResult:
    """Show every open position with unrealised PnL."""
    positions = db.get_positions()
    if not positions:
        return True, "no open positions", []
    header = f"{'TICKER':<22} {'SIDE':<4} {'QTY':>4} {'ENTRY':>6} {'MARK':>5} {'PnL':>7}"
    lines = [header, "-" * len(header)]
    total = 0
    for p in positions:
        pnl = p.get("unrealised_pnl_cents", 0)
        total += pnl
        lines.append(
            f"{p['ticker']:<22} {p['side'].upper():<4} {p['num_contracts']:>4} "
            f"{p['entry_price']:>5}c {p.get('current_price') or p['entry_price']:>4}c "
            f"{pnl:>+6}c"
        )
    lines.append(f"{'TOTAL':<22} {'':<4} {'':>4} {'':>6} {'':>5} {total:>+6}c")
    return True, "\n".join(lines), positions


def manual_buy(ticker: str, side: str, contracts: int, price_cents: int) -> ActionResult:
    """Place a manual buy and log it."""
    order, err = kalshi_client.place_order(
        ticker=ticker,
        side=side,
        action="buy",
        contracts=int(contracts),
        price_cents=int(price_cents),
    )
    if err:
        return False, f"order rejected: {err}", None
    db.log_trade(
        ticker=ticker,
        action="buy",
        side=side,
        contracts=int(contracts),
        price=int(price_cents),
        reason="manual",
    )
    return (
        True,
        f"BUY {contracts}x {ticker.upper()} {side.upper()} @ {price_cents}c "
        f"(order {order.get('order_id')}, status {order.get('status')})",
        order,
    )


def manual_sell(ticker: str, contracts: int, price_cents: int) -> ActionResult:
    """Place a manual sell against the largest existing open leg.

    Picks the side from the current open position; refuses if both sides
    are held (the user should be explicit with ``buy`` in that case).
    """
    pos = db.get_position(ticker)
    if not pos or not pos.get("legs"):
        return False, f"no open position on {ticker.upper()} to sell", None
    legs = pos["legs"]
    if len(legs) > 1:
        return (
            False,
            f"{ticker.upper()} has both YES and NO legs open — "
            "use 'buy ... yes|no' against the opposite side to close",
            pos,
        )
    side = legs[0]["side"]
    order, err = kalshi_client.place_order(
        ticker=ticker,
        side=side,
        action="sell",
        contracts=int(contracts),
        price_cents=int(price_cents),
    )
    if err:
        return False, f"order rejected: {err}", None
    db.log_trade(
        ticker=ticker,
        action="sell",
        side=side,
        contracts=int(contracts),
        price=int(price_cents),
        reason="manual",
    )
    return (
        True,
        f"SELL {contracts}x {ticker.upper()} {side.upper()} @ {price_cents}c "
        f"(order {order.get('order_id')}, status {order.get('status')})",
        order,
    )


def set_alert(ticker: str, alert_type: str, threshold: int) -> ActionResult:
    """Add a price-crossing alert."""
    ok, msg = db.add_alert(ticker, alert_type, int(threshold))
    return ok, msg, None


def show_balance() -> ActionResult:
    """Return the Kalshi available balance in dollars and cents."""
    bal, err = kalshi_client.get_balance()
    if err or not bal or bal.get("balance_cents") is None:
        return False, f"balance lookup failed: {err or 'no balance'}", None
    cents = int(bal["balance_cents"])
    return True, f"balance: ${cents/100:,.2f} ({cents}c)", bal


def analyze_market(ticker: str) -> ActionResult:
    """Run the full sentiment + risk pipeline and return the manual report."""
    msg = scanner.run_manual_update(ticker)
    if msg.startswith(f"MANUAL {ticker.upper()}: error"):
        return False, msg, None
    return True, msg, None


def scan_now() -> ActionResult:
    """Trigger an immediate scan over the whole watchlist."""
    result = scanner.scan_all()
    updates = result["updates"]
    header = (
        f"scan complete — {result['scanned']} markets, "
        f"{len(updates)} updates, {len(result['errors'])} errors"
    )
    body = "\n\n".join(updates) if updates else "(no updates)"
    return True, f"{header}\n\n{body}", result


def help_text() -> ActionResult:
    """Return the in-bot command reference."""
    return True, _HELP, None


_HELP = """signl commands:
  add <TICKER> auto|manual
  remove <TICKER>
  set <TICKER> to auto|manual
  watchlist
  positions
  buy <N> <TICKER> yes|no at <PRICE>
  sell <N> <TICKER> at <PRICE>
  alert <TICKER> above|below <PRICE>
  balance
  analyze <TICKER>
  scan
  help"""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


_TICKER = r"[A-Za-z0-9\-_.]+"
_PRICE = r"\d{1,3}"
_QTY = r"\d{1,5}"

_ROUTES: list[tuple[re.Pattern[str], Handler]] = [
    (re.compile(rf"^add\s+({_TICKER})\s+(auto|manual)$", re.I), add_market),
    (re.compile(rf"^remove\s+({_TICKER})$", re.I), remove_market),
    (re.compile(rf"^set\s+({_TICKER})\s+to\s+(auto|manual)$", re.I), switch_mode),
    (re.compile(r"^(?:show\s+)?watchlist$", re.I), lambda: show_watchlist()),
    (re.compile(r"^positions$", re.I), lambda: show_positions()),
    (
        re.compile(rf"^buy\s+({_QTY})\s+({_TICKER})\s+(yes|no)\s+at\s+({_PRICE})$", re.I),
        lambda n, t, s, p: manual_buy(t, s, int(n), int(p)),
    ),
    (
        re.compile(rf"^sell\s+({_QTY})\s+({_TICKER})\s+at\s+({_PRICE})$", re.I),
        lambda n, t, p: manual_sell(t, int(n), int(p)),
    ),
    (
        re.compile(rf"^alert\s+({_TICKER})\s+(above|below)\s+({_PRICE})$", re.I),
        lambda t, d, p: set_alert(t, f"price_{d.lower()}", int(p)),
    ),
    (re.compile(r"^balance$", re.I), lambda: show_balance()),
    (re.compile(rf"^analyze\s+({_TICKER})$", re.I), lambda t: analyze_market(t)),
    (re.compile(r"^scan$", re.I), lambda: scan_now()),
    (re.compile(r"^help$", re.I), lambda: help_text()),
]


def handle_command(message: str) -> ActionResult:
    """Parse ``message`` and dispatch to the matching handler.

    Returns ``(False, "unknown command...", None)`` when nothing matches.
    All handler exceptions are caught and converted into an error tuple.
    """
    text = (message or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return False, "empty message — try 'help'", None
    for pattern, handler in _ROUTES:
        m = pattern.match(text)
        if not m:
            continue
        try:
            return handler(*m.groups())
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.exception("handler %s failed", getattr(handler, "__name__", handler))
            return False, f"error: {exc}", None
    return False, f"unknown command: {text!r} — send 'help' for the list", None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str))


def _cli(argv: list[str]) -> int:
    if not argv:
        _emit({"ok": False, "message": "usage: watchlist.py \"<telegram message>\""})
        return 2
    message = " ".join(argv).strip()
    db.init_db()
    ok, msg, data = handle_command(message)
    _emit({"ok": ok, "message": msg, "data": data})
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
