# fetch_banknifty_10y.py
#
# Fetch ~10 years of BANKNIFTY 1-minute candles from the Zerodha historical
# API into data/historical/banknifty_1m_full.csv (the CSV the engine uses).
#
# WHY THE OLD VERSION FAILED
#   1) It imported master_runner, which needs KITE_ACCESS_TOKEN at import
#      time and drags in the whole engine. Access tokens expire DAILY, so
#      the script died on init_broker() unless a fresh login was done.
#   2) fetch_banknifty_history.py called update_historical_data() with
#      start/end kwargs — but that helper only accepts lookback_days and
#      always fetches the LATEST days, so the loop never walked back in time.
#
# THIS VERSION
#   - Standalone: reads KITE_API_KEY / KITE_ACCESS_TOKEN from .env directly.
#   - Walks backward from today in 59-day chunks (API max is 60 for minute).
#   - Normalizes every date to "YYYY-MM-DD HH:MM:SS" (no ISO-T mixing),
#     dedupes on date, sorts, and writes atomically.
#   - Survives empty chunks (Zerodha minute data does not always reach back
#     10 years — the script reports how far it actually got).
#
# USAGE
#   python login_headless.py            # refresh the access token FIRST
#   python fetch_banknifty_10y.py       # then fetch (add --years 2 for less)
#
# Rate limits: historical API allows ~3 req/s; we sleep 0.35s per chunk.

import os
import sys
import time
import shutil
import argparse
import datetime as dt

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────
API_KEY      = (os.getenv("KITE_API_KEY") or os.getenv("ZERODHA_API_KEY") or "").strip()
ACCESS_TOKEN = (os.getenv("KITE_ACCESS_TOKEN") or os.getenv("ZERODHA_ACCESS_TOKEN") or "").strip()
INDEX_TOKEN  = int(os.getenv("INDEX_TOKEN", "260105"))          # NIFTY BANK
TARGET_CSV   = os.getenv("HIST_CSV", "data/historical/banknifty_1m_full.csv")

CHUNK_DAYS    = 59        # Zerodha max for minute interval = 60; stay under
SLEEP_BETWEEN = 0.35      # seconds (3 req/s API limit)

DATE_FMT = "%Y-%m-%d %H:%M:%S"


