"""Position-sizing math for the signl bot.

This module is intentionally pure: it takes only numbers and returns only
numbers, with no I/O, no randomness, and no module-level state.  It is
imported by ``scanner.run_auto_trade`` to decide whether and how much to
trade based on the sentiment estimate.

Conventions
-----------
- ``probability`` is an int or float in **0..100** (percent).
- ``yes_price_cents`` is an int in **1..99** (the YES limit price in cents).
- ``balance_cents`` is an int (whole-cent balance).
- Internally, prices and probabilities are converted to decimals in (0, 1)
  before plugging into the Kelly formula.
"""

from __future__ import annotations

from math import floor
from typing import Dict


def _as_fractions(probability: float, yes_price_cents: float) -> tuple[float, float]:
    """Convert spec-shaped inputs to (p, price) decimals in (0, 1)."""
    p = float(probability) / 100.0
    price = float(yes_price_cents) / 100.0
    p = max(0.0, min(1.0, p))
    return p, price


def kelly_criterion(probability: float, yes_price_cents: float) -> float:
    """Return the Kelly-optimal bankroll fraction for a YES contract.

    Formula (per the spec)::

        f = (p * (1/price - 1) - (1 - p)) / (1/price - 1)

    where ``price`` is the decimal price (cents/100).  Result is clamped to
    ``[0.0, 1.0]``.  Returns ``0.0`` for degenerate prices.
    """
    p, price = _as_fractions(probability, yes_price_cents)
    if price <= 0.0 or price >= 1.0:
        return 0.0
    b = (1.0 / price) - 1.0
    if b <= 0.0:
        return 0.0
    f = (p * b - (1.0 - p)) / b
    return max(0.0, min(1.0, f))


def fractional_kelly(
    probability: float,
    yes_price_cents: float,
    fraction: float = 0.25,
) -> float:
    """Return ``fraction`` (default 0.25) of the Kelly-optimal bet size."""
    if fraction <= 0:
        return 0.0
    return float(fraction) * kelly_criterion(probability, yes_price_cents)


def calculate_position_size(
    probability: float,
    yes_price_cents: float,
    balance_cents: int,
    max_position_pct: float = 0.05,
) -> int:
    """Return the integer number of contracts to buy.

    Sizing rule, in order:

    1. Use fractional Kelly (1/4) to compute a target dollar stake.
    2. Cap stake at ``max_position_pct`` of the bankroll (default 5%).
    3. Convert to contracts at ``yes_price_cents``.
    4. Clamp to ``[1, 100]``.
    5. If the bankroll cannot afford a single contract, return ``0``.
    """
    try:
        balance = int(balance_cents)
        price = int(yes_price_cents)
    except (TypeError, ValueError):
        return 0
    if balance <= 0 or price <= 0 or price >= 100:
        return 0

    f = fractional_kelly(probability, price, fraction=0.25)
    cap = max(0.0, float(max_position_pct))
    stake_fraction = min(f, cap)
    if stake_fraction <= 0.0:
        return 0

    stake_cents = stake_fraction * balance
    contracts = floor(stake_cents / price)
    if contracts < 1:
        return 0 if floor(balance / price) < 1 else 1
    return max(1, min(100, int(contracts)))


def should_trade(
    probability: float,
    yes_price_cents: float,
    min_edge: float = 0.05,
) -> bool:
    """Return True iff our estimated edge over the market beats ``min_edge``.

    Market-implied probability is ``yes_price_cents / 100``.  Edge is signed:
    a positive value favours YES, negative favours NO.  This helper returns
    True when ``|edge| > min_edge``, since the caller can then pick the side.
    """
    p, price = _as_fractions(probability, yes_price_cents)
    return abs(p - price) > float(min_edge)


def edge(probability: float, yes_price_cents: float) -> float:
    """Return signed edge (estimated minus implied probability)."""
    p, price = _as_fractions(probability, yes_price_cents)
    return p - price


def trade_summary(
    probability: float,
    yes_price_cents: int,
    balance_cents: int,
    *,
    min_edge: float = 0.05,
    max_position_pct: float = 0.05,
) -> Dict[str, object]:
    """Bundle every sizing decision into one dict for callers and logs."""
    e = edge(probability, yes_price_cents)
    side = "yes" if e > 0 else "no"
    sizing_price = yes_price_cents if side == "yes" else (100 - int(yes_price_cents))
    sizing_prob = probability if side == "yes" else (100 - float(probability))
    contracts = calculate_position_size(
        sizing_prob, sizing_price, balance_cents, max_position_pct=max_position_pct,
    )
    return {
        "side": side,
        "edge": round(e, 4),
        "implied_probability": round(yes_price_cents / 100.0, 4),
        "kelly_fraction": round(kelly_criterion(sizing_prob, sizing_price), 4),
        "fractional_kelly": round(
            fractional_kelly(sizing_prob, sizing_price), 4
        ),
        "contracts": int(contracts),
        "should_trade": should_trade(probability, yes_price_cents, min_edge=min_edge),
        "sizing_price_cents": int(sizing_price),
    }


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    print("kelly(60, 50c)        =", kelly_criterion(60, 50))
    print("frac_kelly(60, 50c)   =", fractional_kelly(60, 50))
    print("size(60, 50c, $100)   =", calculate_position_size(60, 50, 10_000))
    print("should_trade(60, 50)  =", should_trade(60, 50))
    print("trade_summary(70, 40) =", trade_summary(70, 40, 100_000))
