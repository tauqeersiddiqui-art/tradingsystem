# ml/dataset_builder_v3.py
# COST-AWARE direction dataset (P&L-positive barrier labels).
#
# WHY THIS CHANGED (the negative-expectancy root cause):
#   The previous v3 label asked only "does SPOT touch +/-TARGET first?".
#   But trades are OPTIONS. A correct spot direction still LOSES money once
#   the round-trip spread, theta decay over the hold, and brokerage are paid.
#   That is why OOS_AUC was ~0.90 while every threshold was -EV after costs.
#
# FIX:
#   A bar is labeled 1 only if a REALISTICALLY-COSTED option trade taken at
#   that bar would be NET-POSITIVE within LOOKAHEAD bars. The simulation uses
#   the SAME OptionPriceSimulator and _cost_rs the backtest/live engine use,
#   so the label and the P&L finally measure the same thing.
#
# RUN:
#   python ml/dataset_builder_v3.py
#   -> ml/models/training_dataset_v3.csv

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from ml.dataset_builder_v2 import compute_all_features, _in_active_session
from backtest.backtest_engine import OptionPriceSimulator, _mins_to_close
from engine.execution.profit_manager import _cost_rs

DATA_PATH = "data/historical/nifty_1m_full.csv"
OUTPUT    = "ml/models/training_dataset_v3.csv"

# ── Label parameters ───────────────────────────────────────
# IMPORTANT: keep these IN SYNC with backtest/walkforward_oos.py.
LOOKAHEAD          = int(os.getenv("V3_LOOKAHEAD", "12"))   # candles forward
SPREAD_PTS         = float(os.getenv("BT_SPREAD_PTS", "1.0"))  # round-trip option spread (premium pts)
LOT_UNITS          = int(os.getenv("V3_LOT_UNITS", "65"))
MIN_NET_RS         = float(os.getenv("V3_MIN_NET_RS", "0.0"))  # profit floor; raise to demand margin
START_DATE         = os.getenv("V3_START_DATE", "2021-01-01")

_opt = OptionPriceSimulator()


def _best_net_for_side(close, ts, i, side):
    """
    Simulate an option trade entered at bar i for `side`, held up to LOOKAHEAD
    bars, exiting at the BEST achievable premium in that window. Returns net Rs
    after round-trip spread + brokerage. (Best-exit = optimistic ceiling: if the
    label is still rarely +EV under this generous assumption, the edge truly
    isn't there.)
    """
    es  = float(close[i])
    entry_prem = _opt.premium(es, es, side, _mins_to_close(pd.Timestamp(ts[i]))) + SPREAD_PTS / 2.0

    best_exit = -1e18
    for j in range(i + 1, i + 1 + LOOKAHEAD):
        prem = _opt.premium(es, float(close[j]), side, _mins_to_close(pd.Timestamp(ts[j])))
        if prem > best_exit:
            best_exit = prem
    exit_prem = best_exit - SPREAD_PTS / 2.0
    return (exit_prem - entry_prem) * LOT_UNITS - _cost_rs(LOT_UNITS)


def create_first_touch_labels(df: pd.DataFrame) -> pd.DataFrame:
    """
    COST-AWARE labels on EVERY active-session bar.

    For bar i, simulate both a CE and a PE option trade over the next LOOKAHEAD
    bars (same pricing + costs as the engine):
      label_ce = 1 if the CE trade is net-positive (> MIN_NET_RS)
      label_pe = 1 if the PE trade is net-positive (> MIN_NET_RS)
    Both can be 0 (chop / no profitable trade) so the model learns to stay out.
    """
    n     = len(df)
    close = df["close"].values.astype(float)
    ts    = df["date"].values

    mins_from_midnight = df["date"].dt.hour * 60 + df["date"].dt.minute
    in_session = mins_from_midnight.apply(_in_active_session).values

    label_ce = np.zeros(n, dtype=np.int8)
    label_pe = np.zeros(n, dtype=np.int8)

    for i in range(n - LOOKAHEAD):
        if not in_session[i]:
            continue
        ce_net = _best_net_for_side(close, ts, i, "CE")
        pe_net = _best_net_for_side(close, ts, i, "PE")
        if ce_net > MIN_NET_RS:
            label_ce[i] = 1
        if pe_net > MIN_NET_RS:
            label_pe[i] = 1

    df["label_ce"] = label_ce
    df["label_pe"] = label_pe
    df["ce_eligible"] = in_session
    df["pe_eligible"] = in_session
    return df


def main():
    print("=" * 64)
    print("  COST-AWARE DATASET BUILDER v3  (P&L-positive barrier labels)")
    print("=" * 64)
    print(f"  Lookahead={LOOKAHEAD}  Spread={SPREAD_PTS}pt  Lot={LOT_UNITS}  "
          f"MinNet=Rs{MIN_NET_RS}  Start={START_DATE}")

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

    print("[LABELS] Cost-aware option-P&L labels (slow: prices every bar) ...")
    df = create_first_touch_labels(df)

    df = df.dropna().reset_index(drop=True)

    ce_rate = df["label_ce"].mean()
    pe_rate = df["label_pe"].mean()
    both    = ((df["label_ce"] == 1) & (df["label_pe"] == 1)).mean()
    flat    = ((df["label_ce"] == 0) & (df["label_pe"] == 0)).mean()
    print(f"\n  Bars: {len(df):,}")
    print(f"  label_ce=1 : {ce_rate:.1%}   label_pe=1 : {pe_rate:.1%}")
    print(f"  both=1 : {both:.1%}   neither(flat): {flat:.1%}")
    print("  (A LOW positive rate here is expected & honest: few bars are")
    print("   profitable after option frictions. That is the point.)")

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    df.to_csv(OUTPUT, index=False)
    print(f"\n[SAVED] {OUTPUT}  ({len(df):,} rows)")
    print("  Next: python ml/trainer_v3.py  then  python backtest/walkforward_oos.py")
    print("=" * 64)


if __name__ == "__main__":
    main()
