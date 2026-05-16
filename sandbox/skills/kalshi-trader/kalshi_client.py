"""Kalshi REST wrapper for the signl trading bot (RSA-signed auth).

Targets the demo API at ``https://demo-api.kalshi.co/trade-api/v2`` using
the modern v2 authentication scheme: every request carries three headers
that prove possession of an RSA private key registered with Kalshi.

Required headers per request
----------------------------
::

    KALSHI-ACCESS-KEY:       <api_key_id>          # the public ID Kalshi issued
    KALSHI-ACCESS-TIMESTAMP: <unix_milliseconds>   # current time in ms
    KALSHI-ACCESS-SIGNATURE: base64(rsa_pss(sha256(msg)))

where ``msg = f"{timestamp_ms}{METHOD}{path}"``.  ``path`` is the URL path
component only (no query string), e.g. ``/trade-api/v2/markets``.

Configuration (env or .env)
---------------------------
- ``KALSHI_API_KEY_ID``     - the access-key UUID Kalshi issues (required).
- ``KALSHI_PRIVATE_KEY_PATH`` - filesystem path to the RSA private key in
  PEM format (preferred).
- ``KALSHI_PRIVATE_KEY_PEM`` - inline PEM (newlines as ``\\n`` literals or
  real newlines).  Used only when ``KALSHI_PRIVATE_KEY_PATH`` is unset.
- ``KALSHI_PRIVATE_KEY_PASSWORD`` - optional passphrase if the PEM is
  encrypted.

Error policy
------------
Every public function returns ``{"ok": True, ...}`` on success or
``{"ok": False, "error": "..."}`` on failure.  ``_request`` retries up to
3 times with exponential backoff on 429/5xx and raises :class:`KalshiError`
on 401/403/404 and other non-retryable HTTP errors.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass


BASE_URL = os.environ.get(
    "KALSHI_BASE_URL", "https://demo-api.kalshi.co/trade-api/v2"
)
DEFAULT_TIMEOUT = 15
MAX_RETRIES = 3


class KalshiError(RuntimeError):
    """Raised for non-retryable API failures (401/403/404 etc.)."""


_session = requests.Session()
_state: Dict[str, Any] = {
    "api_key_id": None,
    "private_key": None,   # cryptography RSAPrivateKey
}


def _load_private_key():
    """Load the RSA private key from disk or env into a cryptography object."""
    try:
        from cryptography.hazmat.primitives import serialization  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise KalshiError(
            "the 'cryptography' package is required for RSA-signed Kalshi "
            f"auth (pip install cryptography): {exc}"
        )

    password = os.environ.get("KALSHI_PRIVATE_KEY_PASSWORD")
    password_bytes = password.encode("utf-8") if password else None

    pem_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if pem_path:
        path = Path(os.path.expanduser(pem_path))
        if not path.exists():
            raise KalshiError(
                f"KALSHI_PRIVATE_KEY_PATH={pem_path!s} does not exist"
            )
        pem_bytes = path.read_bytes()
    else:
        inline = os.environ.get("KALSHI_PRIVATE_KEY_PEM")
        if not inline:
            raise KalshiError(
                "no Kalshi private key configured: set KALSHI_PRIVATE_KEY_PATH "
                "(preferred) or KALSHI_PRIVATE_KEY_PEM in the environment"
            )
        # Allow `\n` escape sequences in inline env var (common pattern).
        pem_bytes = inline.replace("\\n", "\n").encode("utf-8")

    try:
        return serialization.load_pem_private_key(
            pem_bytes, password=password_bytes,
        )
    except Exception as exc:
        raise KalshiError(f"could not parse Kalshi private key: {exc}")


def _ensure_credentials() -> None:
    """Populate ``_state`` from env on first use."""
    if _state["private_key"] is not None and _state["api_key_id"]:
        return
    api_key_id = os.environ.get("KALSHI_API_KEY_ID")
    if not api_key_id:
        raise KalshiError(
            "KALSHI_API_KEY_ID is not set in the environment"
        )
    _state["api_key_id"] = api_key_id
    _state["private_key"] = _load_private_key()


def _sign(timestamp_ms: int, method: str, path: str) -> str:
    """Return the base64 RSA-PSS-SHA256 signature for the given request."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization  # type: ignore
        from cryptography.hazmat.primitives.asymmetric import padding  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise KalshiError(
            f"the 'cryptography' package is required: {exc}"
        )
    _ = serialization  # silence linters; only imported for type-checking
    message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
    sig = _state["private_key"].sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


def _signed_headers(method: str, path: str) -> Dict[str, str]:
    """Return the three Kalshi auth headers for a single request."""
    _ensure_credentials()
    ts = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": _state["api_key_id"],
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": _sign(int(ts), method, path),
        "Accept": "application/json",
    }


