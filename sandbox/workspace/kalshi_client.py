"""Thin REST client for the Kalshi v2 trading API.

Uses RSA-PSS (SHA-256, MGF1-SHA256, max salt length) signed requests with
the three Kalshi headers:

* ``KALSHI-ACCESS-KEY``       – the API key id
* ``KALSHI-ACCESS-TIMESTAMP`` – current time in milliseconds
* ``KALSHI-ACCESS-SIGNATURE`` – base64(RSA-PSS(SHA256, ts + method + path))

Demo environment is the default; override ``KALSHI_API_BASE`` to point at
``trading.kalshi.com/trade-api/v2`` for production.

Every public function returns ``(data, None)`` on success or
``(None, "human readable error")`` on failure so the OpenClaw skill can
forward the message straight to Telegram without catching exceptions.
"""

from __future__ import annotations

import base64
import logging
import os
import random
import time
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import requests
from cryptography.exceptions import InvalidSignature  # noqa: F401  (re-exported)
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger("signl.kalshi")

DEFAULT_BASE = "https://demo-api.kalshi.co/trade-api/v2"
BASE_URL = os.environ.get("KALSHI_API_BASE", DEFAULT_BASE).rstrip("/")

_SIGNED_PATH_PREFIX = urlsplit(BASE_URL).path or "/trade-api/v2"

DEFAULT_TIMEOUT = 15  # seconds
MAX_RETRIES = 3


class KalshiError(RuntimeError):
    """Raised internally for Kalshi API errors; callers see the str(message)."""


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_private_key() -> rsa.RSAPrivateKey:
    """Load the RSA private key from ``KALSHI_PRIVATE_KEY_PATH``.

    Cached so we don't re-parse PEM on every request.
    """
    path = os.environ.get(
        "KALSHI_PRIVATE_KEY_PATH",
        "/home/ubuntu/kalshi-signl/kalshi-key.pem",
    ).strip()
    if not path:
        raise KalshiError("KALSHI_PRIVATE_KEY_PATH not set")
    try:
        with open(path, "rb") as fh:
            pem = fh.read()
    except OSError as exc:
        raise KalshiError(f"could not read private key at {path}: {exc}") from exc
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise KalshiError(f"invalid PEM private key at {path}: {exc}") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise KalshiError("Kalshi requires an RSA private key")
    return key


def _signature(method: str, path: str, timestamp_ms: str) -> str:
    """Return the base64 RSA-PSS signature for the given request.

    Kalshi's reference implementation uses ``salt_length = DIGEST_LENGTH``
    (32 bytes for SHA-256). PSS verification at Kalshi accepts any valid
    salt length, but we mirror the reference to avoid surprises.
    """
    key = _load_private_key()
    message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
    signed = key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signed).decode("ascii")


def sign_request(method: str, path: str) -> dict[str, str]:
    """Build the three KALSHI-ACCESS-* headers for a method/path pair.

    ``path`` must be the API path the server will see (e.g.
    ``/trade-api/v2/markets/AAPL-2026``), not just the suffix relative
    to ``BASE_URL``.
    """
    key_id = os.environ.get("KALSHI_KEY_ID", "").strip()
    if not key_id:
        raise KalshiError("KALSHI_KEY_ID not set")
    ts = str(int(time.time() * 1000))
    sig = _signature(method, path, ts)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------


def _full_url(suffix: str) -> tuple[str, str]:
    """Map a path suffix (``/markets/X``) to a full URL and the signed path."""
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    signed_path = _SIGNED_PATH_PREFIX + suffix
    return BASE_URL + suffix, signed_path


