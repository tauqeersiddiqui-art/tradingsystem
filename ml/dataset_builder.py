import pandas as pd
import numpy as np
from datetime import datetime

DATA_PATH = "data/historical/nifty_1m_full.csv"
OUTPUT = "ml/models/training_dataset.csv"


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))


def compute_atr(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs()
    ], axis=1).max(axis=1)

    return tr.rolling(period).mean()


def build_features(df):

    # ===== CORE =====
    df["ema20"] = df["close"].ewm(span=20).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()
    df["macd"]  = df["ema20"] - df["ema50"]

    df["returns"] = df["close"].pct_change()
    df["return_1"] = df["returns"]
    df["return_3"] = df["close"].pct_change(3)

    df["volatility"] = df["returns"].rolling(20).std()
    df["rsi"] = compute_rsi(df["close"])

    df["atr"] = compute_atr(df)

    df["trend_strength"] = (df["ema20"] - df["ema50"]) / df["close"]

    # ===== TIME =====
    df["hour"] = df["date"].dt.hour
    df["weekday"] = df["date"].dt.weekday

    market_open = df["date"].dt.normalize() + pd.Timedelta(hours=9, minutes=15)
    market_close = df["date"].dt.normalize() + pd.Timedelta(hours=15, minutes=30)

    df["mins_since_open"] = (df["date"] - market_open).dt.total_seconds() / 60
    df["mins_to_close"]   = (market_close - df["date"]).dt.total_seconds() / 60

    df["session_open"]  = (df["mins_since_open"] < 30).astype(int)
    df["session_close"] = (df["mins_to_close"] < 60).astype(int)

    # ===== OPTIONS FEATURES =====
    df["moneyness"] = (df["close"] - df["ema20"]) / df["close"]
    df["time_to_expiry_min"] = df["mins_to_close"].clip(0, 375)

    # ===== CANDLE STRUCTURE =====
    hl = df["high"] - df["low"]
    body = abs(df["close"] - df["open"])

    df["candle_body_pct"] = body / (hl + 1e-6)

    rolling_high = df["high"].rolling(10).max()
    df["range_break_strength"] = (df["close"] - rolling_high) / (df["atr"] + 1e-6)

    # ===== ADVANCED =====
    df["momentum_velocity"] = df["returns"].diff()

    r5  = df["high"].rolling(5).max()  - df["low"].rolling(5).min()
    r15 = df["high"].rolling(15).max() - df["low"].rolling(15).min()
    df["range_compression"] = r5 / (r15 + 1e-6)

    wick = hl - body
    df["wick_ratio"] = wick / (body + 1e-6)
    df["body_efficiency"] = body / (hl + 1e-6)

    df["mom3_strength"] = df["close"].pct_change(3)

    df["upper_wick"] = (df["high"] - df[["close","open"]].max(axis=1)) / (df["atr"] + 1e-6)
    df["lower_wick"] = (df[["close","open"]].min(axis=1) - df["low"]) / (df["atr"] + 1e-6)

    df["close_position"] = (df["close"] - df["low"]) / (hl + 1e-6)

    return df


def main():
    df = pd.read_csv(DATA_PATH)

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df = df.sort_values("date")

    df = build_features(df)

    df = df.dropna()

    df.to_csv(OUTPUT, index=False)
    print("✅ FIXED dataset →", OUTPUT)


if __name__ == "__main__":
    main()