# scripts/download_banknifty.py
# Download BANKNIFTY 1-minute historical data from Zerodha Kite.
# Same safety pattern as refresh_zerodha_data.py — backs up old CSV first.
#
# RUN:
#   python scripts/download_banknifty.py
#   YEARS_BACK=5 python scripts/download_banknifty.py

import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

CSV_PATH        = "data/historical/banknifty_1m_full.csv"
BANKNIFTY_TOKEN = 260105          # NSE:NIFTY BANK (verify via kite.instruments("NSE") if needed)
YEARS_BACK      = float(os.getenv("YEARS_BACK", "3"))
CHUNK_DAYS      = 60


def main():
    print("=" * 64)
    print("  ZERODHA HISTORICAL DATA DOWNLOAD — BANKNIFTY 1m")
    print("=" * 64)

    api_key = os.getenv("KITE_API_KEY", "").strip()
    token   = os.getenv("KITE_ACCESS_TOKEN", "").strip()
    if not api_key or not token:
        print("[ABORT] KITE_API_KEY / KITE_ACCESS_TOKEN missing in .env")
        return

    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(token)
        prof = kite.profile()
        print(f"[AUTH] OK — {prof.get('user_name', prof.get('user_id', '?'))}")
    except Exception as e:
        print(f"[ABORT] Auth failed: {e}")
        print("  Run:  python login.py   then retry.")
        return

    to_dt   = datetime.now()
    from_dt = to_dt - timedelta(days=int(YEARS_BACK * 365))
    print(f"[FETCH] BANKNIFTY 1m  {from_dt.date()} -> {to_dt.date()}")

    frames = []
    cur = from_dt
    while cur < to_dt:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS), to_dt)
        try:
            raw = kite.historical_data(BANKNIFTY_TOKEN, cur, chunk_end, "minute", oi=False)
            if raw:
                frames.append(pd.DataFrame(raw))
                print(f"  {cur.date()}..{chunk_end.date()}: {len(raw):,} candles")
            time.sleep(0.4)
        except Exception as e:
            print(f"  {cur.date()}..{chunk_end.date()}: FAILED ({e})")
        cur = chunk_end

    if not frames:
        print("[ABORT] No data returned. Check subscription / token.")
        return

    new = pd.concat(frames, ignore_index=True)
    col = pd.to_datetime(new["date"])
    if col.dt.tz is not None:
        col = col.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    new["date"] = col
    new = (new[["date", "open", "high", "low", "close", "volume"]]
           .sort_values("date").drop_duplicates("date").reset_index(drop=True))

    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    if os.path.exists(CSV_PATH):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = CSV_PATH.replace(".csv", f".{stamp}.bak.csv")
        shutil.move(CSV_PATH, bak)
        print(f"[BACKUP] Old file -> {bak}")

    new.to_csv(CSV_PATH, index=False)
    print(f"[SAVED] {CSV_PATH}  {len(new):,} rows  "
          f"{new['date'].min().date()} -> {new['date'].max().date()}")
    print("\n  Next steps:")
    print("    INSTRUMENT=banknifty python backtest/futures_edge_test.py")
    print("=" * 64)


if __name__ == "__main__":
    main()
