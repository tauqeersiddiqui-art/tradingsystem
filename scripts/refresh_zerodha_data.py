# scripts/refresh_zerodha_data.py
# Replace the historical NIFTY 1-minute CSV with a fresh pull from Zerodha.
#
# SAFETY: the existing CSV is BACKED UP (renamed with a timestamp) BEFORE the
# new one is written. The old file is never hard-deleted — if the download
# fails (stale token / no subscription / network), your data is still intact
# in the backup and the live CSV is left as-is.
#
# Zerodha minute-history limits: ~60 days per request; total depth depends on
# your historical-data subscription. We loop in 60-day chunks back to START.
#
# PREREQUISITE: a VALID access token. Zerodha tokens expire daily and need the
# TOTP login. If this script prints an auth error, run your login first:
#     python login.py      (or login_headless.py)
# then re-run this script.
#
# RUN:
#   python scripts/refresh_zerodha_data.py
#   YEARS_BACK=3 python scripts/refresh_zerodha_data.py

import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

CSV_PATH   = "data/historical/banknifty_1m_full.csv"
NIFTY_TOKEN = 260105            # NSE:NIFTY BANK (BANKNIFTY) index
YEARS_BACK  = float(os.getenv("YEARS_BACK", "3"))
CHUNK_DAYS  = 60                # Zerodha minute-data max window per request


def main():
    print("=" * 64)
    print("  ZERODHA HISTORICAL DATA REFRESH (NIFTY 1m)")
    print("=" * 64)

    from pathlib import Path

    project_root = Path(__file__).resolve().parent.parent
    env_file = project_root / ".env"

    print(f"[ENV] Using: {env_file}")

    load_dotenv(env_file, override=True)

    api_key = os.getenv("KITE_API_KEY", "").strip()
    token = os.getenv("KITE_ACCESS_TOKEN", "").strip()

    print(f"[DEBUG] API_KEY={api_key}")

    if token:
        print(f"[DEBUG] TOKEN={token[:8]}...{token[-4:]}")
    else:
        print("[DEBUG] TOKEN=MISSING")

    if not api_key or not token:
        print("[ABORT] KITE_API_KEY / KITE_ACCESS_TOKEN missing")
        return

    try:
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(token)

        print("[DEBUG] Testing profile() ...")

        prof = kite.profile()

        print(f"[AUTH] OK — logged in as {prof.get('user_name', prof.get('user_id', '?'))}")

    except Exception as e:

        print(f"[ABORT] Auth/connection failed: {repr(e)}")

        return
    to_dt   = datetime.now()
    from_dt = to_dt - timedelta(days=int(YEARS_BACK * 365))
    print(f"[FETCH] NIFTY 1m  {from_dt.date()} -> {to_dt.date()}  in {CHUNK_DAYS}d chunks")

    frames = []
    cur = from_dt
    while cur < to_dt:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS), to_dt)
        try:
            raw = kite.historical_data(NIFTY_TOKEN, cur, chunk_end, "minute", oi=False)
            if raw:
                frames.append(pd.DataFrame(raw))
                print(f"  {cur.date()}..{chunk_end.date()}: {len(raw):,} candles")
            time.sleep(0.4)   # respect rate limits
        except Exception as e:
            print(f"  {cur.date()}..{chunk_end.date()}: FAILED ({e})")
        cur = chunk_end

    if not frames:
        print("[ABORT] No data returned (subscription/permission?). Old CSV untouched.")
        return

    new = pd.concat(frames, ignore_index=True)
    col = pd.to_datetime(new["date"])
    if col.dt.tz is not None:
        col = col.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    new["date"] = col
    new = (new[["date", "open", "high", "low", "close", "volume"]]
           .sort_values("date").drop_duplicates("date").reset_index(drop=True))

    # Back up old CSV (never hard-delete) only now that we have new data.
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    if os.path.exists(CSV_PATH):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = CSV_PATH.replace(".csv", f".{stamp}.bak.csv")
        shutil.move(CSV_PATH, bak)
        print(f"[BACKUP] Old data -> {bak}")

    new.to_csv(CSV_PATH, index=False)
    print(f"[SAVED] {CSV_PATH}  {len(new):,} rows  "
          f"{new['date'].min().date()} -> {new['date'].max().date()}")
    print("  Next: rebuild dataset + retrain + REVALIDATE on real data:")
    print("    python ml/dataset_builder_v3.py")
    print("    python backtest/walkforward_oos.py")
    print("=" * 64)


if __name__ == "__main__":
    main()
