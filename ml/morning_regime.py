# ml/morning_regime.py
# LLM-powered morning regime classifier.
# Run BEFORE market opens (ideally 9:00-9:15 AM).
#
# Reads last 5 trading days of OHLCV, feeds structured context to LLM,
# gets a regime decision: TREND_UP / TREND_DOWN / RANGE / SKIP
# Saves result to data/regime_today.json for live engine to read.
#
# RUN:
#   python ml/morning_regime.py                     # uses INSTRUMENT from .env
#   INSTRUMENT=banknifty python ml/morning_regime.py
#   INSTRUMENT=nifty     python ml/morning_regime.py
#
# LIVE ENGINE INTEGRATION:
#   from ml.morning_regime import load_today_regime
#   regime = load_today_regime()   # returns "TREND_UP" / "TREND_DOWN" / "RANGE" / "SKIP" / "UNKNOWN"

import os, sys, json, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, timedelta
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

INSTRUMENT = "banknifty"
DATA_FILE  = "data/historical/banknifty_1m_full.csv"
OUTPUT_PATH  = "data/regime_today.json"
LOOKBACK_DAYS = 6   # days of daily context sent to LLM


# ── Helpers ───────────────────────────────────────────────────────────

def _load_daily(instrument=None, n_days=LOOKBACK_DAYS):
    """Build daily OHLCV from 1-minute CSV, return last n_days."""
    path = DATA_FILE
    if not os.path.exists(path):
        raise FileNotFoundError(f"BANKNIFTY data not found: {path}\n  Run: python scripts/download_banknifty.py")
    df = pd.read_csv(path, parse_dates=["date"])
    df["_d"] = df["date"].dt.date
    daily = df.groupby("_d").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"),   close=("close", "last"),
        volume=("volume", "sum"),
    )
    return daily.tail(n_days)


def _build_prompt(daily, instrument, week_regime="UNKNOWN"):
    name = "BANKNIFTY"
    lines = []
    prev_close = None
    for d, row in daily.iterrows():
        rng  = row["high"] - row["low"]
        pdir = "↑UP" if row["close"] > row["open"] else "↓DN"
        gap  = f"gap={row['open']-prev_close:+.0f}pt" if prev_close is not None else "gap=N/A"
        lines.append(f"  {d}: O={row['open']:.0f} H={row['high']:.0f} "
                     f"L={row['low']:.0f} C={row['close']:.0f} | "
                     f"range={rng:.0f}pt | {pdir} | {gap}")
        prev_close = row["close"]

    today  = daily.iloc[-1]
    prev   = daily.iloc[-2] if len(daily) >= 2 else today
    today_gap  = today["open"] - prev["close"]
    prev_range = prev["high"] - prev["low"]
    prev_dir   = "bullish" if prev["close"] > prev["open"] else "bearish"

    # Weekly context helps bias the daily classification
    week_ctx = ""
    if week_regime == "BULL_WEEK":
        week_ctx = "\nWEEKLY CONTEXT: Currently in a BULL WEEK — weekly trend is UP. Bias toward TREND_UP unless strong reversal signal."
    elif week_regime == "BEAR_WEEK":
        week_ctx = "\nWEEKLY CONTEXT: Currently in a BEAR WEEK — weekly trend is DOWN. Bias toward TREND_DOWN unless strong reversal signal."
    elif week_regime == "CHOP_WEEK":
        week_ctx = "\nWEEKLY CONTEXT: CHOP WEEK — no weekly trend. Lean toward RANGE unless today's data is unusually strong."

    return f"""You are an expert {name} intraday trader with 10 years experience.

LAST {len(daily)} TRADING DAYS — {name}:
{chr(10).join(lines)}{week_ctx}

KEY FACTS FOR TODAY:
  Previous day: range={prev_range:.0f}pt, {prev_dir}, close={prev["close"]:.0f}
  Today open gap: {today_gap:+.0f} points ({'gap-up' if today_gap > 0 else 'gap-down' if today_gap < 0 else 'flat open'})

Based on this price structure AND the weekly context, classify today as exactly ONE regime:

  TREND_UP   — strong bullish day expected; sustained directional move up likely
  TREND_DOWN — strong bearish day expected; sustained directional move down likely
  RANGE      — sideways/oscillating day; avoid directional trades
  SKIP       — ambiguous/reversal; no clear edge today

Reply in EXACTLY this format (3 lines, no extra text):
REGIME: <TREND_UP|TREND_DOWN|RANGE|SKIP>
REASON: <one sentence explaining the key signal>
CONFIDENCE: <HIGH|MEDIUM|LOW>"""


