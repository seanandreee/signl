"""NemoClaw guardrails for the signl Kalshi trading bot.

Hard limits enforced before every place_order() call:
  - MAX_CONTRACTS: max 100 contracts per order
  - MAX_BALANCE_PCT: max 5% of balance per trade (cost = contracts * price_cents)
  - MAX_OPEN_POSITIONS: max 10 concurrent open positions

All blocked actions are appended to guardrails.log with a UTC timestamp and reason.
Returns (True, None) if the order passes, or (False, "reason string") if blocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

LOG_PATH = Path(__file__).parent / "guardrails.log"

MAX_CONTRACTS = 100
MAX_BALANCE_PCT = 0.05       # 5 %
MAX_OPEN_POSITIONS = 10


def _log_blocked(ticker: str, contracts: int, price_cents: int, reason: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = (
        f"[{ts}] BLOCKED ticker={ticker} contracts={contracts} "
        f"price={price_cents}c | reason: {reason}\n"
    )
    with open(LOG_PATH, "a") as fh:
        fh.write(line)


def _count_open_positions() -> int:
    try:
        import db
        result = db.get_positions()
        if result.get("ok"):
            return len(result.get("data", []))
    except Exception:
        pass
    return 0


def check_order(
    ticker: str,
    contracts: int,
    price_cents: int,
    balance_cents: int,
) -> Tuple[bool, Optional[str]]:
    """Validate an order against all hard limits before it is placed."""
    ticker = str(ticker).strip().upper()

    if contracts > MAX_CONTRACTS:
        reason = f"contracts {contracts} exceeds max {MAX_CONTRACTS}"
        _log_blocked(ticker, contracts, price_cents, reason)
        return False, reason

    cost_cents = contracts * price_cents
    max_cost = int(balance_cents * MAX_BALANCE_PCT)
    if cost_cents > max_cost:
        pct = (cost_cents / balance_cents * 100) if balance_cents > 0 else float("inf")
        reason = (
            f"trade cost {cost_cents}c ({pct:.1f}% of balance) exceeds "
            f"{MAX_BALANCE_PCT * 100:.0f}% cap ({max_cost}c)"
        )
        _log_blocked(ticker, contracts, price_cents, reason)
        return False, reason

    open_positions = _count_open_positions()
    if open_positions >= MAX_OPEN_POSITIONS:
        reason = (
            f"open position count {open_positions} at or above "
            f"max {MAX_OPEN_POSITIONS}"
        )
        _log_blocked(ticker, contracts, price_cents, reason)
        return False, reason

    return True, None


if __name__ == "__main__":
    print("=== guardrails.py self-test ===\n")

    cases = [
        ("KXBTC-25DEC",  10,  45, 100_000, "normal trade — should pass"),
        ("KXBTC-25DEC", 101,  45, 100_000, "over contract limit (101 > 100)"),
        ("KXBTC-25DEC",  50,  90,  10_000, "over 5% balance (50*90=4500c > 500c cap)"),
        ("KXBTC-25DEC",   1,  50,      40, "tiny balance, cost > 5% (50c > 2c cap)"),
        ("KXETH-JUN",     5,  60,  50_000, "normal small trade — should pass"),
    ]

    for ticker, contracts, price, balance, label in cases:
        ok, reason = check_order(ticker, contracts, price, balance)
        status = "ALLOW" if ok else f"BLOCK  ({reason})"
        cost = contracts * price
        cap = int(balance * MAX_BALANCE_PCT)
        print(f"  {label}")
        print(f"    ticker={ticker} contracts={contracts} price={price}c "
              f"balance=${balance/100:.2f} cost={cost}c cap={cap}c")
        print(f"    => {status}\n")

    print(f"Log written to: {LOG_PATH}")
