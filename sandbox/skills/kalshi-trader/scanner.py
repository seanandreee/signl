"""Watchlist monitoring loop and per-market pipelines.

Functions return Telegram-ready strings.  Auto-trade output lines are
prefixed with ``[AUTO-TRADE]`` and manual-mode updates are prefixed with
``[MANUAL]`` so the OpenClaw skill can route them to the user channel.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # pragma: no cover
    pass

import db
import guardrails
import kalshi_client
import risk
import sentiment

AUTO_PREFIX = "[AUTO-TRADE]"
MANUAL_PREFIX = "[MANUAL]"
ALERT_PREFIX = "[ALERT]"


def _yes_price(market: Dict[str, Any]) -> Optional[int]:
    """Mid-point YES price from a Kalshi market snapshot.

    Uses (yes_bid + yes_ask) / 2 when both sides are available for an
    accurate edge calculation.  Falls back to last_price, then whichever
    side is present, rather than defaulting to the ask alone.
    """
    bid = market.get("yes_bid")
    ask = market.get("yes_ask")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
        return int(round((bid + ask) / 2))
    for key in ("last_price", "yes_ask", "yes_bid"):
        v = market.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    return None


def check_market(ticker: str) -> Dict[str, Any]:
    """Fetch current market state, refresh DB caches, and fire price alerts.

    Returns ``{"ok": True, "data": market_snapshot, "alerts": [...]}``.
    """
    try:
        ticker = ticker.strip().upper()
        m = kalshi_client.get_market(ticker)
        if not m.get("ok"):
            return {"ok": False, "error": m.get("error", "market lookup failed")}
        market = m["data"]
        db.update_last_checked(ticker)

        cp = _yes_price(market)
        triggered: List[Dict[str, Any]] = []
        if cp is not None:
            db.update_position_price(ticker, cp)
            t = db.get_triggered_alerts(ticker, cp)
            if t.get("ok"):
                triggered = t.get("data", [])
        return {"ok": True, "data": market, "alerts": triggered,
                "yes_price_cents": cp}
    except Exception as exc:
        return {"ok": False, "error": f"check_market failed: {exc}"}


def _format_alert(ticker: str, alert: Dict[str, Any], price: int) -> str:
    direction = "above" if alert["alert_type"] == "price_above" else "below"
    return (
        f"{ALERT_PREFIX} {ticker} {direction} {alert['threshold']}c "
        f"(now {price}c)"
    )


def run_auto_trade(ticker: str, balance_cents: Optional[int] = None) -> str:
    """Full auto-trade pipeline.  Always returns a Telegram-ready string.

    Pass ``balance_cents`` to reuse a balance already fetched by the caller
    (e.g. ``scan_all``); when omitted the function fetches it itself.
    """
    ticker = ticker.strip().upper()
    snap = check_market(ticker)
    if not snap.get("ok"):
        return f"{AUTO_PREFIX} {ticker} skipped: {snap.get('error')}"

    market = snap["data"]
    alert_lines = [_format_alert(ticker, a, snap["yes_price_cents"] or 0)
                   for a in snap.get("alerts", [])]

    full = sentiment.get_full_analysis(ticker)
    if not full.get("ok"):
        msg = (f"{AUTO_PREFIX} {ticker} analysis failed: "
               f"{full.get('error')}")
        return "\n".join([msg] + alert_lines).strip()

    data = full["data"]
    analysis = data["analysis"]
    yes_price = data["yes_price_cents"]
    prob = int(analysis["probability"])
    title = market.get("title") or ticker
    confidence = analysis["confidence"]

    e = risk.edge(prob, yes_price)
    trade_side = "yes" if e > 0 else "no"

    existing = db.get_position(ticker).get("data")
    if existing and existing.get("side") == trade_side:
        out = (
            f"{AUTO_PREFIX} {ticker} SKIP (already holding "
            f"{existing['num_contracts']} {existing['side'].upper()})\n"
            f"  Title: {title}\n"
            f"  YES price: {yes_price}c | Model P(YES): {prob}% | "
            f"edge: {e:+.2f} | conf: {confidence}"
        )
        return "\n".join([out] + alert_lines).strip()

    if not risk.should_trade(prob, yes_price, min_edge=0.05):
        out = (
            f"{AUTO_PREFIX} {ticker} HOLD\n"
            f"  Title: {title}\n"
            f"  YES price: {yes_price}c | Model P(YES): {prob}% | "
            f"edge: {e:+.2f}\n"
            f"  Confidence: {confidence}\n"
            f"  Reasoning: {analysis['reasoning']}"
        )
        return "\n".join([out] + alert_lines).strip()

    if balance_cents is None:
        bal = kalshi_client.get_balance()
        if not bal.get("ok"):
            return (f"{AUTO_PREFIX} {ticker} aborted: balance lookup failed: "
                    f"{bal.get('error')}")
        balance_cents = int(bal["balance_cents"])

    yes_bid = market.get("yes_bid")
    summary = risk.trade_summary(
        prob, yes_price, balance_cents,
        confidence=confidence,
        yes_bid_cents=int(yes_bid) if isinstance(yes_bid, (int, float)) and yes_bid > 0 else None,
    )
    side = summary["side"]
    contracts = int(summary["contracts"])
    sizing_price = int(summary["sizing_price_cents"])

    if contracts <= 0:
        return (f"{AUTO_PREFIX} {ticker} skipped: position sizing returned 0 "
                f"(balance ${balance_cents/100:.2f}, edge "
                f"{summary['edge']:+.2f})")

    allowed, block_reason = guardrails.check_order(
        ticker, contracts=contracts,
        price_cents=sizing_price, balance_cents=balance_cents,
    )
    if not allowed:
        block_msg = (
            f"{AUTO_PREFIX} {ticker} ORDER BLOCKED by NemoClaw guardrails: "
            f"{block_reason}\n"
            f"  Wanted: {contracts} {side.upper()} @ {sizing_price}c | "
            f"Model P(YES): {prob}% | edge: {summary['edge']:+.2f}"
        )
        return "\n".join([block_msg] + alert_lines).strip()

    order = kalshi_client.place_order(
        ticker, side=side, action="buy",
        contracts=contracts, price_cents=sizing_price,
    )

    reason = (
        f"auto: prob={prob}% price={yes_price}c "
        f"edge={summary['edge']:+.2f} kelly={summary['kelly_fraction']}"
    )
    db.log_trade(
        ticker, action="buy", side=side, contracts=contracts,
        price=sizing_price, reason=reason,
        sentiment_score=float(prob), confidence=analysis["confidence"],
    )

    if order.get("ok"):
        db.upsert_position(ticker, side, contracts, sizing_price,
                           current_price=yes_price)
        out = (
            f"{AUTO_PREFIX} {ticker} BOUGHT {contracts} {side.upper()} @ "
            f"{sizing_price}c (order {order.get('order_id')})\n"
            f"  Title: {title}\n"
            f"  Model P(YES): {prob}% | mkt: {yes_price}c | edge: "
            f"{summary['edge']:+.2f} | conf: {analysis['confidence']}\n"
            f"  Reasoning: {analysis['reasoning']}"
        )
    else:
        out = (
            f"{AUTO_PREFIX} {ticker} ORDER FAILED ({contracts} {side.upper()} @ "
            f"{sizing_price}c): {order.get('error')}\n"
            f"  Model P(YES): {prob}% | mkt: {yes_price}c | edge: "
            f"{summary['edge']:+.2f}"
        )

    return "\n".join([out] + alert_lines).strip()


def run_manual_update(ticker: str) -> str:
    """Manual-mode pipeline: analysis with no order.  Telegram-ready string."""
    ticker = ticker.strip().upper()
    snap = check_market(ticker)
    alert_lines: List[str] = []
    if snap.get("ok"):
        alert_lines = [_format_alert(ticker, a, snap["yes_price_cents"] or 0)
                       for a in snap.get("alerts", [])]

    full = sentiment.get_full_analysis(ticker)
    if not full.get("ok"):
        return (f"{MANUAL_PREFIX} {ticker} analysis failed: "
                f"{full.get('error')}")

    data = full["data"]
    market = data["market"]
    analysis = data["analysis"]
    yes_price = data["yes_price_cents"]
    prob = int(analysis["probability"])
    title = market.get("title") or ticker
    headlines = data.get("headlines", []) or []

    headline_block = (
        "\n".join(f"  - {h.get('title', '')[:140]}" for h in headlines[:5])
        or "  (no recent headlines)"
    )
    factors = analysis.get("key_factors") or []
    factor_block = (
        "\n".join(f"  - {f}" for f in factors)
        or "  (none)"
    )

    pos = db.get_position(ticker).get("data")
    pos_line = "(no open position)"
    if pos:
        pos_line = (
            f"open: {pos['num_contracts']} {pos['side'].upper()} @ "
            f"{pos['entry_price']}c (cur {pos.get('current_price') or yes_price}c, "
            f"pnl {pos.get('pnl') or 0}c)"
        )

    text = (
        f"{MANUAL_PREFIX} {ticker} update\n"
        f"  Title: {title}\n"
        f"  YES bid/ask: {market.get('yes_bid')}/{market.get('yes_ask')} | "
        f"vol: {market.get('volume')}\n"
        f"  Model P(YES): {prob}% | edge: "
        f"{risk.edge(prob, yes_price):+.2f} | conf: {analysis['confidence']}\n"
        f"  Recommended: {analysis['recommended_action']}\n"
        f"  Reasoning: {analysis['reasoning']}\n"
        f"  Key factors:\n{factor_block}\n"
        f"  Recent news:\n{headline_block}\n"
        f"  Position: {pos_line}\n"
        f"  Reply with: buy <n> {ticker} <yes|no> at <price> "
        "/ sell <n> {ticker} at <price> / hold"
    ).format(ticker=ticker)

    return "\n".join([text] + alert_lines).strip()


def scan_all() -> List[str]:
    """Iterate the watchlist and dispatch per mode.  Returns one message
    per market (auto markets emit ``[AUTO-TRADE]``; manual markets emit
    ``[MANUAL]``).

    Balance is fetched once for the entire scan cycle so that multiple
    auto-trades in a single pass don't each see the pre-trade balance.
    """
    out: List[str] = []
    wl = db.get_watchlist()
    if not wl.get("ok"):
        return [f"scan_all: {wl.get('error')}"]

    shared_balance_cents: Optional[int] = None
    bal = kalshi_client.get_balance()
    if bal.get("ok"):
        shared_balance_cents = int(bal["balance_cents"])

    for row in wl["data"]:
        ticker = row["ticker"]
        mode = row["mode"]
        try:
            if mode == "auto":
                out.append(run_auto_trade(ticker,
                                          balance_cents=shared_balance_cents))
            else:
                out.append(run_manual_update(ticker))
        except Exception as exc:
            out.append(f"[ERROR] {ticker} ({mode}): {exc}")
    if not out:
        out.append("(watchlist is empty)")
    return out


def format_watchlist() -> str:
    """Return a human-readable watchlist table for Telegram."""
    wl = db.get_watchlist()
    if not wl.get("ok"):
        return f"watchlist: {wl.get('error')}"
    rows = wl["data"]
    if not rows:
        return "Watchlist is empty.  Add markets with: add <TICKER> auto|manual"

    pos_data = db.get_positions().get("data", [])
    pos_by_ticker = {p["ticker"]: p for p in pos_data}

    header = f"{'TICKER':<24} {'MODE':<6} {'YES':>5} {'POS':>16}"
    lines = [header, "-" * len(header)]
    for r in rows:
        ticker = r["ticker"]
        mode = r["mode"]
        try:
            m = kalshi_client.get_market(ticker)
            if m.get("ok"):
                yes = m["data"].get("yes_ask") or m["data"].get("last_price") or "-"
            else:
                yes = "?"
        except Exception:
            yes = "?"
        p = pos_by_ticker.get(ticker)
        pos_str = (
            f"{p['num_contracts']}{p['side'][0].upper()}@{p['entry_price']}c"
            if p else "-"
        )
        lines.append(f"{ticker:<24} {mode:<6} {str(yes):>5} {pos_str:>16}")
    return "\n".join(lines)


def _cli() -> None:  # pragma: no cover - manual entrypoint
    parser = argparse.ArgumentParser(description="signl scanner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan_all")
    p_check = sub.add_parser("check"); p_check.add_argument("ticker")
    p_auto = sub.add_parser("auto"); p_auto.add_argument("ticker")
    p_man = sub.add_parser("manual"); p_man.add_argument("ticker")
    sub.add_parser("watchlist")
    ns = parser.parse_args()

    if ns.cmd == "scan_all":
        for line in scan_all():
            print(line)
            print()
    elif ns.cmd == "check":
        print(check_market(ns.ticker))
    elif ns.cmd == "auto":
        print(run_auto_trade(ns.ticker))
    elif ns.cmd == "manual":
        print(run_manual_update(ns.ticker))
    elif ns.cmd == "watchlist":
        print(format_watchlist())


if __name__ == "__main__":
    _cli()
