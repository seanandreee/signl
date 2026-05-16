"""News + LLM sentiment pipeline for the signl bot.

The pipeline has three stages:

1. :func:`search_news` - hits the Brave Search API (the web-search egress
   already provisioned by OpenClaw via the ``brave`` policy preset) and
   returns up to 10 recent headlines about the market topic.
2. :func:`analyze_sentiment` - sends the headlines plus market context to
   NVIDIA Nemotron via the OpenAI-compatible endpoint and asks for a
   strict-JSON response with probability / confidence / action / reasoning.
3. :func:`get_full_analysis` - orchestrates the two above plus a Kalshi
   market fetch and returns one merged report.

Every function returns a dict ``{"ok": bool, ...}`` so callers (notably
``scanner.py`` and ``watchlist.handle_command``) can format the error text
straight to Telegram.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # pragma: no cover
    pass

import kalshi_client


NEMOTRON_MODEL = os.environ.get(
    "NEMOTRON_MODEL", "nvidia/nemotron-3-super-120b-a12b"
)
NVIDIA_BASE_URL = os.environ.get(
    "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
)
BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"
BRAVE_WEB_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_TIMEOUT = 10
LLM_TIMEOUT = 60


SYSTEM_PROMPT = (
    "You are a quantitative analyst evaluating Kalshi prediction markets. "
    "You will be given a market title, ticker, the current YES limit price "
    "in cents (1-99, where each cent = 1% implied probability), the market "
    "resolution deadline, and a list of recent news headlines. Estimate the "
    "true probability that the market resolves YES, and recommend an action.\n\n"
    "Weight news recency relative to the time horizon: headlines from the "
    "last 24 hours matter most for markets resolving within days; older "
    "headlines may be more relevant for longer-horizon markets.\n\n"
    "You MUST respond with a single valid JSON object and NOTHING ELSE. "
    "No prose, no markdown fences, no preamble. The schema is:\n"
    '{\n'
    '  "probability": <integer 0-100, your estimate of P(YES resolves)>,\n'
    '  "confidence": "low" | "medium" | "high",\n'
    '  "key_factors": [<3-5 short strings describing the drivers>],\n'
    '  "recommended_action": "buy_yes" | "buy_no" | "hold",\n'
    '  "reasoning": "<2-3 sentences explaining the estimate>"\n'
    "}\n\n"
    "Use buy_yes when your probability is materially above the implied "
    "price; buy_no when materially below; hold when within 5 percentage "
    "points of the market."
)


def _brave_get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        raise RuntimeError("BRAVE_API_KEY is not set in the environment")
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }
    resp = requests.get(url, params=params, headers=headers, timeout=BRAVE_TIMEOUT)
    if not resp.ok:
        raise RuntimeError(
            f"brave search {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()


def search_news(market_title: str, count: int = 10) -> Dict[str, Any]:
    """Return up to ``count`` recent headlines relevant to ``market_title``.

    Tries the Brave news endpoint first; falls back to the generic web
    search endpoint when news has zero hits.  Each item is a dict with
    ``title``, ``description``, ``url``, ``age``, ``source``.
    """
    try:
        query = (market_title or "").strip()
        if not query:
            return {"ok": False, "error": "market_title is required"}

        results: List[Dict[str, Any]] = []
        try:
            news = _brave_get(
                BRAVE_NEWS_URL,
                {"q": query, "count": int(count), "freshness": "pw"},
            )
            for item in (news.get("results") or [])[: int(count)]:
                results.append({
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "url": item.get("url", ""),
                    "age": item.get("age", ""),
                    "source": (item.get("meta_url") or {}).get("hostname", ""),
                })
        except Exception:
            results = []

        if not results:
            web = _brave_get(
                BRAVE_WEB_URL, {"q": query, "count": int(count)},
            )
            for item in ((web.get("web") or {}).get("results") or [])[: int(count)]:
                results.append({
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "url": item.get("url", ""),
                    "age": item.get("age", ""),
                    "source": (item.get("meta_url") or {}).get("hostname", ""),
                })

        return {"ok": True, "data": results}
    except Exception as exc:
        return {"ok": False, "error": f"search_news failed: {exc}"}


def _strip_json(raw: str) -> str:
    """Strip leading/trailing fences/preamble from an LLM JSON reply."""
    s = (raw or "").strip()
    if s.startswith("```"):
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start : end + 1]
    return s


def _validate_analysis(parsed: Any) -> Dict[str, Any]:
    """Coerce + validate the Nemotron response.  Returns the cleaned dict
    or raises ``ValueError`` on schema violations."""
    if not isinstance(parsed, dict):
        raise ValueError("response is not a JSON object")
    try:
        prob = int(round(float(parsed["probability"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"missing/invalid probability: {exc}")
    prob = max(0, min(100, prob))

    confidence = str(parsed.get("confidence", "")).strip().lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "medium"

    factors = parsed.get("key_factors") or []
    if not isinstance(factors, list):
        factors = [str(factors)]
    factors = [str(f).strip() for f in factors if str(f).strip()][:6]

    action = str(parsed.get("recommended_action", "")).strip().lower()
    if action not in ("buy_yes", "buy_no", "hold"):
        action = "hold"

    reasoning = str(parsed.get("reasoning", "")).strip()
    if not reasoning:
        reasoning = "(no reasoning provided)"

    return {
        "probability": prob,
        "confidence": confidence,
        "key_factors": factors,
        "recommended_action": action,
        "reasoning": reasoning,
    }


def _call_nemotron(messages: List[Dict[str, str]]) -> str:
    """Call the NVIDIA OpenAI-compatible endpoint and return the raw content."""
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError("NVIDIA_API_KEY is not set in the environment")
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            f"openai package is required (pip install openai): {exc}"
        )
    client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=api_key, timeout=LLM_TIMEOUT)
    resp = client.chat.completions.create(
        model=NEMOTRON_MODEL,
        messages=messages,
        temperature=0.2,
        top_p=0.9,
        max_tokens=900,
    )
    return resp.choices[0].message.content or ""


_AGE_PATTERNS = [
    (re.compile(r"(\d+)\s+hour", re.I), "hours"),
    (re.compile(r"(\d+)\s+day", re.I), "days"),
    (re.compile(r"(\d+)\s+week", re.I), "weeks"),
    (re.compile(r"(\d+)\s+month", re.I), "months"),
]


def _headline_age_hours(age_str: str) -> Optional[float]:
    """Parse a Brave 'age' string like '2 hours ago' into hours.  Returns None
    if unparseable."""
    s = (age_str or "").strip()
    for pattern, unit in _AGE_PATTERNS:
        m = pattern.search(s)
        if m:
            n = int(m.group(1))
            if unit == "hours":
                return float(n)
            if unit == "days":
                return float(n * 24)
            if unit == "weeks":
                return float(n * 24 * 7)
            if unit == "months":
                return float(n * 24 * 30)
    return None


def _filter_headlines(
    headlines: List[Dict[str, Any]],
    close_time: Optional[str],
) -> List[Dict[str, Any]]:
    """Drop headlines that are too stale relative to the market horizon.

    Markets resolving within 7 days: drop anything older than 48 hours.
    Markets resolving beyond 7 days (or unknown horizon): drop anything older
    than 7 days.
    """
    now = datetime.now(timezone.utc)
    if close_time:
        try:
            ct = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            horizon_days = (ct - now).total_seconds() / 86400
        except Exception:
            horizon_days = 30.0
    else:
        horizon_days = 30.0

    max_age_hours = 48.0 if horizon_days <= 7 else 168.0

    filtered = []
    for h in headlines:
        age_h = _headline_age_hours(h.get("age", ""))
        if age_h is None or age_h <= max_age_hours:
            filtered.append(h)
    return filtered


def analyze_sentiment(
    market_title: str,
    market_ticker: str,
    yes_price_cents: int,
    headlines: List[Dict[str, Any]],
    close_time: Optional[str] = None,
) -> Dict[str, Any]:
    """Send context to Nemotron and return a validated analysis dict.

    ``close_time`` is an ISO-8601 string of when the market resolves; it is
    injected into the prompt so the model can weight recency appropriately.

    Returns ``{"ok": True, "data": {probability, confidence, key_factors,
    recommended_action, reasoning}}`` on success.
    """
    try:
        if not market_title:
            return {"ok": False, "error": "market_title is required"}
        try:
            yes_price = int(yes_price_cents)
        except (TypeError, ValueError):
            return {"ok": False, "error": "yes_price_cents must be int"}

        fresh = _filter_headlines(headlines or [], close_time)

        headlines_block = "\n".join(
            f"- {h.get('title', '')} | {h.get('source', '')} | {h.get('age', '')}"
            f"\n    {h.get('description', '')[:240]}"
            for h in fresh[:10]
        ) or "(no recent headlines found)"

        close_line = (
            f"Market resolves: {close_time}\n" if close_time else ""
        )

        user = (
            f"Market title: {market_title}\n"
            f"Ticker: {market_ticker}\n"
            f"Current YES price: {yes_price}c "
            f"(implied probability {yes_price}%)\n"
            f"{close_line}"
            f"\nRecent headlines:\n{headlines_block}\n\n"
            "Respond with the JSON object only."
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

        last_err: Optional[str] = None
        for attempt in range(2):
            try:
                raw = _call_nemotron(messages)
            except Exception as exc:
                last_err = f"LLM call failed: {exc}"
                continue
            try:
                parsed = json.loads(_strip_json(raw))
            except json.JSONDecodeError as exc:
                last_err = f"LLM returned non-JSON: {exc}; raw={raw[:200]!r}"
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": "That was not valid JSON. Respond again with the "
                               "JSON object only, no fences, no commentary.",
                })
                continue
            try:
                clean = _validate_analysis(parsed)
            except ValueError as exc:
                last_err = f"schema violation: {exc}"
                continue
            return {"ok": True, "data": clean}

        return {"ok": False, "error": last_err or "unknown LLM error"}
    except Exception as exc:
        return {"ok": False, "error": f"analyze_sentiment failed: {exc}"}


def get_full_analysis(ticker: str) -> Dict[str, Any]:
    """End-to-end: market lookup + news search + Nemotron analysis.

    Returns ``{"ok": True, "data": {market, headlines, analysis}}`` so the
    caller can render any subset.
    """
    try:
        ticker = (ticker or "").strip().upper()
        if not ticker:
            return {"ok": False, "error": "ticker is required"}

        m = kalshi_client.get_market(ticker)
        if not m.get("ok"):
            return {"ok": False, "error": m.get("error", "market lookup failed")}
        market = m["data"]
        title = market.get("title") or ticker
        yes_ask = market.get("yes_ask")
        yes_bid = market.get("yes_bid")
        if yes_ask and yes_bid:
            yes_price = int(round((yes_bid + yes_ask) / 2))
        elif yes_ask or yes_bid:
            yes_price = yes_ask or yes_bid
        else:
            return {"ok": False,
                    "error": "market has no YES bid or ask price — cannot analyse"}
        close_time: Optional[str] = market.get("close_time")

        n = search_news(title)
        headlines = n.get("data", []) if n.get("ok") else []
        news_error = None if n.get("ok") else n.get("error")

        a = analyze_sentiment(title, ticker, int(yes_price), headlines,
                              close_time=close_time)
        if not a.get("ok"):
            return {
                "ok": False,
                "error": a.get("error", "sentiment failed"),
                "market": market,
                "headlines": headlines,
                "news_error": news_error,
            }

        return {
            "ok": True,
            "data": {
                "market": market,
                "headlines": headlines,
                "analysis": a["data"],
                "yes_price_cents": int(yes_price),
                "news_error": news_error,
            },
        }
    except Exception as exc:
        return {"ok": False, "error": f"get_full_analysis failed: {exc}"}


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import argparse, pprint

    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["news", "analyze", "full"])
    p.add_argument("arg")
    p.add_argument("--price", type=int, default=50)
    ns = p.parse_args()

    if ns.cmd == "news":
        pprint.pprint(search_news(ns.arg))
    elif ns.cmd == "analyze":
        n = search_news(ns.arg).get("data", [])
        pprint.pprint(analyze_sentiment(ns.arg, "TEST", ns.price, n))
    else:
        pprint.pprint(get_full_analysis(ns.arg))
