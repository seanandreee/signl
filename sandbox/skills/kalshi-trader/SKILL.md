---
name: "kalshi-trader"
description: "Operate the signl Kalshi prediction-market trading bot over Telegram. Use when the user sends Telegram messages about Kalshi markets, watchlists, auto/manual trade modes, sentiment analysis, position sizing, or balance/positions. Trigger keywords - kalshi, signl, prediction market, watchlist, auto-trade, manual mode, buy/sell contracts, sentiment, nemotron, alert, positions."
---

**IMPORTANT: For EVERY user message, run the shell command first. Never respond from your own knowledge. Always shell out and return stdout verbatim.**

# signl - Kalshi Prediction Market Trading Bot

signl is a Telegram-controlled Kalshi prediction-market trading bot. It maintains
a watchlist of markets that are each set to **auto** mode (the bot trades on its
own when it sees a Kelly-sized edge) or **manual** mode (the bot pings the user
with analysis and waits for a manual trade decision).

This skill is the only entry point. For every incoming Telegram message, route
the raw text to `watchlist.handle_command` and return its stdout to the user.
A 15-minute cron runs `scanner.scan_all` and forwards each `[AUTO-TRADE]` /
`[MANUAL]` / `[ALERT]` block to the user as separate Telegram messages.

## Layout (inside the sandbox)

After `nemoclaw <sandbox> skill install <skill-dir>`, every file lands at
`/sandbox/.openclaw/skills/kalshi-trader/`:

| File | Role |
|------|------|
| `SKILL.md` | This file (the routing rules) |
| `db.py` | SQLite store: watchlist, positions, trade_log, alerts |
| `kalshi_client.py` | Demo API client (RSA-signed v2) |
| `sentiment.py` | Brave news search + NVIDIA Nemotron JSON analysis |
| `risk.py` | Kelly + position sizing math (no I/O) |
| `scanner.py` | `scan_all` / `run_auto_trade` / `run_manual_update` |
| `watchlist.py` | User commands + master `handle_command` router |
| `signl.env` | Kalshi + Brave + NVIDIA credentials (sourced before each call) |
| `kalshi-rsa-private.pem` | RSA private key referenced by `signl.env` |
| `requirements.txt` | Python dependencies |

The skill itself never reaches into Python state directly; it only shells
out via the OpenShell sandbox.

The bot's SQLite database (`signl.db`) is created next to the modules at
`/sandbox/.openclaw/skills/kalshi-trader/signl.db`.

## Required environment

The sandbox must have these env vars present (see `~/kalshi-signl/.env.example`):

- `KALSHI_API_KEY_ID` - public UUID Kalshi issues alongside the keypair
- `KALSHI_PRIVATE_KEY_PATH` - path to the PEM-encoded RSA private key (preferred), or
  `KALSHI_PRIVATE_KEY_PEM` - inline PEM with `\n` newlines
- `KALSHI_PRIVATE_KEY_PASSWORD` - optional, only if the PEM is encrypted
- `NVIDIA_API_KEY` - integrate.api.nvidia.com bearer for Nemotron
- `BRAVE_API_KEY` - search.brave.com news search (preset `brave` already wires this)
- `TELEGRAM_BOT_TOKEN` - injected by the OpenClaw Telegram bridge