def log(msg: str) -> None:
    print(f"{dt.datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)


def to_naive_ist(series: pd.Series) -> pd.Series:
    """Kite returns tz-aware IST datetimes — strip to naive local strings."""
    s = pd.to_datetime(series)
    try:
        if s.dt.tz is not None:
            s = s.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    except (TypeError, AttributeError):
        pass
    return s


def load_existing() -> pd.DataFrame:
    if not os.path.exists(TARGET_CSV):
        return pd.DataFrame()
    try:
        df = pd.read_csv(TARGET_CSV)
        df["date"] = to_naive_ist(df["date"])
        df["date"] = df["date"].dt.strftime(DATE_FMT)
        return df
    except Exception as e:
        log(f"WARNING: could not parse existing CSV ({e}) — starting fresh buffer")
        return pd.DataFrame()


def save_atomic(df: pd.DataFrame) -> None:
    """Dedupe + sort + atomic replace so a crash never corrupts the CSV."""
    df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    tmp = TARGET_CSV + ".tmp"
    df.to_csv(tmp, index=False)
    shutil.move(tmp, TARGET_CSV)


def fetch_chunk(kite, start: dt.datetime, end: dt.datetime):
    """One historical_data call with automatic chunk-shrink on API errors."""
    chunk = (end - start).days + 1
    while True:
        try:
            return kite.historical_data(INDEX_TOKEN, start, end, "minute", oi=False)
        except Exception as e:
            msg = str(e).lower()
            if ("too big" in msg or "exceed" in msg or "max" in msg) and chunk > 2:
                chunk = max(2, chunk // 2)
                end = start + dt.timedelta(days=chunk - 1)
                log(f"  API interval limit — retrying with {chunk}-day chunk")
                time.sleep(0.35)
                continue
            raise


def main() -> None:
    global TARGET_CSV
    ap = argparse.ArgumentParser(description="Fetch 10y of BANKNIFTY 1m candles")
    ap.add_argument("--years", type=int, default=10, help="how many years back (default 10)")
    ap.add_argument("--out", default=TARGET_CSV, help="target CSV path")
    args = ap.parse_args()

    TARGET_CSV = args.out

    if not API_KEY or not ACCESS_TOKEN:
        log("ERROR: KITE_API_KEY / KITE_ACCESS_TOKEN missing in .env")
        log("Run:  python login_headless.py   # (tokens expire daily)")
        sys.exit(2)

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)

    # Verify auth before spending minutes on history
    try:
        profile = kite.profile()
        log(f"Auth OK — {profile.get('user_id')} / {profile.get('user_name', '')}")
    except Exception as e:
        log(f"ERROR: access token invalid or expired ({e})")
        log("Run:  python login_headless.py   # then retry")
        sys.exit(3)

    os.makedirs(os.path.dirname(TARGET_CSV) or ".", exist_ok=True)
    existing = load_existing()
    log(f"Existing CSV: {len(existing)} rows"
        + (f" ({existing['date'].min()} → {existing['date'].max()})" if len(existing) else ""))

    end_target  = dt.datetime.now()
    start_floor = end_target - dt.timedelta(days=int(args.years * 365.25))

    cursor_end   = end_target
    cursor_start = max(start_floor, cursor_end - dt.timedelta(days=CHUNK_DAYS - 1))

    total_new = 0
    empty_streak = 0
    earliest_seen = None

    while cursor_end > start_floor:
        label = f"{cursor_start.date()} → {cursor_end.date()}"
        try:
            raw = fetch_chunk(kite, cursor_start, cursor_end)
        except Exception as e:
            log(f"Chunk {label} FAILED: {e}")
            log("Aborting to protect the CSV — re-run to resume (fetched chunks")
            log("are safe; the script dedupes on date).")
            sys.exit(4)

        if raw:
            new_df = pd.DataFrame(raw)
            if "date" not in new_df.columns:
                log(f"Chunk {label}: unexpected payload (no 'date' column) — skipped")
                empty_streak += 1
            else:
                new_df["date"] = to_naive_ist(new_df["date"])
                new_df["date"] = new_df["date"].dt.strftime(DATE_FMT)
                before = len(existing)
                combined = (pd.concat([existing, new_df], ignore_index=True)
                            if len(existing) else new_df)
                save_atomic(combined)
                existing = combined
                got = len(existing) - before
                total_new += got
                earliest_seen = existing["date"].min()
                log(f"{label}: +{got:>6} rows  (total {len(existing)}, back to {earliest_seen})")
                empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak == 1:
                log(f"{label}: empty (no minute data this far back or market holiday)")
            elif empty_streak == 5:
                log("5 consecutive empty chunks — Zerodha minute history likely")
                log(f"ends near {earliest_seen or cursor_end}. Stopping here.")
                break

        cursor_end   = cursor_start - dt.timedelta(days=1)
        cursor_start = max(start_floor, cursor_end - dt.timedelta(days=CHUNK_DAYS - 1))
        time.sleep(SLEEP_BETWEEN)

    # ── Final validation report ────────────────────────────────────────
    log("")
    log("=" * 70)
    log("FETCH COMPLETE — validation")
    log(f"  new rows added : {total_new}")
    if len(existing):
        d       = pd.to_datetime(existing["date"])
        days    = d.dt.date.nunique()
        per_day = d.dt.date.value_counts()
        log(f"  total rows     : {len(existing):,}")
        log(f"  date range     : {d.min()}  →  {d.max()}")
        log(f"  distinct days  : {days}  (avg {per_day.mean():.0f} candles/day)")
        log(f"  full days (>=300 candles): {(per_day >= 300).sum()}")
    else:
        log("  CSV is empty — nothing fetched.")
    log("=" * 70)


if __name__ == "__main__":
    main()