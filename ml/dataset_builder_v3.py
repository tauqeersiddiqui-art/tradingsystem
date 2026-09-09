# ml/dataset_builder_v3.py
# Direction-PREDICTING dataset (first-touch barrier labels).
#
# WHY v3 EXISTS — the wrong-direction root cause:
#   v2 labelled CE only on Supertrend=UP bars and PE only on Supertrend=DOWN
#   bars, then trained each model on its own slice. The model therefore never
#   learned to PREDICT direction — it only learned "given the trend is already
#   up, will it continue?". At reversals (local tops/bottoms) the 1m Supertrend
#   flips the WRONG way, the model rubber-stamps it, and the trade goes
#   immediately negative. That is the "wrong direction most of the time" bug.
#
# v3 FIX:
#   * Label EVERY active-session bar (no direction-eligibility filter).
#   * First-touch barrier labels: for each bar, does price reach +TARGET
#     (CE wins) or -TARGET (PE wins) FIRST within LOOKAHEAD? This is a true
#     directional label — the model learns which way price breaks next.
#   * Supertrend / VWAP / ADX stay as FEATURES so the model can learn to
#     trust or distrust them, instead of being gated by them.
#
# Output is drop-in compatible with the live feature pipeline (same 36
# FEATURE_COLUMNS) so predictor_champion.py needs no change.
#
# RUN:
#   python ml/dataset_builder_v3.py
#   -> ml/models/training_dataset_v3.csv

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

# Reuse the EXACT feature computation from v2 (single source of truth).
from ml.dataset_builder_v2 import compute_all_features, _in_active_session

# NEW-30 features (probe_features_v4.add_new_features — the same definitions
# the live feature builder uses via feature_config.build_new_live_features).
from ml.probe_features_v4 import add_new_features

# SINGLE-CSV policy (2026-09-08): there is exactly ONE historical CSV in the
# system — data/historical/banknifty_1m_full.csv. It carries the full
# long history (463k+ rows / 2021-01-01 onwards, merged from
# banknifty_1m_history.csv by scripts/rebuild_history_from_dataset.py) AND is
# the file the live engine appends every completed candle to (HIST_CSV in
# .env / engine/config/config.py), so it is both the more-data file and the
# running-data file. banknifty_1m_history.csv is now a legacy artifact and is
# NOT referenced by any active code path.
# NOTE: OUTPUT below is the file the rebuild script recovers history FROM, so
# always keep a .bak of it before regenerating.
DATA_PATH = "data/historical/banknifty_1m_full.csv"
OUTPUT    = "ml/models/training_dataset_v3.csv"

# ── Label parameters ──────────────────────────────────────────────────
LOOKAHEAD          = 12      # candles to look forward
TARGET_SPOT_POINTS = 50      # barrier distance in spot points
START_DATE         = os.getenv("V3_START_DATE", "2021-01-01")  # regime relevance


