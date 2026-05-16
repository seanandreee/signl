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
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
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
    """Return the base64 RSA-PSS signature for the given request."""
    key = _load_private_key()
    message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
    signed = key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
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
    key_id = os.environ.get("KALSHI_KEY_ID")
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
    """Return the available balance for the authenticated account (in cents)."""
    try:
        body = _request("GET", "/portfolio/balance")
    except KalshiError as exc:
        return None, str(exc)
    if not isinstance(body, dict):
        return None, "unexpected balance response"
    return {
        "balance_cents": body.get("balance"),
        "payout": body.get("payout"),
    }, None


def place_order(
    ticker: str,
    side: str,
    action: str,
    contracts: int,
    price_cents: int,
    *,
    client_order_id: str | None = None,
    order_type: str = "limit",
    time_in_force: str = "good_till_cancel",
) -> tuple[dict[str, Any] | None, str | None]:
    """Place a limit order. ``side`` is yes/no, ``action`` is buy/sell."""
    ticker = ticker.strip().upper()
    side = side.strip().lower()
    action = action.strip().lower()
    if side not in ("yes", "no"):
        return None, f"invalid side '{side}' (yes|no)"
    if action not in ("buy", "sell"):
        return None, f"invalid action '{action}' (buy|sell)"
    try:
        contracts = int(contracts)
        price_cents = int(price_cents)
    except (TypeError, ValueError):
        return None, "contracts and price_cents must be integers"
    if contracts <= 0:
        return None, "contracts must be positive"
    if not 1 <= price_cents <= 99:
        return None, f"price_cents {price_cents} out of range (1-99)"

    payload: dict[str, Any] = {
        "ticker": ticker,
        "side": side,
        "action": action,
        "count": contracts,
        "type": order_type,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id or f"signl-{int(time.time()*1000)}",
    }
    # Kalshi expects yes_price or no_price depending on side.
    if side == "yes":
        payload["yes_price"] = price_cents
    else:
        payload["no_price"] = price_cents
    try:
        body = _request("POST", "/portfolio/orders", json_body=payload)
    except KalshiError as exc:
        return None, str(exc)
    order = body.get("order") if isinstance(body, dict) else None
    if not order:
        return None, f"order response missing 'order': {body}"
    return {
        "order_id": order.get("order_id"),
        "status": order.get("status"),
        "ticker": order.get("ticker"),
        "side": order.get("side"),
        "action": order.get("action"),
        "count": order.get("count"),
        "yes_price": order.get("yes_price"),
        "no_price": order.get("no_price"),
    }, None


def cancel_order(order_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Cancel an open order by id."""
    order_id = (order_id or "").strip()
    if not order_id:
        return None, "order_id is required"
    try:
        body = _request("DELETE", f"/portfolio/orders/{order_id}")
    except KalshiError as exc:
        return None, str(exc)
    return body if isinstance(body, dict) else {"raw": body}, None


def get_order_status(order_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch the latest status of an order."""
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


def _normalise_market(m: dict[str, Any]) -> dict[str, Any]:
    """Pluck the fields the rest of the bot relies on into a stable shape."""
    return {
        "ticker": m.get("ticker"),
        "title": m.get("title") or m.get("subtitle"),
        "yes_bid": m.get("yes_bid"),
        "yes_ask": m.get("yes_ask"),
        "no_bid": m.get("no_bid"),
        "no_ask": m.get("no_ask"),
        "last_price": m.get("last_price"),
        "volume": m.get("volume"),
        "volume_24h": m.get("volume_24h"),
        "open_interest": m.get("open_interest"),
        "close_time": m.get("close_time"),
        "status": m.get("status"),
        "category": m.get("category"),
        "subtitle": m.get("subtitle"),
    }
