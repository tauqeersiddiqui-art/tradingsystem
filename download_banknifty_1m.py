# download_banknifty_1m.py
"""
Download BANKNIFTY 1-minute historical candles for the last 10 years using Kite Connect.
Saves to: data/historical/banknifty_1m_full.csv
Requires: kiteconnect (pip install kiteconnect)
Credentials: set KITE_API_KEY, KITE_API_SECRET, KITE_ACCESS_TOKEN in your environment or .env file.
"""

import os
import time
import csv
import sys
from datetime import datetime, timedelta
import pandas as pd

try:
    from kiteconnect import KiteConnect
except Exception as e:
    print("ERROR: kiteconnect not installed. Run: pip install kiteconnect", file=sys.stderr)
    raise

# ---------- CONFIG ----------
OUT_PATH = os.path.join("data", "historical", "banknifty_1m_full.csv")
SYMBOL_CANDIDATES = ["BANKNIFTY", "BANK NIFTY", "NIFTY BANK"]
INTERVAL = "minute"   # kiteconnect uses 'minute' for 1-min
YEARS = 10
CHUNK_DAYS = 30       # fetch at most 30 days per call to be safe
SLEEP_BETWEEN_CALLS = 0.5
# ----------------------------

def load_env_vars():
    # Prefer environment variables; optionally load .env if present
    from pathlib import Path
    env_file = Path(".env")
    if env_file.exists():
        with open(env_file, "r", encoding="utf8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    cfg = {
        "api_key": os.getenv("KITE_API_KEY") or os.getenv("ZERODHA_API_KEY"),
        "api_secret": os.getenv("KITE_API_SECRET") or os.getenv("ZERODHA_API_SECRET"),
        "access_token": os.getenv("KITE_ACCESS_TOKEN") or os.getenv("ZERODHA_ACCESS_TOKEN"),
    }
    if not cfg["api_key"] or not cfg["access_token"]:
        raise RuntimeError("Missing KITE_API_KEY or KITE_ACCESS_TOKEN in environment or .env.")
    return cfg

def find_token(kite, symbol_candidates):
    print("Looking up instrument tokens (this may take a few seconds)...")
    instruments = kite.instruments()
    # prefer index/instrument with exact match in tradingsymbol
    for cand in symbol_candidates:
        for inst in instruments:
            if inst.get("tradingsymbol", "").upper().startswith(cand.upper()):
                # prefer index exchange (NFO may have options; look for index type)
                if inst.get("segment") in ("NSE", "NFO", None) or inst.get("exchange"):
                    print(f"Found token: {inst['tradingsymbol']} token={inst['instrument_token']} exchange={inst.get('exchange')}")
                    return inst["instrument_token"], inst["tradingsymbol"]
    # fallback: ask user to inspect instruments externally
    raise RuntimeError("Could not auto-find BANKNIFTY token. Inspect kite.instruments() output or pass token manually.")

def fetch_chunk(kite, token, from_dt, to_dt, interval):
    # kite.historical_data expects strings or datetime objects depending on version
    # return list of dicts with 'date','open','high','low','close','volume'
    try:
        rows = kite.historical_data(token, from_dt, to_dt, interval)
    except TypeError:
        # older versions may require strings
        rows = kite.historical_data(token, from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                                    to_dt.strftime("%Y-%m-%d %H:%M:%S"), interval)
    return rows

def to_dataframe(all_rows):
    if not all_rows:
        return pd.DataFrame(columns=["date","open","high","low","close","volume"])
    df = pd.DataFrame(all_rows)
    # normalize date column name variants
    if "date" not in df.columns:
        if "timestamp" in df.columns:
            df = df.rename(columns={"timestamp":"date"})
        elif "time" in df.columns:
            df = df.rename(columns={"time":"date"})
    df["date"] = pd.to_datetime(df["date"])
    df = df.loc[:, ["date","open","high","low","close","volume"]]
    # de-duplicate and sort
    df = df.drop_duplicates(subset=["date"], keep="last")
    df = df.sort_values("date").reset_index(drop=True)
    return df

def main():
    cfg = load_env_vars()
    kite = KiteConnect(api_key=cfg["api_key"])
    kite.set_access_token(cfg["access_token"])
    token, tradingsymbol = find_token(kite, SYMBOL_CANDIDATES)

    now = datetime.utcnow()
    end_dt = now
    start_dt = now - timedelta(days=365 * YEARS)

    print(f"Requested range: {start_dt.date()} → {end_dt.date()} ({YEARS} years)")
    cur_from = start_dt
    all_frames = []
    chunk_i = 0

    while cur_from < end_dt:
        cur_to = min(cur_from + timedelta(days=CHUNK_DAYS), end_dt)
        print(f"[{chunk_i}] fetching {cur_from.date()} → {cur_to.date()} ...", end=" ", flush=True)
        try:
            rows = fetch_chunk(kite, token, cur_from, cur_to, INTERVAL)
        except Exception as e:
            print(f"FAILED: {e}")
            # be polite to API and back off
            time.sleep(2)
            continue
        print(f"got {len(rows)} rows")
        df_chunk = to_dataframe(rows)
        all_frames.append(df_chunk)
        # advance
        cur_from = cur_to + timedelta(minutes=1)
        chunk_i += 1
        time.sleep(SLEEP_BETWEEN_CALLS)

    if not all_frames:
        print("No data downloaded. Exiting.", file=sys.stderr)
        return

    df_all = pd.concat(all_frames, ignore_index=True)
    df_all = df_all.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)

    # Save CSV (ensure folder exists)
    out_dir = os.path.dirname(OUT_PATH)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    df_all.to_csv(OUT_PATH, index=False)
    print(f"Saved {len(df_all)} rows to {OUT_PATH}")

    # verification summary
    first = df_all["date"].iloc[0]
    last = df_all["date"].iloc[-1]
    print("VERIFICATION:")
    print("  First timestamp:", first)
    print("  Last  timestamp:", last)
    print("  Rows:", len(df_all))
    # quick check for monotonicity
    monotonic = df_all["date"].is_monotonic_increasing
    print("  Monotonic timestamps:", monotonic)

if __name__ == "__main__":
    main()