def _request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    auth: bool = True,
) -> Dict[str, Any]:
    """Perform an HTTP request with retry/backoff and standardised errors.

    ``path`` is the API path *including* the ``/trade-api/v2`` prefix part
    that comes after :data:`BASE_URL`'s host.  The signature is computed
    over the path component of the full URL (no query string).
    """
    url = f"{BASE_URL}{path}"
    sign_path = urlparse(url).path
    headers: Dict[str, str] = {"Accept": "application/json"}
    if auth:
        headers.update(_signed_headers(method, sign_path))
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = _session.request(
                method.upper(),
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(min(2 ** attempt, 4))
            continue

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1") or 1)
            time.sleep(min(retry_after, 5) + attempt)
            continue
        if 500 <= resp.status_code < 600:
            time.sleep(min(2 ** attempt, 4))
            continue
        if resp.status_code in (401, 403, 404):
            raise KalshiError(
                f"{resp.status_code} {resp.reason}: "
                f"{method.upper()} {path} -> {resp.text[:300]}"
            )
        if not resp.ok:
            raise KalshiError(
                f"{resp.status_code} {resp.reason}: "
                f"{method.upper()} {path} -> {resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError:
            return {"_raw": resp.text}

    raise KalshiError(
        f"request failed after {MAX_RETRIES} attempts: "
        f"{method.upper()} {path} ({last_exc})"
    )


def login(
    api_key_id: Optional[str] = None,
    private_key_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate Kalshi credentials by performing one signed request.

    The legacy email/password flow is gone; this helper is kept so the
    rest of the codebase (and any operator scripts) can still call
    ``login()`` to verify configuration is correct.  Pass ``api_key_id``
    and ``private_key_path`` to override the env vars for one process.
    """
    try:
        if api_key_id:
            os.environ["KALSHI_API_KEY_ID"] = api_key_id
        if private_key_path:
            os.environ["KALSHI_PRIVATE_KEY_PATH"] = private_key_path
        _state["api_key_id"] = None
        _state["private_key"] = None
        _ensure_credentials()
        body = _request("GET", "/exchange/status")
        return {"ok": True, "msg": "kalshi credentials verified",
                "exchange_status": body}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"login failed: {exc}"}


def _normalise_market(m: Dict[str, Any]) -> Dict[str, Any]:
    """Project the Kalshi market shape onto the fields signl cares about."""
    return {
        "ticker": m.get("ticker"),
        "title": m.get("title") or m.get("subtitle") or "",
        "yes_bid": m.get("yes_bid"),
        "yes_ask": m.get("yes_ask"),
        "no_bid": m.get("no_bid"),
        "no_ask": m.get("no_ask"),
        "volume": m.get("volume"),
        "close_time": m.get("close_time"),
        "status": m.get("status"),
        "last_price": m.get("last_price"),
    }


def get_market(ticker: str) -> Dict[str, Any]:
    """Return normalised market detail for ``ticker``."""
    try:
        ticker = ticker.strip().upper()
        body = _request("GET", f"/markets/{ticker}")
        market = body.get("market") or {}
        if not market:
            return {"ok": False, "error": f"market not found: {ticker}"}
        return {"ok": True, "data": _normalise_market(market)}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"get_market failed: {exc}"}


def get_markets_by_keyword(keyword: str, limit: int = 50) -> Dict[str, Any]:
    """Search open markets and return those whose title/ticker contain
    ``keyword`` (case-insensitive).
    """
    try:
        kw = keyword.strip().lower()
        body = _request(
            "GET", "/markets",
            params={"status": "open", "limit": int(limit)},
        )
        all_markets = body.get("markets") or []
        matches = [
            _normalise_market(m) for m in all_markets
            if kw in (m.get("title") or "").lower()
            or kw in (m.get("ticker") or "").lower()
            or kw in (m.get("subtitle") or "").lower()
        ]
        return {"ok": True, "data": matches}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"get_markets_by_keyword failed: {exc}"}


def get_all_open_markets(limit: int = 200) -> Dict[str, Any]:
    """Return open markets sorted by descending 24h volume."""
    try:
        body = _request(
            "GET", "/markets",
            params={"status": "open", "limit": int(limit)},
        )
        markets = [_normalise_market(m) for m in (body.get("markets") or [])]
        markets.sort(key=lambda m: (m.get("volume") or 0), reverse=True)
        return {"ok": True, "data": markets}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"get_all_open_markets failed: {exc}"}


def get_my_positions() -> Dict[str, Any]:
    """Return the current portfolio positions reported by Kalshi."""
    try:
        body = _request("GET", "/portfolio/positions")
        return {"ok": True,
                "data": body.get("market_positions")
                        or body.get("positions") or []}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"get_my_positions failed: {exc}"}


def get_balance() -> Dict[str, Any]:
    """Return the user's available balance (in cents)."""
    try:
        body = _request("GET", "/portfolio/balance")
        balance = body.get("balance")
        if balance is None:
            return {"ok": False,
                    "error": f"balance response missing 'balance': {body}"}
        return {"ok": True, "balance_cents": int(balance), "data": body}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"get_balance failed: {exc}"}


def place_order(
    ticker: str,
    side: str,
    action: str,
    contracts: int,
    price_cents: int,
) -> Dict[str, Any]:
    """Place a limit order.

    ``side`` is ``yes``/``no``; ``action`` is ``buy``/``sell``;
    ``price_cents`` is 1..99.
    """
    try:
        ticker = ticker.strip().upper()
        side = side.strip().lower()
        action = action.strip().lower()
        if side not in ("yes", "no"):
            return {"ok": False, "error": "side must be 'yes' or 'no'"}
        if action not in ("buy", "sell"):
            return {"ok": False, "error": "action must be 'buy' or 'sell'"}
        contracts = int(contracts)
        price_cents = int(price_cents)
        if contracts <= 0:
            return {"ok": False, "error": "contracts must be > 0"}
        if not 1 <= price_cents <= 99:
            return {"ok": False, "error": "price_cents must be 1..99"}

        payload: Dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": side,
            "action": action,
            "type": "limit",
            "count": contracts,
        }
        if side == "yes":
            payload["yes_price"] = price_cents
        else:
            payload["no_price"] = price_cents

        body = _request("POST", "/portfolio/orders", json_body=payload)
        order = body.get("order") or {}
        order_id = order.get("order_id") or body.get("order_id")
        return {"ok": True, "order_id": order_id, "data": order or body}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"place_order failed: {exc}"}


