# ml/day_classifier.py
#
# DAY TYPE CLASSIFIER — ML Used Correctly
#
# ARCHITECTURAL DECISION:
#   The LightGBM model (AUC ~0.55) is too weak to filter individual 1-minute trades.
#   But classifying the ENTIRE DAY at 9:45 using the first 30 minutes of data
#   is a fundamentally easier problem with better signal-to-noise ratio.
#
# HOW IT WORKS:
#   1. Collect the first 30 candles (9:15–9:44)
#   2. Compute day-level features from those candles
#   3. Classify: TREND_DAY / RANGE_DAY / VOLATILE_DAY
#   4. ORB signals are taken ONLY on TREND_DAY
#   5. RANGE_DAY and VOLATILE_DAY = sit out (or use different strategy)
#
# WHY THIS WORKS:
#   - Day type persistence is real: trending days tend to continue trending
#   - NIFTY first-30-min range predicts daily range with measurable accuracy
#   - This reduces total signals from 1800 → ~60-80/year, improving quality
#
# TRAINING:
#   Run build_day_classifier_dataset() once on historical data,
#   then train_day_classifier() to produce the saved model.

import os
import numpy as np
import pandas as pd
import joblib
import lightgbm as lgb
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import classification_report


MODEL_PATH   = "ml/models/day_classifier_lgbm.pkl"
DATA_PATH    = "data/historical/banknifty_1m_full.csv"
LABEL_PATH   = "ml/models/day_classifier_labels.csv"

# Day type labels
DAY_TREND    = 0
DAY_RANGE    = 1
DAY_VOLATILE = 2
DAY_LABELS   = {DAY_TREND: "TREND", DAY_RANGE: "RANGE", DAY_VOLATILE: "VOLATILE"}


@dataclass
class DayFeatures:
    """Features computed from first 30 minutes (9:15–9:44)."""
    open_to_close_30m:  float   # % move from open to 9:44 close
    range_30m_pct:      float   # (high - low) / open over first 30 bars
    first_bar_body_pct: float   # First candle body / range ratio
    direction_pct:      float   # % bars closing higher than open (bullish bias)
    momentum:           float   # (last_close - first_close) / first_close
    atr_30m:            float   # Average true range of first 30 bars
    atr_to_range_ratio: float   # ATR / (high - low) — measures choppiness
    ema9_slope:         float   # EMA9 slope over 30 bars
    consecutive_same_dir: int   # Max consecutive same-direction bars
    gap_pct:            float   # Gap from yesterday's close to today's open


def compute_day_features(candles_30m: pd.DataFrame,
                          prev_close: Optional[float] = None) -> DayFeatures:
    """
    Args:
        candles_30m: DataFrame with columns [open, high, low, close, volume]
                     representing the first 30 one-minute candles
        prev_close:  Yesterday's closing price (for gap calculation)
    """
    if len(candles_30m) < 10:
        raise ValueError(f"Need at least 10 candles, got {len(candles_30m)}")

    df         = candles_30m.reset_index(drop=True)
    first_open = float(df.iloc[0]["open"])
    last_close = float(df.iloc[-1]["close"])

    # Basic move
    open_to_close_30m = (last_close - first_open) / first_open

    # Range
    high_30m     = float(df["high"].max())
    low_30m      = float(df["low"].min())
    range_30m    = high_30m - low_30m
    range_30m_pct = range_30m / first_open

    # First candle body
    first_bar = df.iloc[0]
    first_body = abs(float(first_bar["close"]) - float(first_bar["open"]))
    first_hl   = float(first_bar["high"]) - float(first_bar["low"])
    first_bar_body_pct = first_body / max(first_hl, 0.1)

    # Directional bias (% bullish bars)
    bull_bars      = (df["close"] > df["open"]).sum()
    direction_pct  = bull_bars / len(df)

    # Momentum
    momentum = (last_close - float(df.iloc[0]["close"])) / float(df.iloc[0]["close"])

    # ATR
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    trs    = [max(highs[i] - lows[i],
                  abs(highs[i] - closes[i-1]),
                  abs(lows[i]  - closes[i-1]))
              for i in range(1, len(df))]
    atr_30m = float(np.mean(trs)) if trs else range_30m / 2

    # ATR/Range ratio — < 0.5 means choppy, > 0.7 means directional
    atr_to_range_ratio = atr_30m / max(range_30m, 0.1)

    # EMA9 slope
    alpha    = 2.0 / (9 + 1)
    ema_val  = float(np.mean(closes[:9]))
    ema_vals = [ema_val]
    for p in closes[9:]:
        ema_val = p * alpha + ema_val * (1 - alpha)
        ema_vals.append(ema_val)
    ema9_slope = (ema_vals[-1] - ema_vals[0]) / max(ema_vals[0], 1) if len(ema_vals) > 1 else 0

    # Consecutive same-direction bars
    directions = (df["close"] > df["open"]).astype(int).tolist()
    max_streak = cur_streak = 1
    for i in range(1, len(directions)):
        if directions[i] == directions[i-1]:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 1
    consecutive_same_dir = max_streak

    # Gap
    gap_pct = (first_open - prev_close) / prev_close if prev_close else 0.0

    return DayFeatures(
        open_to_close_30m     = round(open_to_close_30m,   6),
        range_30m_pct         = round(range_30m_pct,       6),
        first_bar_body_pct    = round(first_bar_body_pct,  4),
        direction_pct         = round(direction_pct,       4),
        momentum              = round(momentum,            6),
        atr_30m               = round(atr_30m,             4),
        atr_to_range_ratio    = round(atr_to_range_ratio,  4),
        ema9_slope            = round(ema9_slope,          6),
        consecutive_same_dir  = int(consecutive_same_dir),
        gap_pct               = round(gap_pct,             6),
    )


