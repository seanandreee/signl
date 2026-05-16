"""Position-sizing math for the signl bot. Pure functions, no I/O.

All prices in this module are in **cents (1-99)**, all balances in cents,
all probabilities in the **0-1** range unless otherwise noted.

The Kelly fraction below is the standard binary-bet form expressed in terms
of decimal price (cents / 100):

    f* = (p * b - q) / b      where  b = 1/price - 1,  q = 1 - p

Buying YES at price ``c`` (cents) wins ``1 - c`` cents per contract on a YES
resolution and loses ``c`` cents on a NO resolution, so the odds-against
ratio is exactly ``1/price - 1``.
"""

from __future__ import annotations

import math


def _validate(probability: float, yes_price_cents: int) -> tuple[float, float]:
    """Coerce inputs and return ``(p, price_decimal)``.

    Raises ``ValueError`` so the (small number of) callers can decide how to
    surface the error. The higher-level wrappers (scanner, watchlist) catch
    these and turn them into ``(ok, message)`` tuples.
    """
    try:
        p = float(probability)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"probability must be numeric, got {probability!r}") from exc
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"probability must be in [0,1], got {p}")
    try:
        c = int(yes_price_cents)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"yes_price_cents must be int, got {yes_price_cents!r}"
        ) from exc
    if not 1 <= c <= 99:
        raise ValueError(f"yes_price_cents must be in [1,99], got {c}")
    return p, c / 100.0


def kelly_criterion(probability: float, yes_price_cents: int) -> float:
    """Return the full-Kelly fraction of bankroll to bet on YES.

    Formula: ``f = (p * (1/price - 1) - (1 - p)) / (1/price - 1)``.
    Returns 0 when the bet has no edge (negative Kelly is clamped to 0).
    """
    p, price = _validate(probability, yes_price_cents)
    b = 1.0 / price - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - p
    f = (p * b - q) / b
    return max(0.0, f)


def fractional_kelly(
    probability: float, yes_price_cents: int, fraction: float = 0.25
) -> float:
    """Return ``fraction * kelly_criterion(...)``; defaults to quarter-Kelly."""
    if fraction < 0:
        raise ValueError(f"fraction must be non-negative, got {fraction}")
    return fraction * kelly_criterion(probability, yes_price_cents)


def calculate_position_size(
    probability: float,
    yes_price_cents: int,
    balance_cents: int,
    max_position_pct: float = 0.05,
    kelly_fraction: float = 0.25,
    min_contracts: int = 1,
    max_contracts: int = 100,
) -> int:
    """Return the number of contracts to buy under quarter-Kelly + cap.

    The dollar amount is the smaller of (a) fractional Kelly on bankroll
    and (b) ``max_position_pct`` of bankroll. Contract count is then
    ``floor(amount / price)`` and clamped to ``[min_contracts, max_contracts]``.
    Returns 0 when there's no positive edge or the balance is non-positive.
    """
    try:
        balance = int(balance_cents)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"balance_cents must be int, got {balance_cents!r}") from exc
    if balance <= 0:
        return 0
    if not 0 <= max_position_pct <= 1:
        raise ValueError(
            f"max_position_pct must be in [0,1], got {max_position_pct}"
        )
    try:
        kelly_fraction_f = float(kelly_fraction)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"kelly_fraction must be numeric, got {kelly_fraction!r}"
        ) from exc

    kelly_pct = fractional_kelly(probability, yes_price_cents, kelly_fraction_f)
    if kelly_pct <= 0:
        return 0
    capped_pct = min(kelly_pct, max_position_pct)
    spend_cents = capped_pct * balance
    contracts = int(math.floor(spend_cents / yes_price_cents))
    if contracts <= 0:
        return 0
    return max(min_contracts, min(max_contracts, contracts))


def should_trade(
    probability: float, yes_price_cents: int, min_edge: float = 0.05
) -> tuple[bool, float, str]:
    """Decide whether the estimated edge over the market warrants action.

    Returns ``(go, edge, side)`` where:

    * ``edge`` is ``probability - (yes_price_cents / 100)`` — positive means
      YES is underpriced, negative means YES is overpriced (i.e. NO is the
      better side).
    * ``side`` is ``"yes"`` / ``"no"`` / ``"hold"``.
    * ``go`` is True iff ``abs(edge) >= min_edge``.
    """
    p, price = _validate(probability, yes_price_cents)
    if min_edge < 0:
        raise ValueError(f"min_edge must be non-negative, got {min_edge}")
    edge = p - price
    if edge >= min_edge:
        return True, edge, "yes"
    if -edge >= min_edge:
        return True, edge, "no"
    return False, edge, "hold"