Authentication is RSA-PSS-SHA256: every Kalshi request carries
`KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, and
`KALSHI-ACCESS-SIGNATURE` headers signed over `f"{ts_ms}{METHOD}{path}"`.
There is no token to refresh and no login state to cache.

The OpenShell policy preset at `~/kalshi-signl/policies/kalshi.yaml` allows
`python3` to reach `demo-api.kalshi.co`, `trading.kalshi.com`,
`integrate.api.nvidia.com`, `api.search.brave.com`, and `api.telegram.org`,
plus read/write inside `~/kalshi-signl/sandbox/workspace/`.

## Telegram command catalog

Every command below is a literal Telegram message. Leading `/` is optional and
matching is case-insensitive. The router is in `watchlist.handle_command`.

| Telegram message | Effect |
|------------------|--------|
| `add KXMARKET auto` | Add market in auto-trade mode |
| `add KXMARKET manual` | Add market in manual-update mode |
| `remove KXMARKET` | Soft-delete from watchlist |
| `set KXMARKET to auto` / `set KXMARKET to manual` | Switch mode |
| `watchlist` / `show watchlist` | Render watchlist table with prices + positions |
| `positions` | Show open positions with current PnL |
| `balance` | Show Kalshi balance |
| `buy 5 KXMARKET yes at 42` | Place limit buy: 5 contracts YES at 42c |
| `sell 5 KXMARKET at 60` | Sell 5 contracts of the open side at 60c |
| `alert KXMARKET above 70` | Fire one-shot alert when YES >= 70c |
| `alert KXMARKET below 30` | Fire one-shot alert when YES <= 30c |
| `analyze KXMARKET` | Run a full sentiment + market report on demand |
| `scan` | Manually trigger `scanner.scan_all` |
| `help` | Show the command list |

## Routing rule (every inbound Telegram message)

For every message received from the user (excluding the bot's own messages):

1. Treat the raw message text as a single positional argument and shell out:

   ```bash
   SKILL_DIR=/sandbox/.openclaw/skills/kalshi-trader
   cd "$SKILL_DIR" \
     && set -a && . "$SKILL_DIR/signl.env" && set +a \
     && python3 -m watchlist handle "$MESSAGE"
   ```

   The `handle` subcommand joins all positional arguments with spaces, and
   reads stdin if none are given.

2. Take the entire stdout of that command and post it back to Telegram as a
   single message. Do not edit, summarise, or wrap the output in extra prose.

3. If stdout is empty, post `(no output)`. If stderr is non-empty or the exit
   code is non-zero, post `error: <stderr or exit code>` so the user can debug.

## Scheduled scan (cron, every 15 minutes)

Install this cron once during onboarding (runs inside the sandbox):

```cron
*/15 * * * * cd /sandbox/.openclaw/skills/kalshi-trader \
  && set -a && . /sandbox/.openclaw/skills/kalshi-trader/signl.env && set +a \
  && /usr/bin/python3 -m scanner scan_all 2>&1
```

Each invocation prints one block per watchlist market separated by a blank
line. Split the output on blank-line boundaries and route each non-empty block
as follows:

- Block starts with `[AUTO-TRADE]` -> push to Telegram immediately. These are
  auto-mode results: a trade was attempted (success or failure), held by
  Kelly sizing, or skipped. Always notify.
- Block starts with `[MANUAL]` -> push to Telegram immediately. The user is
  expected to reply with `buy ...` / `sell ...` / `hold` / `analyze ...`.
- Block starts with `[ALERT]` -> push to Telegram immediately (price-alert
  trip from `db.get_triggered_alerts`).
- Block starts with `[ERROR]` -> push to Telegram immediately so the user
  knows the loop failed for that ticker.

Auto-trades are always announced. Manual updates are also announced
proactively, on every scan tick, regardless of whether the model recommends
buy/sell/hold - users want the regular pulse.

## On-demand commands the user might issue back

Most user replies are routed exactly like inbound commands above. A few
patterns benefit from proactive follow-up by the skill:

- After `analyze KXMARKET` the user may follow up with `buy ...` or
  `sell ...` - the router handles these directly; no skill-level state needed.
- After an `[AUTO-TRADE]` notification the user may say `cancel <order-id>` -
  shell out to `python3 -c "import kalshi_client, sys; print(kalshi_client.cancel_order(sys.argv[1]))" "<order-id>"`.

## First-run setup checklist (one-shot, run on the host)

```bash
# 1. Install deps locally for host-side smoke tests.
pip install -r ~/kalshi-signl/requirements.txt

# 2. Edit ~/kalshi-signl/.env so KALSHI_API_KEY_ID is your demo key UUID
#    and KALSHI_PRIVATE_KEY_PATH is the absolute path to your PEM.
#    NVIDIA_API_KEY, BRAVE_API_KEY, TELEGRAM_BOT_TOKEN should also be set.

# 3. Verify Kalshi RSA auth on the host before deploying.
cd ~/kalshi-signl/sandbox/workspace && python3 -m kalshi_client verify

# 4. One-shot deploy to the sandbox (bundles modules + PEM + env, applies
#    the network policy, installs the skill, then recovers the bridge).
bash ~/kalshi-signl/deploy.sh signl3

# 5. From Telegram, message your bot:  help
#    Then try:  balance, watchlist, add KXTICKER manual, analyze KXTICKER, scan
```

## Operational notes

- All Python helpers return either readable strings or
  `{"ok": bool, ...}` dicts and never raise to the shell layer. Stderr is for
  unexpected exceptions only - relay it verbatim.
- Prices and thresholds are integer cents (1..99). Probabilities are 0..100.
- Position sizing is fractional (1/4) Kelly capped at 5% of bankroll, clamped
  to `[1, 100]` contracts.
- The bot only trades on the demo API. Switching to the live API is a
  one-line change in `kalshi_client.BASE_URL` plus updating `policies/kalshi.yaml`
  to allow `trading.kalshi.com`.
