# ml/weekly_regime.py
# LLM weekly trend classifier. Run every Monday morning before 9:15 AM.
#
# KEY INSIGHT FROM BACKTESTING:
#   ORB directional entries only produce edge during trending WEEKS.
#   In choppy/range weeks the win rate drops to ~35% (below break-even).
#   In trend weeks it rises to ~52-60%.
#   Detecting the week's character before trading it is the single biggest
#   improvement possible for the system.
#
# HOW IT WORKS:
#   Looks at the last 4 weeks of BANKNIFTY price action and asks the LLM:
#   "Is the trend continuing this week, or did momentum die?"
#   Output: BULL_WEEK / BEAR_WEEK / CHOP_WEEK
#
# INTEGRATION:
#   1. Run: python ml/weekly_regime.py  (every Monday ~9:00 AM)
#   2. morning_regime.py reads it and gates daily regime
#   3. Live engine reads via load_week_regime()
#   4. Only take ORB CE entries on BULL_WEEK, PE entries on BEAR_WEEK.
#      Skip all directional entries on CHOP_WEEK.
#
# RUN:
#   python ml/weekly_regime.py
#   INSTRUMENT=banknifty python ml/weekly_regime.py

import os, sys, json, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, date, timedelta
from pathlib import Path
import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

INSTRUMENT  = "banknifty"
OUTPUT_PATH = "data/weekly_regime.json"
DATA_FILE   = "data/historical/banknifty_1m_full.csv"

# Minimum net weekly directional move to classify as a trend week
_TREND_MOVE = 250   # BANKNIFTY points (prev-week close - open)


# ── Weekly OHLCV builder ──────────────────────────────────────────────

def _load_weekly_df(instrument=None, n_weeks=6):
    """Build weekly OHLCV from 1-minute CSV. Returns last n_weeks rows."""
    path = DATA_FILE
    if not os.path.exists(path):
        raise FileNotFoundError(f"BANKNIFTY data not found: {path}\n  Run: python scripts/download_banknifty.py")
    df = pd.read_csv(path, parse_dates=["date"])
    df["_week"] = df["date"].dt.to_period("W")
    weekly = df.groupby("_week").agg(
        open=("open",   "first"),
        high=("high",   "max"),
        low=("low",     "min"),
        close=("close", "last"),
    )
    weekly.index = weekly.index.to_timestamp()   # Monday of each week
    return weekly.tail(n_weeks)


# ── LLM prompt ───────────────────────────────────────────────────────

def _build_prompt(weekly, instrument):
    name = "BANKNIFTY"
    lines = []
    prev_close = None
    for dt, row in weekly.iterrows():
        rng      = row["high"] - row["low"]
        net_move = row["close"] - row["open"]
        direction = "↑BULL" if net_move > 0 else "↓BEAR"
        gap = f"gap={row['open']-prev_close:+.0f}" if prev_close is not None else "gap=N/A"
        lines.append(
            f"  W/e {dt.strftime('%d-%b')}: O={row['open']:.0f} H={row['high']:.0f} "
            f"L={row['low']:.0f} C={row['close']:.0f} | "
            f"range={rng:.0f}pt net={net_move:+.0f}pt | {direction} | {gap}"
        )
        prev_close = row["close"]

    last   = weekly.iloc[-1]
    prev   = weekly.iloc[-2] if len(weekly) >= 2 else last
    l_move = last["close"]  - last["open"]
    p_move = prev["close"]  - prev["open"]
    l_rng  = last["high"]   - last["low"]

    return f"""You are an expert {name} weekly trend analyst with 10 years experience.

LAST {len(weekly)} WEEKS OF {name} DATA:
{chr(10).join(lines)}

THIS WEEK'S CONTEXT:
  Last week net move: {l_move:+.0f}pt ({'bullish' if l_move > 0 else 'bearish'}) | range: {l_rng:.0f}pt
  Week before: {p_move:+.0f}pt ({'bullish' if p_move > 0 else 'bearish'})
  Trend consistency (last 3 weeks same direction): {'YES' if len(weekly) >= 3 and all(
    (weekly.iloc[i]['close'] > weekly.iloc[i]['open']) == (l_move > 0)
    for i in [-1, -2, -3]
  ) else 'NO'}

For the COMING WEEK, classify as exactly ONE of:
  BULL_WEEK  — uptrend continues; take CE entries on ORB breakouts
  BEAR_WEEK  — downtrend continues; take PE entries on ORB breakdowns
  CHOP_WEEK  — no clear trend; avoid all directional entries this week

Reply in EXACTLY this format (3 lines, no extra text):
WEEK_REGIME: <BULL_WEEK|BEAR_WEEK|CHOP_WEEK>
REASON: <one sentence>
CONFIDENCE: <HIGH|MEDIUM|LOW>"""


# ── LLM call ─────────────────────────────────────────────────────────

