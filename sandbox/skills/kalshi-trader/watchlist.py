"""Telegram command surface for the signl bot.

Every public function takes the parsed arguments OpenClaw will hand to it
and returns a string that should be relayed verbatim to Telegram.

The ``handle_command(message)`` master router parses a raw Telegram message
(case-insensitive, leading slash optional) and dispatches to the right
helper, returning the user-visible response.  ``__main__`` exposes the
router so OpenClaw can shell out with a single command:

    python3 -m watchlist handle "<raw telegram message>"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # pragma: no cover
    pass

import db
import kalshi_client
import scanner
import sentiment


HELP_TEXT = """\
signl commands:
  add <TICKER> auto|manual          - add a market to the watchlist
  remove <TICKER>                   - remove from watchlist
  set <TICKER> to auto|manual       - switch mode
  watchlist | show watchlist        - print watchlist with prices
  positions                         - print open positions with PnL
  balance                           - show Kalshi balance
  buy <N> <TICKER> <yes|no> at <P>  - place buy limit order at P cents
  sell <N> <TICKER> at <P>          - place sell on existing position
  alert <TICKER> above|below <P>    - set price alert in cents
  analyze <TICKER>                  - run a full sentiment + market report
  scan                              - run the full watchlist now
  help                              - show this message
"""


def _err(msg: str) -> str:
    return f"error: {msg}"


def add_market(ticker: str, mode: str) -> str:
    """Validate the ticker on Kalshi, then add it to the watchlist."""
    ticker = (ticker or "").strip().upper()
    mode = (mode or "").strip().lower()
    if not ticker:
        return _err("ticker is required")
    if mode not in ("auto", "manual"):
        return _err("mode must be 'auto' or 'manual'")
    m = kalshi_client.get_market(ticker)
    if not m.get("ok"):
        return _err(f"could not validate {ticker} on Kalshi: {m.get('error')}")
    res = db.add_to_watchlist(ticker, mode)
    if not res.get("ok"):
        return _err(res.get("error", "add failed"))
    title = m["data"].get("title") or ticker
    if res.get("status") == "already_exists":
        return f"{ticker} already on watchlist - {title}"
    return f"added {ticker} ({title}) in {mode} mode"


def remove_market(ticker: str) -> str:
    """Remove a market from the watchlist."""
    res = db.remove_from_watchlist(ticker)
    return res.get("msg") if res.get("ok") else _err(res.get("error", "remove failed"))


def switch_mode(ticker: str, mode: str) -> str:
    """Switch a watchlist entry between auto and manual mode."""
    res = db.update_mode(ticker, mode)
    return res.get("msg") if res.get("ok") else _err(res.get("error", "update failed"))


def show_watchlist() -> str:
    """Return the formatted watchlist (delegates to scanner.format_watchlist)."""
    try:
        return scanner.format_watchlist()
    except Exception as exc:
        return _err(f"show_watchlist failed: {exc}")


def show_positions() -> str:
    """Return open positions with current PnL."""
    try:
        kp = kalshi_client.get_my_positions()
        kalshi_pos = kp.get("data", []) if kp.get("ok") else []
        local = db.get_positions()
        if not local.get("ok"):
            return _err(local.get("error", "get_positions failed"))
        rows = local["data"]
        if not rows and not kalshi_pos:
            return "No open positions."

        lines = ["Open positions:"]
        seen_tickers: List[str] = []
        for r in rows:
            ticker = r["ticker"]
            seen_tickers.append(ticker)
            current = r.get("current_price")
            if current is None:
                m = kalshi_client.get_market(ticker)
                if m.get("ok"):
                    current = (m["data"].get("yes_ask")
                               or m["data"].get("last_price")
                               or m["data"].get("yes_bid"))
            n = int(r["num_contracts"])
            entry = int(r["entry_price"])
            side = r["side"]
            if current is not None:
                if side == "yes":
                    pnl = (int(current) - entry) * n
                else:
                    pnl = (entry - int(current)) * n
            else:
                pnl = r.get("pnl") or 0
            lines.append(
                f"  {ticker} {n} {side.upper()} @ {entry}c "
                f"(cur {current if current is not None else '?'}c, "
                f"pnl {int(pnl)}c)"
            )
        for kp_row in kalshi_pos:
            kt = (kp_row.get("ticker") or "").upper()
            if kt and kt not in seen_tickers:
                lines.append(
                    f"  {kt} (Kalshi-only) "
                    f"contracts={kp_row.get('position') or kp_row.get('contracts')}"
                )
        return "\n".join(lines)
    except Exception as exc:
        return _err(f"show_positions failed: {exc}")


def manual_buy(ticker: str, side: str, contracts: int, price_cents: int) -> str:
    """Place a manual buy order, log it, and update local position state."""
    try:
        ticker = ticker.strip().upper()
        side = side.strip().lower()
        contracts = int(contracts)
        price_cents = int(price_cents)
    except Exception as exc:
        return _err(f"bad arguments: {exc}")

    order = kalshi_client.place_order(
        ticker, side=side, action="buy",
        contracts=contracts, price_cents=price_cents,
    )
    if not order.get("ok"):
        return _err(f"order failed: {order.get('error')}")
    db.log_trade(
        ticker, action="buy", side=side, contracts=contracts,
        price=price_cents, reason="manual",
    )
    db.upsert_position(ticker, side, contracts, price_cents,
                       current_price=price_cents)
    return (f"BUY placed: {contracts} {ticker} {side.upper()} @ {price_cents}c "
            f"(order {order.get('order_id')})")


def manual_sell(ticker: str, contracts: int, price_cents: int) -> str:
    """Place a manual sell on an existing position, prefer the larger side."""
    try:
        ticker = ticker.strip().upper()
        contracts = int(contracts)
        price_cents = int(price_cents)
    except Exception as exc:
        return _err(f"bad arguments: {exc}")

    pos = db.get_position(ticker).get("data")
    if not pos:
        return _err(f"no open position for {ticker} - cannot sell")
    side = pos["side"]
    if int(pos["num_contracts"]) < contracts:
        return _err(
            f"position has only {pos['num_contracts']} contracts; "
            f"requested {contracts}"
        )

    order = kalshi_client.place_order(
        ticker, side=side, action="sell",
        contracts=contracts, price_cents=price_cents,
    )
    if not order.get("ok"):
        return _err(f"order failed: {order.get('error')}")
    db.log_trade(
        ticker, action="sell", side=side, contracts=contracts,
        price=price_cents, reason="manual",
    )
    closed = db.close_position(ticker, side, contracts, price_cents)
    pnl_str = (f" pnl {closed.get('pnl_cents')}c"
               if closed.get("ok") else "")
    return (f"SELL placed: {contracts} {ticker} {side.upper()} @ {price_cents}c"
            f" (order {order.get('order_id')}){pnl_str}")


def set_alert(ticker: str, alert_type: str, threshold: int) -> str:
    """Add a one-shot price alert."""
    res = db.add_alert(ticker, alert_type, threshold)
    return res.get("msg") if res.get("ok") else _err(res.get("error", "alert failed"))


def show_balance() -> str:
    """Return the current Kalshi balance in dollars."""
    try:
        b = kalshi_client.get_balance()
        if not b.get("ok"):
            return _err(b.get("error", "balance failed"))
        cents = int(b["balance_cents"])
        return f"Balance: ${cents/100:.2f} ({cents} cents)"
    except Exception as exc:
        return _err(f"show_balance failed: {exc}")


def analyze(ticker: str) -> str:
    """Run get_full_analysis and render a Telegram-friendly report."""
    try:
        full = sentiment.get_full_analysis(ticker)
        if not full.get("ok"):
            return _err(f"analyze failed: {full.get('error')}")
        data = full["data"]
        market = data["market"]
        a = data["analysis"]
        yes_price = data["yes_price_cents"]
        title = market.get("title") or ticker
        headlines = data.get("headlines") or []
        headline_block = "\n".join(
            f"  - {h.get('title', '')[:140]}" for h in headlines[:5]
        ) or "  (no recent headlines)"
        factor_block = "\n".join(f"  - {f}" for f in a.get("key_factors", [])) \
            or "  (none)"
        return (
            f"{ticker} analysis\n"
            f"  Title: {title}\n"
            f"  YES bid/ask: {market.get('yes_bid')}/{market.get('yes_ask')} | "
            f"vol: {market.get('volume')}\n"
            f"  Model P(YES): {a['probability']}% | confidence: "
            f"{a['confidence']}\n"
            f"  Recommended: {a['recommended_action']}\n"
            f"  Reasoning: {a['reasoning']}\n"
            f"  Key factors:\n{factor_block}\n"
            f"  Recent news:\n{headline_block}"
        )
    except Exception as exc:
        return _err(f"analyze failed: {exc}")


def run_scan_now() -> str:
    """Trigger scanner.scan_all and return its output joined by blank lines."""
    try:
        out = scanner.scan_all()
        return "\n\n".join(out) if out else "(no markets to scan)"
    except Exception as exc:
        return _err(f"scan failed: {exc}")


_PATTERNS: List[tuple[re.Pattern[str], Callable[..., str]]] = [
    (re.compile(r"^add\s+(\S+)\s+(auto|manual)\s*$", re.I),
     lambda t, m: add_market(t, m)),
    (re.compile(r"^remove\s+(\S+)\s*$", re.I),
     lambda t: remove_market(t)),
    (re.compile(r"^set\s+(\S+)\s+to\s+(auto|manual)\s*$", re.I),
     lambda t, m: switch_mode(t, m)),
    (re.compile(r"^(?:show\s+)?watchlist\s*$", re.I),
     lambda: show_watchlist()),
    (re.compile(r"^positions?\s*$", re.I),
     lambda: show_positions()),
    (re.compile(r"^balance\s*$", re.I),
     lambda: show_balance()),
    (re.compile(r"^scan\s*$", re.I),
     lambda: run_scan_now()),
    (re.compile(r"^buy\s+(\d+)\s+(\S+)\s+(yes|no)\s+at\s+(\d+)\s*c?\s*$", re.I),
     lambda n, t, s, p: manual_buy(t, s, int(n), int(p))),
    (re.compile(r"^sell\s+(\d+)\s+(\S+)\s+at\s+(\d+)\s*c?\s*$", re.I),
     lambda n, t, p: manual_sell(t, int(n), int(p))),
    (re.compile(r"^alert\s+(\S+)\s+(above|below)\s+(\d+)\s*c?\s*$", re.I),
     lambda t, d, p: set_alert(t, "price_above" if d.lower() == "above"
                                else "price_below", int(p))),
    (re.compile(r"^analyze\s+(\S+)\s*$", re.I),
     lambda t: analyze(t)),
    (re.compile(r"^(?:help|/help|\?)\s*$", re.I),
     lambda: HELP_TEXT),
]


def handle_command(message: str) -> str:
    """Parse a raw Telegram message and dispatch.

    The message may begin with a leading ``/`` (e.g. ``/watchlist``) or be
    plain text.  Unknown commands return ``HELP_TEXT``.
    """
    try:
        if message is None:
            return HELP_TEXT
        msg = message.strip()
        if msg.startswith("/"):
            msg = msg[1:].lstrip()
        if not msg:
            return HELP_TEXT
        for pattern, handler in _PATTERNS:
            match = pattern.match(msg)
            if match:
                return handler(*match.groups())
        return f"unknown command: {message!r}\n\n{HELP_TEXT}"
    except Exception as exc:
        return _err(f"handle_command failed: {exc}")


def _cli() -> None:  # pragma: no cover - manual entrypoint
    parser = argparse.ArgumentParser(description="signl watchlist CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_handle = sub.add_parser("handle",
                              help="route a raw Telegram message string")
    p_handle.add_argument("message", nargs="*",
                          help="message text (joined with spaces)")

    sub.add_parser("watchlist")
    sub.add_parser("positions")
    sub.add_parser("balance")
    sub.add_parser("scan")
    sub.add_parser("help")

    p_add = sub.add_parser("add"); p_add.add_argument("ticker"); p_add.add_argument("mode")
    p_rm = sub.add_parser("remove"); p_rm.add_argument("ticker")
    p_set = sub.add_parser("set"); p_set.add_argument("ticker"); p_set.add_argument("mode")
    p_an = sub.add_parser("analyze"); p_an.add_argument("ticker")
    p_buy = sub.add_parser("buy")
    p_buy.add_argument("ticker"); p_buy.add_argument("side")
    p_buy.add_argument("contracts", type=int); p_buy.add_argument("price", type=int)
    p_sell = sub.add_parser("sell")
    p_sell.add_argument("ticker"); p_sell.add_argument("contracts", type=int)
    p_sell.add_argument("price", type=int)
    p_alert = sub.add_parser("alert")
    p_alert.add_argument("ticker"); p_alert.add_argument("direction")
    p_alert.add_argument("threshold", type=int)

    ns = parser.parse_args()

    if ns.cmd == "handle":
        msg = " ".join(ns.message) if ns.message else sys.stdin.read()
        print(handle_command(msg))
    elif ns.cmd == "watchlist":
        print(show_watchlist())
    elif ns.cmd == "positions":
        print(show_positions())
    elif ns.cmd == "balance":
        print(show_balance())
    elif ns.cmd == "scan":
        print(run_scan_now())
    elif ns.cmd == "help":
        print(HELP_TEXT)
    elif ns.cmd == "add":
        print(add_market(ns.ticker, ns.mode))
    elif ns.cmd == "remove":
        print(remove_market(ns.ticker))
    elif ns.cmd == "set":
        print(switch_mode(ns.ticker, ns.mode))
    elif ns.cmd == "analyze":
        print(analyze(ns.ticker))
    elif ns.cmd == "buy":
        print(manual_buy(ns.ticker, ns.side, ns.contracts, ns.price))
    elif ns.cmd == "sell":
        print(manual_sell(ns.ticker, ns.contracts, ns.price))
    elif ns.cmd == "alert":
        d = "price_above" if ns.direction.lower() == "above" else "price_below"
        print(set_alert(ns.ticker, d, ns.threshold))


if __name__ == "__main__":
    _cli()