def _request(
    method: str,
    suffix: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Send a signed request, retry on 429/5xx, raise on hard errors."""
    url, signed_path = _full_url(suffix)
    last_err: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            headers = sign_request(method, signed_path)
        except KalshiError:
            raise
        headers["Accept"] = "application/json"
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_err = f"network error: {exc}"
            log.warning("kalshi %s %s attempt %d: %s", method, url, attempt, last_err)
            if attempt < MAX_RETRIES:
                time.sleep((2 ** (attempt - 1)) + random.random())
                continue
            raise KalshiError(last_err) from exc
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            last_err = f"HTTP {resp.status_code}: {_safe_body(resp)}"
            log.warning(
                "kalshi %s %s attempt %d: %s", method, url, attempt, last_err
            )
            if attempt < MAX_RETRIES:
                time.sleep((2 ** (attempt - 1)) + random.random())
                continue
            raise KalshiError(last_err)
        if resp.status_code in (401, 403):
            raise KalshiError(
                f"Kalshi auth failed (HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        if resp.status_code == 404:
            raise KalshiError(f"Kalshi resource not found (HTTP 404): {url}")
        if resp.status_code >= 400:
            raise KalshiError(
                f"Kalshi rejected request (HTTP {resp.status_code}): {_safe_body(resp)}"
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise KalshiError(f"non-JSON response from {url}: {exc}") from exc
    raise KalshiError(last_err or "exhausted retries")


def _safe_body(resp: requests.Response) -> str:
    try:
        text = resp.text
    except Exception:  # pragma: no cover - defensive
        return "<unreadable body>"
    return (text or "")[:400]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_market(ticker: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return market details for ``ticker``.

    Returns ``({title, yes_bid, yes_ask, no_bid, no_ask, volume, close_time, status, ...}, None)``
    on success, ``(None, err)`` on failure.
    """
    ticker = ticker.strip().upper()
    if not ticker:
        return None, "ticker is required"
    try:
        body = _request("GET", f"/markets/{ticker}")
        market = body.get("market") if isinstance(body, dict) else None
        if not market:
            return None, f"market {ticker} not returned by Kalshi"
        return _normalise_market(market), None
    except KalshiError as exc:
        return None, str(exc)


def get_markets_by_keyword(
    keyword: str, status: str = "open", limit: int = 50
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Search markets whose title or ticker contains ``keyword``."""
    keyword = (keyword or "").strip()
    if not keyword:
        return None, "keyword is required"
    try:
        body = _request(
            "GET",
            "/markets",
            params={"status": status, "limit": min(max(int(limit), 1), 200)},
        )
    except KalshiError as exc:
        return None, str(exc)
    markets = body.get("markets", []) if isinstance(body, dict) else []
    needle = keyword.lower()
    matches = [
        _normalise_market(m)
        for m in markets
        if needle in (m.get("title") or "").lower()
        or needle in (m.get("ticker") or "").lower()
        or needle in (m.get("subtitle") or "").lower()
    ]
    return matches, None


def get_all_open_markets(
    limit: int = 100,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return open markets sorted by 24h volume (highest first)."""
    try:
        body = _request(
            "GET",
            "/markets",
            params={"status": "open", "limit": min(max(int(limit), 1), 200)},
        )
    except KalshiError as exc:
        return None, str(exc)
    markets = body.get("markets", []) if isinstance(body, dict) else []
    normalised = [_normalise_market(m) for m in markets]
    normalised.sort(key=lambda m: m.get("volume_24h") or m.get("volume") or 0, reverse=True)
    return normalised, None


def get_my_positions() -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return current portfolio positions from Kalshi."""
    try:
        body = _request("GET", "/portfolio/positions")
    except KalshiError as exc:
        return None, str(exc)
    if not isinstance(body, dict):
        return [], None
    out: list[dict[str, Any]] = []
    for p in body.get("market_positions", []) or []:
        out.append(
            {
                "ticker": p.get("ticker"),
                "position": p.get("position"),
                "market_exposure": p.get("market_exposure"),
                "realised_pnl": p.get("realized_pnl"),
                "fees_paid": p.get("fees_paid"),
                "resting_orders_count": p.get("resting_orders_count"),
            }
        )
    return out, None


def get_balance() -> tuple[dict[str, Any] | None, str | None]:
    """Return the available balance for the authenticated account (in cents).

    Kalshi now returns both ``balance`` (integer cents, legacy) and
    ``balance_dollars`` (fixed-point dollar string, e.g. ``"12.3400"``).
    We prefer ``balance_dollars`` when present and convert to cents for
    consistency with the rest of the bot, falling back to ``balance``.
    """
    try:
        body = _request("GET", "/portfolio/balance")
    except KalshiError as exc:
        return None, str(exc)
    if not isinstance(body, dict):
        return None, "unexpected balance response"
    dollars = body.get("balance_dollars")
    cents: int | None = None
    if dollars not in (None, ""):
        cents = _dollars_to_cents(dollars)
    if cents is None and body.get("balance") is not None:
        cents = int(body["balance"])
    return {
        "balance_cents": cents,
        "balance_dollars": dollars,
        "portfolio_value": body.get("portfolio_value"),
        "payout": body.get("payout"),
    }, None


def _cents_to_dollar_str(cents: int) -> str:
    """Format an integer-cents price as a 4-decimal dollar string (Kalshi V2)."""
    return f"{cents / 100:.4f}"


def place_order(
    ticker: str,
    side: str,
    action: str,
    contracts: int,
    price_cents: int,
    *,
    client_order_id: str | None = None,
    time_in_force: str = "good_till_canceled",
    self_trade_prevention_type: str = "taker_at_cross",
) -> tuple[dict[str, Any] | None, str | None]:
    """Place a limit order via the V2 ``/portfolio/events/orders`` endpoint.

    The bot's public surface still speaks in ``side ∈ {yes, no}`` and
    ``action ∈ {buy, sell}`` with integer-cent prices, which we translate
    to V2's YES-side book quoting:

    * ``buy yes  @ Nc`` -> bid at ``"0.NN00"``     (buy YES)
    * ``sell yes @ Nc`` -> ask at ``"0.NN00"``     (sell YES)
    * ``buy no  @ Nc`` -> ask at ``"0.{100-N}00"`` (selling YES at the complementary price)
    * ``sell no @ Nc`` -> bid at ``"0.{100-N}00"`` (buying YES at the complementary price)

    Legacy ``/portfolio/orders`` was deprecated May 6, 2026.
    """
    ticker = ticker.strip().upper()
    side = side.strip().lower()
    action = action.strip().lower()
    if side not in ("yes", "no"):
        return None, f"invalid side '{side}' (yes|no)"
    if action not in ("buy", "sell"):
        return None, f"invalid action '{action}' (buy|sell)"
    if time_in_force not in (
        "good_till_canceled",
        "fill_or_kill",
        "immediate_or_cancel",
    ):
        return None, f"invalid time_in_force '{time_in_force}'"
    if self_trade_prevention_type not in ("taker_at_cross", "maker"):
        return None, f"invalid self_trade_prevention_type '{self_trade_prevention_type}'"
    try:
        contracts = int(contracts)
        price_cents = int(price_cents)
    except (TypeError, ValueError):
        return None, "contracts and price_cents must be integers"
    if contracts <= 0:
        return None, "contracts must be positive"
    if not 1 <= price_cents <= 99:
        return None, f"price_cents {price_cents} out of range (1-99)"

    # Translate (side, action) -> V2 book_side + YES-quoted price.
    is_yes_direction = (side == "yes" and action == "buy") or (
        side == "no" and action == "sell"
    )
    book_side = "bid" if is_yes_direction else "ask"
    if side == "yes":
        yes_price_cents = price_cents
    else:
        yes_price_cents = 100 - price_cents

    payload: dict[str, Any] = {
        "ticker": ticker,
        "client_order_id": client_order_id or f"signl-{int(time.time() * 1000)}",
        "side": book_side,
        "count": f"{contracts}.00",
        "price": _cents_to_dollar_str(yes_price_cents),
        "time_in_force": time_in_force,
        "self_trade_prevention_type": self_trade_prevention_type,
    }
    try:
        body = _request("POST", "/portfolio/events/orders", json_body=payload)
    except KalshiError as exc:
        return None, str(exc)
    if not isinstance(body, dict):
        return None, f"unexpected order response: {body!r}"
    fill_count = _dollars_to_cents(body.get("fill_count")) or 0
    remaining = _dollars_to_cents(body.get("remaining_count")) or 0
    # ``_dollars_to_cents`` multiplies by 100; counts already have ≤2 decimals,
    # so this matches Kalshi's centicount convention exactly.
    avg_fill = body.get("average_fill_price")
    return {
        "order_id": body.get("order_id"),
        "client_order_id": body.get("client_order_id"),
        "ticker": ticker,
        "requested_side": side,
        "requested_action": action,
        "requested_contracts": contracts,
        "requested_price_cents": price_cents,
        "book_side": book_side,
        "yes_price_cents_sent": yes_price_cents,
        "fill_centicount": fill_count,
        "remaining_centicount": remaining,
        "average_fill_price_dollars": avg_fill,
        "ts_ms": body.get("ts_ms"),
        "status": "filled" if remaining == 0 and fill_count > 0 else "resting",
    }, None


def cancel_order(order_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Cancel an open order by id (V2 ``/portfolio/events/orders/{id}``)."""
    order_id = (order_id or "").strip()
    if not order_id:
        return None, "order_id is required"
    try:
        body = _request("DELETE", f"/portfolio/events/orders/{order_id}")
    except KalshiError as exc:
        return None, str(exc)
    return body if isinstance(body, dict) else {"raw": body}, None


def get_order_status(order_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch the latest status of an order.

    Read endpoint; the legacy path is still the canonical one for single-order
    reads as of 2026-05.
    """
    order_id = (order_id or "").strip()
    if not order_id:
        return None, "order_id is required"
    try:
        body = _request("GET", f"/portfolio/orders/{order_id}")
    except KalshiError as exc:
        return None, str(exc)
    order = body.get("order") if isinstance(body, dict) else None
    if not order:
        return None, f"order {order_id} not found"
    return order, None


def get_market_history(
    ticker: str, limit: int = 60
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return recent candlestick history for ``ticker`` (default last 60 buckets)."""
    ticker = ticker.strip().upper()
    if not ticker:
        return None, "ticker is required"
    try:
        body = _request(
            "GET",
            f"/markets/{ticker}/candlesticks",
            params={"period_interval": 1, "limit": int(limit)},
        )
    except KalshiError as exc:
        return None, str(exc)
    candles = body.get("candlesticks", []) if isinstance(body, dict) else []
    return candles, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dollars_to_cents(value: Any) -> int | None:
    """Convert a Kalshi *_dollars field (str like "0.0800" or float) to int cents.

    Kalshi production migrated read endpoints to dollar-denominated strings
    (e.g. ``yes_ask_dollars: "0.0800"``). The rest of the bot still works in
    integer cents 1-99, so we round to the nearest cent on the way in.
    Returns ``None`` for missing/empty/unparseable values.
    """
    if value is None or value == "":
        return None
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def _first_present(m: dict[str, Any], *keys: str) -> Any:
    """Return the first key in ``m`` that has a non-None, non-empty value."""
    for k in keys:
        v = m.get(k)
        if v not in (None, ""):
            return v
    return None


def _normalise_market(m: dict[str, Any]) -> dict[str, Any]:
    """Pluck the fields the rest of the bot relies on into a stable shape.

    Handles both Kalshi's current ``*_dollars`` / ``*_fp`` schema and the
    legacy integer-cents schema, normalising everything to integer cents for
    prices and integer counts for volume / open interest.
    """
    yes_ask_d = _first_present(m, "yes_ask_dollars")
    yes_bid_d = _first_present(m, "yes_bid_dollars")
    no_ask_d = _first_present(m, "no_ask_dollars")
    no_bid_d = _first_present(m, "no_bid_dollars")
    last_d = _first_present(m, "last_price_dollars")
    vol_fp = _first_present(m, "volume_fp", "volume")
    vol24_fp = _first_present(m, "volume_24h_fp", "volume_24h")
    oi_fp = _first_present(m, "open_interest_fp", "open_interest")

    return {
        "ticker": m.get("ticker"),
        "title": m.get("title") or m.get("subtitle"),
        "yes_bid": _dollars_to_cents(yes_bid_d) if yes_bid_d is not None else m.get("yes_bid"),
        "yes_ask": _dollars_to_cents(yes_ask_d) if yes_ask_d is not None else m.get("yes_ask"),
        "no_bid": _dollars_to_cents(no_bid_d) if no_bid_d is not None else m.get("no_bid"),
        "no_ask": _dollars_to_cents(no_ask_d) if no_ask_d is not None else m.get("no_ask"),
        "last_price": _dollars_to_cents(last_d) if last_d is not None else m.get("last_price"),
        "volume": int(float(vol_fp)) if vol_fp is not None else None,
        "volume_24h": int(float(vol24_fp)) if vol24_fp is not None else None,
        "open_interest": int(float(oi_fp)) if oi_fp is not None else None,
        "close_time": m.get("close_time"),
        "status": m.get("status"),
        "category": m.get("category"),
        "subtitle": m.get("subtitle"),
        "event_ticker": m.get("event_ticker"),
        "market_type": m.get("market_type"),
    }