def _call_llm(prompt):
    """Call OpenAI-compatible LLM. Returns raw text response."""
    api_key  = os.getenv("LLM_API_KEY", "").strip()
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model    = os.getenv("LLM_MODEL", "gpt-4o-mini")

    if not api_key or api_key.startswith("freellmapi-xxx"):
        raise ValueError("LLM_API_KEY not configured")

    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 120,
            "temperature": 0.1,   # low temp for consistent classification
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _parse_response(text):
    """Parse REGIME/REASON/CONFIDENCE from LLM response."""
    result = {"regime": "UNKNOWN", "reason": "", "confidence": "LOW"}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("REGIME:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("TREND_UP", "TREND_DOWN", "RANGE", "SKIP"):
                result["regime"] = val
        elif line.startswith("REASON:"):
            result["reason"] = line.split(":", 1)[1].strip()
        elif line.startswith("CONFIDENCE:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("HIGH", "MEDIUM", "LOW"):
                result["confidence"] = val
    return result


# ── Public API ────────────────────────────────────────────────────────

def classify_today(instrument=None) -> dict:
    """
    Classify today's market regime using LLM, incorporating weekly trend context.
    Returns dict with keys: regime, reason, confidence, instrument, date, timestamp
    """
    inst = (instrument or INSTRUMENT).lower()
    daily = _load_daily(inst)

    # Read weekly regime (if already run this week)
    try:
        from ml.weekly_regime import load_week_regime
        week_regime = load_week_regime(inst)
    except Exception:
        week_regime = "UNKNOWN"

    prompt = _build_prompt(daily, inst, week_regime)

    print(f"[LLM] Calling regime classifier for {inst.upper()} ...")
    raw = _call_llm(prompt)
    parsed = _parse_response(raw)

    result = {
        "date":       str(date.today()),
        "instrument": inst,
        "regime":     parsed["regime"],
        "reason":     parsed["reason"],
        "confidence": parsed["confidence"],
        "timestamp":  datetime.now().isoformat(timespec="seconds"),
        "raw_response": raw,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2)

    return result


def load_today_regime(instrument=None) -> str:
    """
    Read today's regime from saved JSON.
    Returns regime string: TREND_UP / TREND_DOWN / RANGE / SKIP / UNKNOWN
    Call this from the live engine at startup.
    """
    inst = (instrument or INSTRUMENT).lower()
    try:
        if not os.path.exists(OUTPUT_PATH):
            return "UNKNOWN"
        with open(OUTPUT_PATH) as f:
            data = json.load(f)
        if data.get("date") != str(date.today()):
            return "UNKNOWN"   # stale file from yesterday
        if data.get("instrument", "").lower() != inst:
            return "UNKNOWN"
        return data.get("regime", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


# ── Backtest rule-based proxy (no LLM needed) ─────────────────────────

def regime_proxy(prev_open, prev_high, prev_low, prev_close, today_open,
                 instrument="banknifty") -> str:
    """
    Rule-based regime approximation for BACKTESTING (no LLM call).
    Mimics what a well-calibrated LLM would say given the same data.

    BANKNIFTY thresholds:   range>300pt = large, gap>80pt = strong
    NIFTY thresholds:       range>130pt = large, gap>25pt = strong
    """
    prev_range = prev_high - prev_low
    prev_dir   = 1 if prev_close > prev_open else -1
    gap        = today_open - prev_close

    large_range = 300; strong_gap = 80   # BANKNIFTY thresholds

    # Strong trend continuation: large previous range + gap in same direction
    if prev_range >= large_range and prev_dir == 1 and gap >= strong_gap:
        return "TREND_UP"
    if prev_range >= large_range and prev_dir == -1 and gap <= -strong_gap:
        return "TREND_DOWN"

    # Moderate trend: decent range, moderate gap
    if prev_range >= large_range * 0.75 and gap >= strong_gap * 0.5 and prev_dir == 1:
        return "TREND_UP"
    if prev_range >= large_range * 0.75 and gap <= -strong_gap * 0.5 and prev_dir == -1:
        return "TREND_DOWN"

    # Gap reversal or small range → range/choppy
    return "RANGE"


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"  MORNING REGIME CLASSIFIER — {INSTRUMENT.upper()}")
    print("=" * 60)

    try:
        result = classify_today(INSTRUMENT)
        print(f"\n  REGIME:     {result['regime']}")
        print(f"  REASON:     {result['reason']}")
        print(f"  CONFIDENCE: {result['confidence']}")
        print(f"  SAVED:      {OUTPUT_PATH}")
        print()
        if result["regime"] == "TREND_UP":
            print("  → Today: CE trades only (ORB breakout above)")
        elif result["regime"] == "TREND_DOWN":
            print("  → Today: PE trades only (ORB breakdown below)")
        elif result["regime"] == "RANGE":
            print("  → Today: NO directional trades (choppy day)")
        else:
            print("  → Today: SKIP — no clear bias")
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        print("  Download data first:  python scripts/download_banknifty.py")
    except Exception as e:
        print(f"[ERROR] LLM call failed: {e}")
        print("  Check LLM_BASE_URL and LLM_API_KEY in .env")

    print("=" * 60)


if __name__ == "__main__":
    main()