def create_first_touch_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    First-touch barrier labels on EVERY active-session bar.

    For bar i, look at the next LOOKAHEAD bars:
      - up_hit   = first bar whose HIGH reaches close[i] + TARGET
      - down_hit = first bar whose LOW  reaches close[i] - TARGET
      label_ce = 1 if up_hit occurs first (CE would profit)
      label_pe = 1 if down_hit occurs first (PE would profit)
    A bar where neither barrier is touched gets both labels = 0 (chop/no-trade)
    so the model learns to output LOW probability there.

    THAT SHARED ZERO IS A DEFECT, and it is why the deployed champions have no
    directional edge. ~37% of bars touch neither barrier, so label_ce=0 AND
    label_pe=0 on the SAME rows. Both heads therefore maximise AUC by learning
    which bars are ACTIVE, not which way they go. Measured on the champions:
        corr(ce_adj, pe_adj)                          = +0.783
        AUC of (ce+pe) vs "any barrier touched"       =  0.754   <- what it learned
        AUC of (ce-pe) vs direction, touched rows     =  0.515   <- coin flip
        CE head 0.6446 (all rows) -> 0.5167 (touched only)
    argmax(ce_adj, pe_adj) over two activity forecasts carries no directional
    information, which is why 0/36 threshold cells and 0/32 geometry cells were
    net-positive over 243 clean OOS days.

    So this function now ALSO emits two DISENTANGLED labels:
        label_touch = 1 if either barrier is touched   (the learnable signal,
                      AUC ~0.76 — this is a volatility/activity forecast)
        label_dir   = 1 up-first, 0 down-first, NaN when untouched (the
                      conditional-direction target; train on touched rows only)
    label_ce/label_pe are left untouched for backward compatibility with the
    deployed trainer. New models should use label_touch + label_dir so the two
    questions are never fused into one degenerate pair again.

    Honest caveat: ml/probe_direction.py + ml/probe_features_v4.py show
    label_dir tops out at OOS AUC ~0.517 on these 35 features (and ~0.516 after
    adding 30 features of 6 new kinds), i.e. below the tradability bar. Fixing
    the label makes the problem MEASURABLE, not automatically profitable.
    """
    n      = len(df)
    close  = df["close"].values.astype(float)
    high   = df["high"].values.astype(float)
    low    = df["low"].values.astype(float)

    mins_from_midnight = df["date"].dt.hour * 60 + df["date"].dt.minute
    in_session = mins_from_midnight.apply(_in_active_session).values

    label_ce = np.zeros(n, dtype=np.int8)
    label_pe = np.zeros(n, dtype=np.int8)
    # Disentangled targets: activity is separate from direction.
    label_touch = np.zeros(n, dtype=np.int8)
    label_dir = np.full(n, np.nan)          # NaN on untouched bars, by design

    tgt = TARGET_SPOT_POINTS
    for i in range(n - LOOKAHEAD):
        if not in_session[i]:
            continue
        up_target = close[i] + tgt
        dn_target = close[i] - tgt
        up_hit = dn_hit = None
        for j in range(i + 1, i + LOOKAHEAD + 1):
            if up_hit is None and high[j] >= up_target:
                up_hit = j
            if dn_hit is None and low[j] <= dn_target:
                dn_hit = j
            if up_hit is not None and dn_hit is not None:
                break
        if up_hit is not None and (dn_hit is None or up_hit <= dn_hit):
            label_ce[i] = 1
            label_touch[i] = 1
            label_dir[i] = 1.0
        elif dn_hit is not None and (up_hit is None or dn_hit < up_hit):
            label_pe[i] = 1
            label_touch[i] = 1
            label_dir[i] = 0.0

    df["label_ce"] = label_ce
    df["label_pe"] = label_pe
    df["label_touch"] = label_touch
    df["label_dir"] = label_dir
    # Kept for compatibility with any downstream code; in v3 every bar is
    # eligible for both directions (the model decides).
    df["ce_eligible"] = in_session
    df["pe_eligible"] = in_session
    return df


def main():
    print("=" * 64)
    print("  DIRECTIONAL DATASET BUILDER v3  (first-touch barrier labels)")
    print("=" * 64)
    print(f"  Target={TARGET_SPOT_POINTS}pt  Lookahead={LOOKAHEAD}  Start={START_DATE}")

    print(f"\n[DATA] Loading {DATA_PATH} ...")
    df = pd.read_csv(DATA_PATH)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    if "volume" not in df.columns:
        df["volume"] = 0.0

    df = df[df["date"] >= pd.Timestamp(START_DATE)].reset_index(drop=True)
    print(f"[DATA] {len(df):,} rows | {df['date'].min().date()} -> {df['date'].max().date()}")

    print("[FEATURES] Computing indicators ...")
    df = compute_all_features(df)

    print("[FEATURES] Adding 30 multi-timeframe / context features ...")
    df, _new_cols = add_new_features(df)
    # Rolling-window features are NaN/Inf at the start of the series and
    # around overnight gaps — fill with 0 (model trains with fillna(0)).
    _feat_cols = [c for c in _new_cols if c in df.columns]
    df[_feat_cols] = df[_feat_cols].replace([np.inf, -np.inf], np.nan)
    df[_feat_cols] = df[_feat_cols].fillna(0.0)

    print("[LABELS] First-touch directional labels (this is the slow part) ...")
    df = create_first_touch_labels(df)

    # label_dir is NaN BY DESIGN on untouched bars (see create_first_touch_labels).
    # A bare df.dropna() would therefore delete every chop bar — it cut 463,331
    # rows to 291,302 and made label_touch read 100%, destroying the very
    # distinction this label exists to expose. Drop on feature/label columns only.
    _keep_nan = ["label_dir"]
    _subset = [c for c in df.columns if c not in _keep_nan]
    before = len(df)
    df = df.dropna(subset=_subset).reset_index(drop=True)
    print(f"  dropna: {before:,} -> {len(df):,} rows "
          f"(label_dir NaNs preserved: {int(df['label_dir'].isna().sum()):,})")

    ce_rate = df["label_ce"].mean()
    pe_rate = df["label_pe"].mean()
    flat    = 1.0 - ce_rate - pe_rate
    print(f"\n  Bars: {len(df):,}")
    print(f"  label_ce=1 : {ce_rate:.1%}   label_pe=1 : {pe_rate:.1%}   neither(flat): {flat:.1%}")
    print("  (CE+PE should be roughly balanced; large skew = directional bias in data)")
    print(f"  label_touch=1 : {df['label_touch'].mean():.1%}   "
          f"label_dir usable rows : {int(df['label_dir'].notna().sum()):,}")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    df.to_csv(OUTPUT, index=False)
    print(f"\n[SAVED] {OUTPUT}  ({len(df):,} rows)")
    print("  Next: python ml/trainer_v3.py")
    print("=" * 64)


if __name__ == "__main__":
    main()
