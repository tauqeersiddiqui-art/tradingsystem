# scripts/rebuild_history_from_dataset.py
#
# Reconstruct the long-history raw OHLC CSV that dataset_builder_v3.py needs.
#
# WHY THIS EXISTS
#   The builder reads DATA_PATH = data/historical/nifty_1m_full.csv, but that
#   file was overwritten with the same 47-day 2026 BANKNIFTY window as
#   banknifty_1m_full.csv (both: 2026-01-09 -> 2026-09-04, close 56064-58587).
#   The 451,260-row / 1,211-day history the deployed champions were trained on
#   (2021-01-01 -> 2026-09-02, close 29,720-58,571) survives only inside
#   ml/models/training_dataset_v3.csv, which carries date+OHLC alongside the
#   35 features.
#
#   So the history is NOT lost and does NOT need a broker re-fetch: lift the
#   OHLC columns back out of the dataset, union them with whatever the 2026
#   CSV adds, and write a single canonical file the builder can point at.
#
# WHAT IT DOES NOT FIX
#   volume is 0 throughout (it is 0 in both sources), so volume_ratio stays
#   degenerate. Real volume needs the historical API.
#
# RUN: python scripts/rebuild_history_from_dataset.py [--dry-run]

import os
import sys
import shutil
import argparse
import datetime as dt

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATASET = os.path.join(_ROOT, "ml", "models", "training_dataset_v3.csv")
_BN_CSV = os.path.join(_ROOT, "data", "historical", "banknifty_1m_full.csv")
_OUT = os.path.join(_ROOT, "data", "historical", "banknifty_1m_history.csv")

_COLS = ["date", "open", "high", "low", "close", "volume"]
DATE_FMT = "%Y-%m-%d %H:%M:%S"


def log(m):
    print(f"  {m}", flush=True)


def load_source(path, tag):
    """Read date+OHLC(+volume) from any CSV that has them."""
    if not os.path.exists(path):
        log(f"{tag}: MISSING ({path})")
        return pd.DataFrame(columns=_COLS)
    head = pd.read_csv(path, nrows=0).columns.tolist()
    need = [c for c in _COLS if c in head]
    missing = [c for c in ("date", "open", "high", "low", "close") if c not in head]
    if missing:
        log(f"{tag}: unusable — missing {missing}")
        return pd.DataFrame(columns=_COLS)
    df = pd.read_csv(path, usecols=need)
    if "volume" not in df.columns:
        df["volume"] = 0
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).drop_duplicates("date")
    log(f"{tag}: {len(df):,} rows | {df['date'].min()} -> {df['date'].max()} "
        f"| {df['date'].dt.date.nunique()} days | close "
        f"{df['close'].min():.0f}-{df['close'].max():.0f}")
    return df[_COLS]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=_OUT)
    args = ap.parse_args()

    print("=" * 72)
    print("  REBUILD LONG-HISTORY OHLC FROM training_dataset_v3.csv")
    print("=" * 72)
    print("\n[1] sources")
    hist = load_source(_DATASET, "training_dataset_v3 (long history)")
    live = load_source(_BN_CSV, "banknifty_1m_full (2026 window)")

    if hist.empty:
        print("\nABORT: the dataset carries no usable OHLC — nothing to rebuild from.")
        sys.exit(2)

    print("\n[2] union (dataset wins on overlap — it is the trained-on series)")
    merged = pd.concat([live, hist], ignore_index=True)
    merged = (merged.sort_values("date")
                    .drop_duplicates("date", keep="last")
                    .sort_values("date")
                    .reset_index(drop=True))
    log(f"merged: {len(merged):,} rows | {merged['date'].min()} -> "
        f"{merged['date'].max()} | {merged['date'].dt.date.nunique()} days")

    print("\n[3] integrity")
    bad = merged[(merged["high"] < merged["low"])
                 | (merged["close"] > merged["high"])
                 | (merged["close"] < merged["low"])]
    log(f"OHLC violations: {len(bad)}")
    log(f"non-zero volume rows: {int((merged['volume'] != 0).sum()):,} "
        f"of {len(merged):,}   <-- 0 means volume_ratio stays dead")
    per_year = merged["date"].dt.year.value_counts().sort_index()
    for y, n in per_year.items():
        d = merged[merged["date"].dt.year == y]["date"].dt.date.nunique()
        log(f"{y}: {n:>7,} rows  {d:>4} days")

    if args.dry_run:
        print("\n[dry-run] nothing written.")
        return

    print("\n[4] write")
    out = merged.copy()
    out["date"] = out["date"].dt.strftime(DATE_FMT)
    if os.path.exists(args.out):
        bak = f"{args.out}.bak_{dt.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(args.out, bak)
        log(f"backed up existing -> {os.path.basename(bak)}")
    tmp = args.out + ".tmp"
    out.to_csv(tmp, index=False)
    shutil.move(tmp, args.out)
    log(f"wrote {args.out}  ({len(out):,} rows)")

    print("\n" + "=" * 72)
    print("  NEXT: point ml/dataset_builder_v3.py DATA_PATH at")
    print(f"        {os.path.relpath(args.out, _ROOT).replace(os.sep, '/')}")
    print("=" * 72)


if __name__ == "__main__":
    main()
