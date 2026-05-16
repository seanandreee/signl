#!/usr/bin/env bash
# audit.sh — NemoClaw security audit for the signl Kalshi trading bot.
#
# Usage:
#   ./audit.sh                  # full report
#   ./audit.sh --guardrails     # blocked trade attempts only
#   ./audit.sh --trades         # trade actions only
#   ./audit.sh --network        # OCSF network allow/deny events only
#
# OCSF network events are produced by the OpenShell sandbox runtime and
# streamed via `openshell logs`. The openclaw application log at
# /tmp/openclaw-998/openclaw-2026-05-16.log is NDJSON (application events
# only); network enforcement events never appear there.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${SCRIPT_DIR}/sandbox/workspace"
GUARDRAILS_LOG="${WORKSPACE}/guardrails.log"
OPENCLAW_LOG="/tmp/openclaw-998/openclaw-2026-05-16.log"   # NDJSON app log
SANDBOX_NAME="trader"
OCSF_LINES=500   # how many recent log lines to scan for OCSF events

GREEN='\033[32m'
RED='\033[31m'
BOLD='\033[1m'
RESET='\033[0m'

FILTER="${1:-}"

hr() { printf '%0.s─' {1..72}; echo; }

# ── Header ────────────────────────────────────────────────────────────────────
if [[ -z "$FILTER" || "$FILTER" == "--all" ]]; then
    echo
    echo "  signl Kalshi Bot — NemoClaw Security Audit Report"
    echo "  Generated : $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "  Sandbox   : ${SANDBOX_NAME}"
    echo "  App log   : ${OPENCLAW_LOG}"
    hr
fi

# ── Section 1: Guardrails (blocked trade attempts) ────────────────────────────
if [[ -z "$FILTER" || "$FILTER" == "--guardrails" ]]; then
    echo
    echo "  GUARDRAILS — Blocked Trade Attempts"
    hr
    if [[ -f "$GUARDRAILS_LOG" ]]; then
        COUNT=$(wc -l < "$GUARDRAILS_LOG")
        echo "  Total blocked: ${COUNT}"
        echo
        while IFS= read -r line; do
            ts="$(       echo "$line" | grep -oP '(?<=\[)[^\]]+(?=\])')"
            ticker="$(   echo "$line" | grep -oP '(?<=ticker=)\S+')"
            contracts="$(echo "$line" | grep -oP '(?<=contracts=)\d+')"
            price="$(    echo "$line" | grep -oP '(?<=price=)\S+')"
            reason="$(   echo "$line" | grep -oP '(?<=reason: ).+')"
            printf "  ${RED}BLOCKED${RESET}  %-24s  %-20s  %s contracts @ %s\n" \
                   "${ts}" "${ticker}" "${contracts}" "${price}"
            printf "           Reason: %s\n\n" "${reason}"
        done < "$GUARDRAILS_LOG"
    else
        echo "  No guardrails log found at: ${GUARDRAILS_LOG}"
        echo "  (No blocked trades recorded yet)"
    fi
fi

# ── Section 2: Trade Actions ──────────────────────────────────────────────────
if [[ -z "$FILTER" || "$FILTER" == "--trades" ]]; then
    echo
    echo "  TRADE ACTIONS — Orders Placed"
    hr
    DB_PATH="${WORKSPACE}/signl.db"
    if command -v sqlite3 &>/dev/null && [[ -f "$DB_PATH" ]]; then
        sqlite3 -separator '|' "$DB_PATH" \
            "SELECT timestamp, ticker, action, side, contracts, price, reason
             FROM trade_log ORDER BY timestamp DESC LIMIT 50;" \
        | while IFS='|' read -r ts ticker action side contracts price reason; do
            printf "  [%s]  %-20s  %s %s  %s contracts @ %sc\n" \
                   "${ts}" "${ticker}" "${action}" "${side}" "${contracts}" "${price}"
            [[ -n "$reason" ]] && printf "    Reason: %s\n" "$reason"
            echo
        done
        TRADE_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM trade_log;" 2>/dev/null || echo "?")
        echo "  Total trades logged: ${TRADE_COUNT}"
    else
        echo "  signl.db not found — no trades recorded yet"
        echo "  DB path: ${DB_PATH}"
    fi
fi