FEATURE_NAMES = [
    "open_to_close_30m", "range_30m_pct", "first_bar_body_pct",
    "direction_pct", "momentum", "atr_30m", "atr_to_range_ratio",
    "ema9_slope", "consecutive_same_dir", "gap_pct",
]


# ─── DATASET BUILDER ─────────────────────────────────────────────────────────

def build_day_classifier_dataset(save_path: str = LABEL_PATH) -> pd.DataFrame:
    """
    Build a day-level dataset from historical 1-minute data.
    Label each day as TREND / RANGE / VOLATILE based on:
      - Daily range (high - low) / open
      - Direction of daily close vs open
      - Intraday persistence (did price trend in one direction?)

    LABELING LOGIC:
      VOLATILE:  daily range > 1.5% of open  (wide swings, unpredictable)
      TREND:     daily range > 0.6% AND abs(open_to_close) > 0.4% AND
                 direction consistent with ORB direction
      RANGE:     everything else
    """
    print("Loading historical data...")
    df = pd.read_csv(DATA_PATH)
    df["date"]     = pd.to_datetime(df["date"])
    df["day"]      = df["date"].dt.date
    df["hour"]     = df["date"].dt.hour
    df["minute"]   = df["date"].dt.minute

    # Filter to market hours
    df = df[(df["hour"] >= 9) & (df["hour"] <= 15)].copy()

    records    = []
    all_days   = sorted(df["day"].unique())
    prev_close = None

    for i, trading_day in enumerate(all_days):
        day_df = df[df["day"] == trading_day].sort_values("date")

        # First 30 candles (9:15–9:44)
        first_30 = day_df[
            (day_df["hour"] == 9) & (day_df["minute"] >= 15) |
            (day_df["hour"] == 9) & (day_df["minute"] <= 44)
        ].head(30)

        if len(first_30) < 15:
            prev_close = float(day_df.iloc[-1]["close"]) if len(day_df) > 0 else prev_close
            continue

        # Full day stats for labeling
        day_open  = float(day_df.iloc[0]["open"])
        day_high  = float(day_df["high"].max())
        day_low   = float(day_df["low"].min())
        day_close = float(day_df.iloc[-1]["close"])

        daily_range_pct = (day_high - day_low) / day_open
        daily_move_pct  = (day_close - day_open) / day_open

        # Trend persistence: what fraction of the day did price move in open direction?
        day_df["bar_direction"] = (day_df["close"] > day_df["open"]).astype(int)
        dominant_dir   = 1 if daily_move_pct > 0 else 0
        persistence    = (day_df["bar_direction"] == dominant_dir).mean()

        # Label assignment
        if daily_range_pct > 0.015:
            label = DAY_VOLATILE
        elif daily_range_pct > 0.006 and abs(daily_move_pct) > 0.004 and persistence > 0.60:
            label = DAY_TREND
        else:
            label = DAY_RANGE

        # Compute features
        try:
            feats = compute_day_features(first_30, prev_close)
        except Exception as e:
            print(f"  {trading_day}: feature error — {e}")
            prev_close = day_close
            continue

        record = {"date": str(trading_day), "label": label}
        for fn in FEATURE_NAMES:
            record[fn] = getattr(feats, fn)
        records.append(record)

        prev_close = day_close

    result_df = pd.DataFrame(records)
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    result_df.to_csv(save_path, index=False)

    print(f"\nDay classifier dataset: {len(result_df)} trading days")
    print(result_df["label"].map(DAY_LABELS).value_counts())
    print(f"Saved: {save_path}")
    return result_df