def _call_llm(prompt):
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
            "max_tokens": 100,
            "temperature": 0.1,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _parse(text):
    result = {"week_regime": "CHOP_WEEK", "reason": "", "confidence": "LOW"}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("WEEK_REGIME:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("BULL_WEEK", "BEAR_WEEK", "CHOP_WEEK"):
                result["week_regime"] = val
        elif line.startswith("REASON:"):
            result["reason"] = line.split(":", 1)[1].strip()
        elif line.startswith("CONFIDENCE:"):
            val = line.split(":", 1)[1].strip().upper()
            if val in ("HIGH", "MEDIUM", "LOW"):
                result["confidence"] = val
    return result


# ── Public API ────────────────────────────────────────────────────────

def classify_week(instrument=None) -> dict:
    """Call LLM to classify the coming week. Saves to data/weekly_regime.json."""
    inst   = (instrument or INSTRUMENT).lower()
    weekly = _load_weekly_df(inst)
    prompt = _build_prompt(weekly, inst)
    print(f"[LLM] Calling weekly regime classifier for {inst.upper()} ...")
    raw    = _call_llm(prompt)
    parsed = _parse(raw)
    # Determine the Monday of the current week
    today     = date.today()
    monday    = today - timedelta(days=today.weekday())
    result = {
        "week_start":   str(monday),
        "instrument":   inst,
        "week_regime":  parsed["week_regime"],
        "reason":       parsed["reason"],
        "confidence":   parsed["confidence"],
        "timestamp":    datetime.now().isoformat(timespec="seconds"),
        "raw_response": raw,
    }
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2)
    return result


def load_week_regime(instrument=None) -> str:
    """
    Read this week's regime from saved JSON.
    Returns: BULL_WEEK / BEAR_WEEK / CHOP_WEEK / UNKNOWN
    Call from live engine at startup every day.
    """
    inst = (instrument or INSTRUMENT).lower()
    try:
        if not os.path.exists(OUTPUT_PATH):
            return "UNKNOWN"
        with open(OUTPUT_PATH) as f:
            data = json.load(f)
        today  = date.today()
        monday = today - timedelta(days=today.weekday())
        if data.get("week_start") != str(monday):
            return "UNKNOWN"   # stale from last week
        if data.get("instrument", "").lower() != inst:
            return "UNKNOWN"
        return data.get("week_regime", "UNKNOWN")
    except Exception:
        return "UNKNOWN"


# ── Backtest proxy (no LLM) ───────────────────────────────────────────

def add_weekly_regime_to_df(df, instrument="banknifty") -> pd.DataFrame:
    """
    Add 'week_regime' column to df using PREVIOUS week's price action.
    Used in futures_edge_test.py for backtesting (no LLM needed).

    Rule: look at previous week's (close - open).
      |net_move| >= threshold AND move direction → BULL_WEEK or BEAR_WEEK
      otherwise → CHOP_WEEK

    Using PREVIOUS week avoids look-ahead bias.
    """
    df = df.copy()
    df["_week"] = df["date"].dt.to_period("W")

    weekly_open  = df.groupby("_week")["open"].first()
    weekly_close = df.groupby("_week")["close"].last()
    weekly_range = df.groupby("_week").apply(
        lambda g: g["high"].max() - g["low"].min()
    )

    threshold = _TREND_MOVE

    week_regimes = {}
    weeks = sorted(weekly_open.index)
    for i, w in enumerate(weeks):
        if i == 0:
            week_regimes[w] = "CHOP_WEEK"
            continue
        pw        = weeks[i - 1]
        net_move  = float(weekly_close[pw] - weekly_open[pw])
        wk_range  = float(weekly_range[pw])

        # Require both directional move AND decent range to call trend
        if abs(net_move) >= threshold and wk_range >= threshold * 1.5:
            week_regimes[w] = "BULL_WEEK" if net_move > 0 else "BEAR_WEEK"
        else:
            week_regimes[w] = "CHOP_WEEK"

    df["week_regime"] = df["_week"].map(week_regimes).fillna("CHOP_WEEK")
    df.drop(columns=["_week"], inplace=True)
    return df


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"  WEEKLY REGIME CLASSIFIER — {INSTRUMENT.upper()}")
    print("=" * 60)
    try:
        result = classify_week(INSTRUMENT)
        print(f"\n  WEEK_REGIME: {result['week_regime']}")
        print(f"  REASON:      {result['reason']}")
        print(f"  CONFIDENCE:  {result['confidence']}")
        print(f"  SAVED:       {OUTPUT_PATH}")
        print()
        if result["week_regime"] == "BULL_WEEK":
            print("  → This week: CE trades only (ORB breakout above)")
        elif result["week_regime"] == "BEAR_WEEK":
            print("  → This week: PE trades only (ORB breakdown below)")
        else:
            print("  → This week: SKIP all directional trades (chop week)")
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
    except Exception as e:
        print(f"[ERROR] LLM call failed: {e}")
    print("=" * 60)


if __name__ == "__main__":
    main()