def cancel_order(order_id: str) -> Dict[str, Any]:
    """Cancel a resting order by id."""
    try:
        body = _request("DELETE", f"/portfolio/orders/{order_id}")
        return {"ok": True, "data": body}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"cancel_order failed: {exc}"}


def get_order_status(order_id: str) -> Dict[str, Any]:
    """Return the current status of ``order_id``."""
    try:
        body = _request("GET", f"/portfolio/orders/{order_id}")
        order = body.get("order") or body
        return {"ok": True, "status": order.get("status"), "data": order}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"get_order_status failed: {exc}"}


def get_market_history(
    ticker: str,
    *,
    limit: int = 100,
    period_interval: int = 60,
) -> Dict[str, Any]:
    """Return recent candles / trades for ``ticker``.

    ``period_interval`` is in minutes per candle.  Falls back to the
    ``/trades`` endpoint if the candlestick endpoint is unavailable.
    """
    try:
        ticker = ticker.strip().upper()
        try:
            body = _request(
                "GET", f"/markets/{ticker}/candlesticks",
                params={"limit": int(limit),
                        "period_interval": int(period_interval)},
            )
            return {"ok": True,
                    "data": body.get("candlesticks") or body.get("history") or body}
        except KalshiError:
            body = _request(
                "GET", "/markets/trades",
                params={"ticker": ticker, "limit": int(limit)},
            )
            return {"ok": True, "data": body.get("trades") or body}
    except KalshiError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"get_market_history failed: {exc}"}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import argparse, pprint

    p = argparse.ArgumentParser(description="Kalshi REST helper CLI (RSA auth)")
    p.add_argument("cmd", choices=[
        "verify", "market", "search", "open", "positions", "balance",
        "place", "cancel", "status", "history",
    ])
    p.add_argument("args", nargs="*")
    ns = p.parse_args()

    if ns.cmd == "verify":
        pprint.pprint(login())
    elif ns.cmd == "market":
        pprint.pprint(get_market(ns.args[0]))
    elif ns.cmd == "search":
        pprint.pprint(get_markets_by_keyword(ns.args[0]))
    elif ns.cmd == "open":
        pprint.pprint(get_all_open_markets())
    elif ns.cmd == "positions":
        pprint.pprint(get_my_positions())
    elif ns.cmd == "balance":
        pprint.pprint(get_balance())
    elif ns.cmd == "place":
        ticker, side, action, contracts, price = ns.args
        pprint.pprint(
            place_order(ticker, side, action, int(contracts), int(price))
        )
    elif ns.cmd == "cancel":
        pprint.pprint(cancel_order(ns.args[0]))
    elif ns.cmd == "status":
        pprint.pprint(get_order_status(ns.args[0]))
    elif ns.cmd == "history":
        pprint.pprint(get_market_history(ns.args[0]))