# ── Section 3: OCSF Network Events ───────────────────────────────────────────
# Lines arrive from openshell logs in this format:
#   ALLOWED HTTP:  [epoch] [sandbox] [OCSF ] [ocsf] HTTP:METHOD [INFO] ALLOWED METHOD URL [policy:NAME engine:ENGINE]
#   ALLOWED NET:   [epoch] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] ALLOWED PROCESS(PID) -> HOST:PORT [policy:NAME engine:ENGINE]
#   DENIED  NET:   [epoch] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED PROCESS(PID) -> HOST:PORT [policy:- engine:opa] [reason:TEXT]
#
# Field layout (space-split): $1=epoch $2=sandbox $3=[OCSF $4=] $5=[ocsf]
#   $6=EVENT $7=[SEV] $8=ACTION $9=PROCESS_or_METHOD $10=URL_or_-> $11=URL_or_HOST ...
if [[ -z "$FILTER" || "$FILTER" == "--network" ]]; then
    echo
    echo "  NETWORK — OCSF Allow / Deny Events (sandbox: ${SANDBOX_NAME})"
    hr

    # Collect OCSF lines that have an ALLOWED or DENIED verdict, skip SSH noise
    RAW_OCSF="$(openshell logs "${SANDBOX_NAME}" -n "${OCSF_LINES}" --source all 2>/dev/null \
        | grep "\[OCSF " \
        | grep -E " ALLOWED| DENIED" \
        | grep -v "SSH:OPEN")"

    if [[ -z "$RAW_OCSF" ]]; then
        echo "  No OCSF network events found in the last ${OCSF_LINES} log lines."
        echo "  (Events appear once the bot makes outbound connections.)"
    else
        echo "$RAW_OCSF" | gawk -v GREEN="$GREEN" -v RED="$RED" -v BOLD="$BOLD" -v RESET="$RESET" '
        BEGIN { allowed = 0; denied = 0 }
        {
            # $1=[epoch.ms]  $2=[sandbox]  $3=[OCSF  $4=]  $5=[ocsf]
            # $6=EVENT       $7=[SEV]      $8=ACTION  $9+= payload
            epoch  = $1;  gsub(/[\[\].]/, "", epoch);  epoch = int(epoch)
            ts     = strftime("%H:%M:%S", epoch, 1)    # UTC
            event  = $6   # HTTP:GET, HTTP:POST, NET:OPEN, …
            action = $8   # ALLOWED | DENIED

            # Extract policy name from [policy:NAME ...]
            pol = "?"
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^\[policy:/) { pol = $i; sub(/^\[policy:/, "", pol); break }
            }

            if (action == "ALLOWED") {
                allowed++
                if (event ~ /^HTTP:/) {
                    # $9=method  $10=url
                    printf GREEN "  [%s] ALLOWED  %-6s  %s  [policy:%s]\n" RESET, ts, $9, $10, pol
                } else {
                    # NET:OPEN — two shapes:
                    #   long:  ALLOWED PROCESS(PID) -> HOST:PORT [policy:NAME ...]
                    #   short: ALLOWED HOST:PORT                 (local/inference)
                    if ($10 == "->") {
                        printf GREEN "  [%s] ALLOWED  %s -> %s  [policy:%s]\n" RESET, ts, $9, $11, pol
                    } else {
                        printf GREEN "  [%s] ALLOWED  %s  [policy:%s]\n" RESET, ts, $9, pol
                    }
                }
            } else if (action == "DENIED") {
                denied++
                # $9=process  $10=->  $11=host:port
                proc = $9;  dest = $11
                # Extract [reason:TEXT] — find it by string search
                reason = ""
                p = index($0, "[reason:")
                if (p > 0) {
                    reason = substr($0, p + 8)
                    sub(/\]$/, "", reason)
                    if (length(reason) > 90) reason = substr(reason, 1, 90) "…"
                }
                printf RED "  [%s] DENIED   %s -> %s\n" RESET, ts, proc, dest
                if (reason != "") printf RED "           Reason: %s\n" RESET, reason
            }
        }
        END {
            print ""
            printf BOLD "  Summary: %d allowed, %d denied\n" RESET, allowed, denied
        }
        '
    fi
fi

# ── Section 4: Open Positions ─────────────────────────────────────────────────
if [[ -z "$FILTER" || "$FILTER" == "--all" ]]; then
    echo
    echo "  OPEN POSITIONS"
    hr
    DB_PATH="${WORKSPACE}/signl.db"
    if command -v sqlite3 &>/dev/null && [[ -f "$DB_PATH" ]]; then
        POS_ROWS="$(sqlite3 -separator '|' "$DB_PATH" \
            "SELECT ticker, side, num_contracts, entry_price,
                    COALESCE(current_price,0), COALESCE(pnl,0), opened_at
             FROM positions WHERE status='open' ORDER BY opened_at;" 2>/dev/null)"
        if [[ -n "$POS_ROWS" ]]; then
            echo "$POS_ROWS" | while IFS='|' read -r ticker side qty entry cur pnl opened; do
                printf "  %-22s  %-3s  %s contracts  entry=%sc  cur=%sc  pnl=%sc\n" \
                       "$ticker" "$side" "$qty" "$entry" "$cur" "$pnl"
            done
        else
            echo "  (no open positions)"
        fi
        POS_COUNT=$(sqlite3 "$DB_PATH" \
            "SELECT COUNT(*) FROM positions WHERE status='open';" 2>/dev/null || echo "?")
        echo
        printf "  Open positions: %s / 10 (NemoClaw guardrail limit)\n" "$POS_COUNT"
    else
        echo "  signl.db not found"
    fi
    echo
    hr
fi