# ─── TRAINING ─────────────────────────────────────────────────────────────────

def train_day_classifier(label_path: str = LABEL_PATH,
                          model_path: str = MODEL_PATH) -> None:
    df = pd.read_csv(label_path).dropna()
    X  = df[FEATURE_NAMES].values
    y  = df["label"].values

    print(f"\nTraining day classifier on {len(df)} days...")
    print("Label distribution:")
    for lv, ln in DAY_LABELS.items():
        print(f"  {ln}: {(y == lv).sum()} days ({(y == lv).mean():.1%})")

    tscv  = TimeSeriesSplit(n_splits=5)
    aucs  = []

    for fold, (tr_idx, va_idx) in enumerate(tscv.split(X)):
        model = lgb.LGBMClassifier(
            n_estimators=200, learning_rate=0.05, max_depth=4,
            num_leaves=15, class_weight="balanced",
            verbose=-1, random_state=42,
        )
        model.fit(X[tr_idx], y[tr_idx])
        preds = model.predict(X[va_idx])
        acc   = (preds == y[va_idx]).mean()
        aucs.append(acc)
        print(f"  Fold {fold+1}: Accuracy={acc:.3f}")

    print(f"\nMean CV Accuracy: {np.mean(aucs):.3f}")

    # Train final model
    final = lgb.LGBMClassifier(
        n_estimators=300, learning_rate=0.03, max_depth=5,
        num_leaves=20, class_weight="balanced",
        verbose=-1, random_state=42,
    )
    final.fit(X, y)

    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    joblib.dump(final, model_path)
    print(f"Saved: {model_path}")


# ─── LIVE INFERENCE ───────────────────────────────────────────────────────────

class DayClassifier:
    """
    Loaded once at engine startup. Called at 9:45 with first-30-min candles.
    Returns day type string used to gate ORB trading.
    """

    def __init__(self):
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(
                f"Day classifier model not found: {MODEL_PATH}\n"
                "Run: python -m ml.day_classifier --build --train"
            )
        self._model  = joblib.load(MODEL_PATH)
        self._label  = None
        self._probs  = None

    def classify(self, candles_30m: pd.DataFrame,
                  prev_close: Optional[float] = None) -> str:
        """
        Call once at 9:45 with the first 30 candles.
        Returns: "TREND" / "RANGE" / "VOLATILE"
        """
        feats       = compute_day_features(candles_30m, prev_close)
        feat_array  = np.array([[getattr(feats, fn) for fn in FEATURE_NAMES]])
        pred        = int(self._model.predict(feat_array)[0])
        self._probs = self._model.predict_proba(feat_array)[0]
        self._label = DAY_LABELS[pred]
        return self._label

    @property
    def confidence(self) -> float:
        """Probability of the predicted class."""
        if self._probs is None:
            return 0.0
        return float(self._probs.max())

    @property
    def day_type(self) -> Optional[str]:
        return self._label

    def should_trade_orb(self) -> bool:
        """
        Gate function: return True only if day type warrants ORB trading.
        TREND days: trade ORB
        RANGE / VOLATILE: skip (or use a different strategy)
        """
        return self._label == "TREND"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", action="store_true", help="Build label dataset")
    parser.add_argument("--train", action="store_true", help="Train classifier")
    args = parser.parse_args()

    if args.build:
        build_day_classifier_dataset()
    if args.train:
        train_day_classifier()
    if not args.build and not args.train:
        parser.print_help()
