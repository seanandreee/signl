"""Watchlist scanner — the autonomous loop OpenClaw schedules every 15 min.

Modes:

* ``auto``   – the bot fetches the market, runs sentiment, sizes a position
  with Kelly, and (if the edge clears ``MIN_EDGE``) places a limit order on
  Kalshi at the current ask. Every step is logged to SQLite.
* ``manual`` – the bot only fetches market + sentiment and returns a
  nicely-formatted Telegram message for the user to act on.

CLI entrypoints (so SKILL.md can call this directly):

* ``python -m scanner scan_all``        – run the full watchlist
* ``python -m scanner check <ticker>``  – refresh prices + alerts for one ticker
* ``python -m scanner auto <ticker>``   – force an auto-trade pass on one ticker
* ``python -m scanner manual <ticker>`` – force a manual report on one ticker
* ``python -m scanner format``          – print the watchlist table
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
except ImportError:
    pass

import db
import kalshi_client
import risk
import sentiment

log = logging.getLogger("signl.scanner")

MIN_EDGE = 0.05          # 5 cents of edge before we trade in auto mode
MAX_POSITION_PCT = 0.05  # never spend more than 5% of bankroll on one bet
KELLY_FRACTION = 0.25    # quarter-Kelly


# ---------------------------------------------------------------------------
# Per-market work units
# ---------------------------------------------------------------------------


def check_market(ticker: str) -> dict[str, Any]:
    """Fetch latest market state, persist last_checked, and evaluate alerts.

    Always returns a dict; ``error`` is populated when the market lookup fails.
    """
    ticker = ticker.strip().upper()
    market, err = kalshi_client.get_market(ticker)
    if err:
        return {"ticker": ticker, "error": err}
    db.touch_last_checked(ticker)
    yes_ask = market.get("yes_ask")
    if isinstance(yes_ask, int):
        db.update_position_marks({ticker: yes_ask})
    alerts = db.get_triggered_alerts(
        {ticker: yes_ask} if isinstance(yes_ask, int) else {}
    )
    return {
        "ticker": ticker,
        "market": market,
        "alerts_fired": alerts,
    }


def run_auto_trade(ticker: str) -> str:
    """Auto pipeline: analysis -> sizing -> order -> log.

    Returns a one-line Telegram-friendly summary.
    """
    ticker = ticker.strip().upper()
    analysis, err = sentiment.get_full_analysis(ticker)
    if err:
        return f"AUTO {ticker}: error fetching analysis: {err}"
    market = analysis["market"]
    sent = analysis["sentiment"]
    prob_pct = sent.get("probability")
    yes_ask = market.get("yes_ask")
    no_ask = market.get("no_ask")
    if prob_pct is None or yes_ask is None:
        return (
            f"AUTO {ticker}: skipping — missing data "
            f"(prob={prob_pct}, yes_ask={yes_ask})"
        )

    p = prob_pct / 100.0
    try:
        go, edge, side = risk.should_trade(p, int(yes_ask), MIN_EDGE)
    except ValueError as exc:
        return f"AUTO {ticker}: risk error: {exc}"
    if not go:
        return (
            f"AUTO {ticker} HOLD — p={p:.2f} vs market {yes_ask/100:.2f} "
            f"(edge {edge:+.2f}, need ±{MIN_EDGE:.2f})"
        )

    # Pick the appropriate ask price to lift.
    price_to_pay = yes_ask if side == "yes" else no_ask
    if price_to_pay is None or not 1 <= price_to_pay <= 99:
        return f"AUTO {ticker}: side {side} has no usable ask price"

    bal, bal_err = kalshi_client.get_balance()
    if bal_err or not bal or bal.get("balance_cents") is None:
        return f"AUTO {ticker}: balance lookup failed: {bal_err or 'no balance'}"
    balance_cents = int(bal["balance_cents"])

    # Position sizing always uses the probability of the SIDE we're buying.
    side_prob = p if side == "yes" else 1.0 - p
    try:
        contracts = risk.calculate_position_size(
            side_prob,
            price_to_pay,
            balance_cents,
            max_position_pct=MAX_POSITION_PCT,
            kelly_fraction=KELLY_FRACTION,
        )
    except ValueError as exc:
        return f"AUTO {ticker}: sizing error: {exc}"
    if contracts <= 0:
        return (
            f"AUTO {ticker} HOLD — sizing returned 0 contracts "
            f"(balance {balance_cents}c, price {price_to_pay}c)"
        )

    order, ord_err = kalshi_client.place_order(
        ticker=ticker,
        side=side,
        action="buy",
        contracts=contracts,
        price_cents=price_to_pay,
    )
    if ord_err or not order:
        return (
            f"AUTO {ticker}: order REJECTED — {ord_err or 'unknown'} "
            f"(would have bought {contracts}x {side.upper()} @ {price_to_pay}c)"
        )

    db.log_trade(
        ticker=ticker,
        action="buy",
        side=side,
        contracts=contracts,
        price=price_to_pay,
        reason=(sent.get("reasoning") or "")[:240],
        sentiment_score=p,
        confidence=sent.get("confidence"),
    )
    return (
        f"AUTO {ticker} {side.upper()} x{contracts} @ {price_to_pay}c "
        f"(p={p:.2f}, edge={edge:+.2f}, conf={sent.get('confidence')}, "
        f"order={order.get('order_id')})"
    )


def run_manual_update(ticker: str) -> str:
    """Manual pipeline: analysis only, formatted for Telegram."""
    ticker = ticker.strip().upper()
    analysis, err = sentiment.get_full_analysis(ticker)
    if err:
        return f"MANUAL {ticker}: error fetching analysis: {err}"
    market = analysis["market"]
    sent = analysis["sentiment"]
    headlines = analysis["headlines"]

    yes_bid = market.get("yes_bid")
    yes_ask = market.get("yes_ask")
    vol = market.get("volume_24h") or market.get("volume") or 0
    title = market.get("title") or ticker
    prob = sent.get("probability")
    prob_str = f"{prob}%" if prob is not None else "n/a"
    market_implied = f"{yes_ask}%" if yes_ask is not None else "n/a"

    edge_str = "n/a"
    suggestion = "hold"
    if prob is not None and yes_ask is not None:
        try:
            _, edge, side = risk.should_trade(prob / 100.0, int(yes_ask), MIN_EDGE)
            edge_str = f"{edge:+.2f}"
            if side == "yes":
                suggestion = "BUY YES"
            elif side == "no":
                suggestion = "BUY NO"
        except ValueError:
            pass

    lines = [
        f"📊 {ticker} (manual)",
        f"{title}",
        f"yes_bid {yes_bid}c  yes_ask {yes_ask}c  vol24h {vol}",
        f"signl p={prob_str}  market={market_implied}  edge={edge_str}",
        f"action: {sent.get('recommended_action', 'hold')} "
        f"(conf {sent.get('confidence', 'low')}) → suggestion: {suggestion}",
        f"why: {sent.get('reasoning', '')}",
    ]
    if sent.get("key_factors"):
        lines.append("factors: " + "; ".join(sent["key_factors"]))
    if headlines:
        lines.append("headlines:")
        for h in headlines[:5]:
            t = (h.get("title") or "").strip()
            if t:
                lines.append(f"  • {t}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Whole-watchlist passes
# ---------------------------------------------------------------------------


def scan_all() -> dict[str, Any]:
    """Iterate the watchlist, run the right pipeline for each entry.

    Returns ``{"updates": [str, ...], "errors": [str, ...]}`` where each
    update is a Telegram-ready message. Alerts that fired during the scan
    are appended as extra updates.
    """
    db.init_db()
    rows = db.get_watchlist()
    updates: list[str] = []
    errors: list[str] = []
    price_snapshot: dict[str, int] = {}
    for row in rows:
        ticker = row["ticker"]
        mode = row["mode"]
        try:
            if mode == "auto":
                msg = run_auto_trade(ticker)
            else:
                msg = run_manual_update(ticker)
        except Exception as exc:  # last-resort guard, never crash the loop
            log.exception("scan_all failed on %s", ticker)
            err = f"{ticker}: unhandled error: {exc}"
            errors.append(err)
            updates.append(err)
            continue
        updates.append(msg)
        # Best-effort price scrape so triggered alerts are evaluated once below.
        market, _ = kalshi_client.get_market(ticker)
        if market and isinstance(market.get("yes_ask"), int):
            price_snapshot[ticker] = market["yes_ask"]
        db.touch_last_checked(ticker)
    if price_snapshot:
        db.update_position_marks(price_snapshot)
        for alert in db.get_triggered_alerts(price_snapshot):
            updates.append(
                f"🔔 ALERT {alert['ticker']} {alert['alert_type'].split('_')[1]} "
                f"{alert['threshold']}c (now {alert['current_price']}c)"
            )
    return {"updates": updates, "errors": errors, "scanned": len(rows)}


def format_watchlist() -> str:
    """Return a monospace table of the watchlist + current price + position."""
    rows = db.get_watchlist()
    if not rows:
        return "watchlist is empty (use 'add <TICKER> auto|manual')"
    header = f"{'TICKER':<24} {'MODE':<7} {'PRICE':>7} {'POS':>10}"
    out = [header, "-" * len(header)]
    for row in rows:
        ticker = row["ticker"]
        market, _ = kalshi_client.get_market(ticker)
        price = market.get("yes_ask") if market else None
        price_str = f"{price}c" if isinstance(price, int) else "—"
        pos = db.get_position(ticker)
        if pos and pos.get("legs"):
            leg = pos["legs"][0]
            pos_str = f"{leg['side'].upper()}x{leg['contracts']}"
        else:
            pos_str = "—"
        out.append(f"{ticker:<24} {row['mode']:<7} {price_str:>7} {pos_str:>10}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str))


def _cli(argv: list[str]) -> int:
    if not argv:
        _emit({"ok": False, "message": "usage: scanner <scan_all|check|auto|manual|format> [ticker]"})
        return 2
    cmd = argv[0].lower()
    try:
        if cmd in ("scan_all", "scan"):
            result = scan_all()
            return _ok(
                f"scanned {result['scanned']} markets, "
                f"{len(result['updates'])} updates, {len(result['errors'])} errors",
                data=result,
            )
        if cmd == "check":
            if len(argv) < 2:
                return _err("usage: scanner check <ticker>")
            data = check_market(argv[1])
            if data.get("error"):
                return _err(data["error"], data=data)
            return _ok(f"refreshed {argv[1].upper()}", data=data)
        if cmd == "auto":
            if len(argv) < 2:
                return _err("usage: scanner auto <ticker>")
            msg = run_auto_trade(argv[1])
            return _ok(msg)
        if cmd == "manual":
            if len(argv) < 2:
                return _err("usage: scanner manual <ticker>")
            msg = run_manual_update(argv[1])
            return _ok(msg)
        if cmd == "format":
            return _ok(format_watchlist())
        return _err(f"unknown command '{cmd}'")
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("scanner cli error")
        return _err(f"unhandled error: {exc}")


def _ok(message: str, data: Any = None) -> int:
    _emit({"ok": True, "message": message, "data": data})
    return 0


def _err(message: str, data: Any = None) -> int:
    _emit({"ok": False, "message": message, "data": data})
    return 0  # exit 0 so OpenClaw still reads stdout JSON


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
