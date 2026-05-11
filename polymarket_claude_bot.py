"""
Claude-Powered Polymarket Bot
==============================
Requirements:
  pip install anthropic httpx schedule python-dotenv

Environment variables to set in Railway:
  ANTHROPIC_API_KEY   = your Anthropic key
  PAPER_TRADE         = true
"""

import os
import json
import time
import logging
import schedule
from datetime import datetime, timezone
from dotenv import load_dotenv

import httpx
import anthropic

load_dotenv()

# ── Settings ──────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PAPER_TRADE       = os.getenv("PAPER_TRADE", "true").lower() == "true"
CLOB_BASE         = "https://clob.polymarket.com"
MIN_CONFIDENCE    = 0.70
MAX_BET_USDC      = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("polybot")


def fetch_markets():
    try:
        resp = httpx.get(f"{CLOB_BASE}/markets", params={"active": True, "limit": 100}, timeout=15)
        resp.raise_for_status()
        markets = resp.json().get("data", [])
        log.info(f"Fetched {len(markets)} markets")
        return markets
    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        return []


def filter_markets(markets):
    from datetime import datetime, timezone, timedelta
    good = []
    now = datetime.now(timezone.utc)
    for m in markets:
        try:
            tokens = m.get("tokens", [])
            yes = next((t for t in tokens if t.get("outcome") == "Yes"), None)
            no  = next((t for t in tokens if t.get("outcome") == "No"),  None)
            if not yes or not no:
                continue
            yes_price = float(yes.get("price", 0))
            volume    = float(m.get("volume", 0))

            # Parse end date
            end_str = m.get("end_date_iso", "")
            if not end_str:
                continue
            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

            # Only markets ending within the next 7 days
            days_left = (end_date - now).days
            if days_left < 0 or days_left > 7:
                continue

            if volume > 1000 and 0.05 < yes_price < 0.95:
                good.append({
                    "id":       m.get("condition_id"),
                    "question": m.get("question", ""),
                    "yes":      yes_price,
                    "no":       float(no.get("price", 0)),
                    "volume":   volume,
                    "end":      end_str,
                    "days_left": days_left,
                })
        except Exception:
            continue
    good.sort(key=lambda x: x["volume"], reverse=True)
    log.info(f"Filtered to {len(good[:5])} short-term candidates")
    return good[:5]


def ask_claude(market):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""You are a prediction market analyst.

Market: {market['question']}
YES price: {market['yes']:.2f}  ({market['yes']*100:.0f}% implied probability)
NO price:  {market['no']:.2f}
Volume: ${market['volume']:,.0f}
End date: {market['end']}
Today: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

Should I trade this? Reply ONLY with valid JSON, no extra text:
{{
  "should_trade": true,
  "confidence": 0.80,
  "recommended_side": "NO",
  "reasoning": "One short paragraph.",
  "risk_flags": ["risk1", "risk2"]
}}"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        log.error(f"Claude error: {e}")
        return {"should_trade": False, "confidence": 0, "recommended_side": "NO", "reasoning": str(e), "risk_flags": []}


def place_order(market, verdict):
    side  = verdict["recommended_side"]
    price = market["yes"] if side == "YES" else market["no"]
    size  = round(MAX_BET_USDC / price, 2)
    log.info(
        f"[PAPER ORDER] {side} {size} shares @ ${price:.3f} | "
        f"Confidence: {verdict['confidence']:.0%} | "
        f"{market['question'][:60]}"
    )


def run_cycle():
    log.info(f"=== New cycle — {datetime.now(timezone.utc).strftime('%H:%M UTC')} ===")
    markets = fetch_markets()
    candidates = filter_markets(markets)

    for market in candidates:
        log.info(f"Analyzing: {market['question'][:70]}")
        verdict = ask_claude(market)
        log.info(f"  → should_trade={verdict['should_trade']} confidence={verdict['confidence']:.0%} side={verdict['recommended_side']}")
        log.info(f"  → {verdict['reasoning'][:120]}")

        if verdict["should_trade"] and verdict["confidence"] >= MIN_CONFIDENCE:
            place_order(market, verdict)

        time.sleep(3)

    log.info("Cycle complete.\n")


if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set!")

    mode = "PAPER" if PAPER_TRADE else "LIVE"
    log.info(f"Claude Polymarket Bot starting [{mode} MODE]")

    run_cycle()
    schedule.every(5).minutes.do(run_cycle)

    while True:
        schedule.run_pending()
        time.sleep(10)
  
