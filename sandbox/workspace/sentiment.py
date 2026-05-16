"""News + LLM sentiment pipeline for the signl bot.

* Headlines come from the Brave Search API (``api.search.brave.com``).
* Reasoning comes from NVIDIA Nemotron-3-Super served via the
  OpenAI-compatible endpoint at ``integrate.api.nvidia.com``.

The Nemotron response is forced into a strict JSON shape — we never trust
free-form text and always degrade gracefully into a "low confidence / hold"
fallback so the scanner can keep running.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

import kalshi_client

log = logging.getLogger("signl.sentiment")

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NEMOTRON_MODEL = os.environ.get(
    "NEMOTRON_MODEL", "nvidia/nemotron-3-super-120b-a12b"
)

VALID_ACTIONS = ("buy_yes", "buy_no", "hold")
VALID_CONFIDENCE = ("low", "medium", "high")

SYSTEM_PROMPT = """You are a disciplined probabilistic forecaster working on \
Kalshi binary prediction markets. Given a market question, the current YES \
price (in cents from 1-99, which the market implies as the probability), and \
recent news headlines, estimate the true probability the market resolves YES.

You MUST respond with ONLY a single JSON object, no preamble, no commentary, \
no markdown fences. The JSON object MUST have exactly these keys:

{
  "probability": <integer 0-100, your true estimate of P(YES)>,
  "confidence": "low" | "medium" | "high",
  "key_factors": [<short strings, 2-5 items>],
  "recommended_action": "buy_yes" | "buy_no" | "hold",
  "reasoning": "<2-3 sentence rationale>"
}

Rules:
- "buy_yes" only if your probability is meaningfully higher than the market price.
- "buy_no"  only if your probability is meaningfully lower than the market price.
- Otherwise "hold".
- Pick "high" confidence only when headlines clearly point one way.
- Never include any text outside the JSON object."""


# ---------------------------------------------------------------------------
# Brave Search
# ---------------------------------------------------------------------------


def search_news(market_title: str, count: int = 10) -> list[dict[str, Any]]:
    """Return up to ``count`` recent web results for ``market_title``.

    Falls back to an empty list on any error (missing key, network issue,
    rate limit) so analysis can proceed without news.
    """
    market_title = (market_title or "").strip()
    if not market_title:
        return []
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        log.warning("BRAVE_API_KEY not set; returning no headlines")
        return []
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {
        "q": market_title,
        "count": max(1, min(int(count), 20)),
        "freshness": "pw",  # past week
        "text_decorations": "false",
        "safesearch": "moderate",
    }
    try:
        resp = requests.get(
            BRAVE_ENDPOINT, headers=headers, params=params, timeout=10
        )
    except requests.RequestException as exc:
        log.warning("brave search network error: %s", exc)
        return []
    if resp.status_code != 200:
        log.warning(
            "brave search HTTP %s: %s", resp.status_code, (resp.text or "")[:200]
        )
        return []
    try:
        data = resp.json()
    except ValueError:
        log.warning("brave search returned non-JSON body")
        return []
    web = (data.get("web") or {}).get("results") or []
    out: list[dict[str, Any]] = []
    for r in web[: int(count)]:
        out.append(
            {
                "title": _clean(r.get("title")),
                "description": _clean(r.get("description")),
                "url": r.get("url"),
                "age": r.get("age") or r.get("page_age"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Nemotron analysis
# ---------------------------------------------------------------------------


def analyze_sentiment(
    market_title: str,
    market_ticker: str,
    yes_price_cents: int | None,
    headlines: list[dict[str, Any]],
) -> dict[str, Any]:
    """Send the prompt to Nemotron and return the parsed/validated JSON dict.

    Always returns a dict with the canonical keys. On any error the dict has
    ``probability=None``, ``confidence="low"``, ``recommended_action="hold"``,
    and ``reasoning`` describes the failure.
    """
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        return _fallback("NVIDIA_API_KEY not set")
    try:
        from openai import OpenAI  # imported lazily so tests don't need it
    except ImportError as exc:
        return _fallback(f"openai package missing: {exc}")

    user_payload = {
        "market_ticker": market_ticker,
        "market_title": market_title,
        "yes_price_cents": yes_price_cents,
        "yes_price_implied_probability": (
            round(yes_price_cents / 100, 4) if yes_price_cents else None
        ),
        "headlines": headlines or [],
    }
    user_msg = (
        "Market context (as JSON):\n```json\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
        + "\n```\n\nReturn ONLY the JSON object as specified."
    )

    client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key)
    try:
        completion = client.chat.completions.create(
            model=NEMOTRON_MODEL,
            temperature=0.2,
            max_tokens=600,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
        )
    except TypeError:
        # Older openai clients don't accept response_format; retry without it.
        try:
            completion = client.chat.completions.create(
                model=NEMOTRON_MODEL,
                temperature=0.2,
                max_tokens=600,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
        except Exception as exc:  # pragma: no cover - network errors
            return _fallback(f"nemotron error: {exc}")
    except Exception as exc:  # pragma: no cover - network errors
        return _fallback(f"nemotron error: {exc}")

    try:
        raw = completion.choices[0].message.content or ""
    except (AttributeError, IndexError):
        return _fallback("empty Nemotron completion")
    parsed = _parse_json(raw)
    if parsed is None:
        return _fallback(
            f"could not parse Nemotron JSON; first 200 chars: {raw[:200]!r}"
        )
    return _validate(parsed)


def get_full_analysis(ticker: str) -> tuple[dict[str, Any] | None, str | None]:
    """One-shot: market -> headlines -> sentiment, returned as a single dict."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None, "ticker is required"
    market, err = kalshi_client.get_market(ticker)
    if err:
        return None, err
    title = market.get("title") or market.get("subtitle") or ticker
    yes_ask = market.get("yes_ask")
    headlines = search_news(title)
    sentiment = analyze_sentiment(title, ticker, yes_ask, headlines)
    return {
        "ticker": ticker,
        "market": market,
        "headlines": headlines,
        "sentiment": sentiment,
    }, None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _parse_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON parsing: strip code fences, find the first object."""
    text = (raw or "").strip()
    text = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Last resort: find the first {...} block.
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def _validate(obj: dict[str, Any]) -> dict[str, Any]:
    """Coerce/clamp Nemotron's response into the canonical schema."""
    prob = obj.get("probability")
    try:
        prob_int = int(round(float(prob))) if prob is not None else None
    except (TypeError, ValueError):
        prob_int = None
    if prob_int is not None:
        prob_int = max(0, min(100, prob_int))

    confidence = str(obj.get("confidence", "")).strip().lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    action = str(obj.get("recommended_action", "")).strip().lower()
    if action not in VALID_ACTIONS:
        action = "hold"

    factors = obj.get("key_factors") or []
    if isinstance(factors, str):
        factors = [factors]
    factors = [str(f).strip() for f in factors if str(f).strip()][:8]

    reasoning = str(obj.get("reasoning", "")).strip() or "(no reasoning provided)"

    return {
        "probability": prob_int,
        "confidence": confidence,
        "key_factors": factors,
        "recommended_action": action,
        "reasoning": reasoning,
    }


def _fallback(reason: str) -> dict[str, Any]:
    log.warning("sentiment fallback: %s", reason)
    return {
        "probability": None,
        "confidence": "low",
        "key_factors": [],
        "recommended_action": "hold",
        "reasoning": f"sentiment unavailable: {reason}",
    }


def _clean(text: str | None) -> str:
    """Strip HTML-ish tags Brave occasionally leaves in titles/descriptions."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()
