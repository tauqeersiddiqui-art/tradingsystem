"""
Compare features computed by training pipeline (compute_all_features)
vs live pipeline (build_live_features) for the same candle window.
Reveals any remaining mismatches beyond momentum_velocity.
"""
import sys, os, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import joblib

from ml.feature_config import FEATURE_COLUMNS, build_live_features
from ml.dataset_builder_v2 import compute_all_features

# ── Load candles (200 context + today) ───────────────────────────────
df_all = pd.read_csv("data/historical/banknifty_1m_full.csv")
df_all["date"] = pd.to_datetime(df_all["date"], errors="coerce")
df_all = df_all.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
if "volume" not in df_all.columns:
    df_all["volume"] = 0.0

today = df_all["date"].max().normalize()
df_ctx   = df_all[df_all["date"] < today].tail(300)
df_today = df_all[df_all["date"].dt.normalize() == today]
df_combined = pd.concat([df_ctx, df_today], ignore_index=True).reset_index(drop=True)

# Training pipeline features for every row
df_train_feats = compute_all_features(df_combined.copy())

# Pick the LAST completed candle as our test point
df_session = df_train_feats[df_train_feats["date"].dt.normalize() == today]
df_session = df_session[df_session["date"].dt.time >= pd.to_datetime("09:15").time()]

# We'll compare 5 candles: ORB (09:30), mid-morning, and last 3 of session
test_indices = []
session_times = df_session["date"].dt.strftime("%H:%M").tolist()
for t in ["09:30", "10:00", "11:00"]:
    if t in session_times:
        test_indices.append(session_times.index(t))
test_indices += list(range(max(0, len(df_session)-3), len(df_session)))
test_indices = sorted(set(test_indices))

# ── Load model to score each candle ──────────────────────────────────
ce_model = joblib.load("ml/models/champion_ce_lgbm.pkl")

print("=" * 100)
print("  FEATURE PIPELINE COMPARISON:  training (compute_all_features) vs live (build_live_features)")
print("=" * 100)

# For each test candle, build features both ways and score
for idx in test_indices:
    row = df_session.iloc[idx]
    ts  = row["date"]

    # ── TRAINING features (ground truth) ─────────────────────────────
    train_feats = {f: row[f] for f in FEATURE_COLUMNS if f in row.index}

    # ── LIVE features: simulate what live engine does ─────────────────
    # Use the same rolling window the live engine sees (last 200 rows up to this candle)
    abs_idx = df_combined[df_combined["date"] == ts].index
    if len(abs_idx) == 0:
        continue
    abs_idx = abs_idx[0]
    window  = df_combined.iloc[max(0, abs_idx - 200):abs_idx + 1]

    closes  = window["close"].tolist()
    opens   = window["open"].tolist()
    highs   = window["high"].tolist()
    lows    = window["low"].tolist()
    volumes = window["volume"].tolist() if "volume" in window.columns else [0] * len(closes)

    # Simulate _compute_signal_dict from live_engine.py (post-fix)
    from ml.indicators import (supertrend as compute_supertrend, adx as compute_adx,
                               atr_wilder as _atr_wilder, rsi_wilder as _rsi_wilder)
    import numpy as np_

    h_arr = np_.array(highs, dtype=float)
    l_arr = np_.array(lows,  dtype=float)
    c_arr = np_.array(closes, dtype=float)

    st_dir_arr, st_line_arr = compute_supertrend(h_arr, l_arr, c_arr, 10, 3.0)
    adx_arr, di_plus, di_minus = compute_adx(h_arr, l_arr, c_arr, 14)

    n = len(closes)
    def ema_scalar(series, span):
        alpha = 2 / (span + 1)
        val = series[0]
        for p in series[1:]:
            val = p * alpha + val * (1 - alpha)
        return val

    # EMA: use full window (no cap) — matches training
    ema20 = ema_scalar(closes, 20)
    ema50 = ema_scalar(closes, 50) if n >= 50 else ema20

    # RSI: Wilder recursive smoothing — matches training
    rsi_arr = _rsi_wilder(c_arr, period=14)
    rsi_1m  = float(rsi_arr[-1]) if n >= 14 else 50.0

    last_st = float(st_dir_arr[-1])
    st_line = float(st_line_arr[-1])
    st_dist = (closes[-1] - st_line) / closes[-1] if closes[-1] != 0 else 0.0

    # VWAP: session-level accumulation (approximated from window that starts at 09:15)
    sess_mask = window["date"].dt.time >= pd.to_datetime("09:15").time()
    sess_w = window[sess_mask]
    if len(sess_w) > 0:
        tp = (sess_w["high"] + sess_w["low"] + sess_w["close"]) / 3
        vol = sess_w["volume"].replace(0, 1)
        vwap_val = (tp * vol).sum() / vol.sum()
    else:
        vwap_val = closes[-1]
    pvwap = (closes[-1] - vwap_val) / closes[-1] if closes[-1] != 0 else 0.0

    avg_vol = np_.mean(volumes[-20:]) if len(volumes) >= 20 else 1.0
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1.0

    # ATR: Wilder RMA over full window — matches training
    atr_arr_live = _atr_wilder(h_arr, l_arr, c_arr, period=14)
    atr_live     = float(atr_arr_live[-1]) if n >= 14 else 1.0

    signal = {
        "ema20": ema20, "ema50": ema50, "rsi_1m": rsi_1m,
        "atr": max(atr_live, 0.5),
        "trend_strength": (ema20 - ema50) / closes[-1] if closes[-1] != 0 else 0.0,
        "supertrend_dir": last_st, "supertrend_dist": float(np_.clip(st_dist, -0.05, 0.05)),
        "price_vs_vwap": float(np_.clip(pvwap, -0.05, 0.05)),
        "adx": float(np_.clip(adx_arr[-1], 0, 100)),
        "di_spread": float(np_.clip(di_plus[-1] - di_minus[-1], -60, 60)),
        "ema_alignment": 1.0 if ema20 > ema50 else -1.0,
        "volume_ratio": float(np_.clip(vol_ratio, 0, 10)),
    }

    live_feats = build_live_features(closes, opens, highs, lows, volumes, signal, ts=ts)

    # ── Score both feature sets ───────────────────────────────────────
    X_train = pd.DataFrame([{f: train_feats.get(f, 0.0) for f in FEATURE_COLUMNS}])
    X_live  = pd.DataFrame([{f: live_feats.get(f, 0.0)  for f in FEATURE_COLUMNS}])

    score_train = ce_model.base_model.predict_proba(X_train)[0, 1]
    score_live  = ce_model.base_model.predict_proba(X_live)[0, 1]

    print(f"\n{'─'*100}")
    print(f"  Candle: {ts.strftime('%H:%M')}  |  TRAINING raw={score_train:.4f}  |  LIVE raw={score_live:.4f}  |  delta={score_live-score_train:+.4f}")
    print(f"{'─'*100}")
    print(f"  {'Feature':<28}  {'Train':>10}  {'Live':>10}  {'Delta':>10}  {'AbsDelta':>10}  {'Flag'}")
    print(f"  {'─'*28}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*10}  {'─'*6}")

    diffs = []
    for f in FEATURE_COLUMNS:
        tv = float(train_feats.get(f, 0.0))
        lv = float(live_feats.get(f, 0.0))
        delta = lv - tv
        abs_delta = abs(delta)
        diffs.append((f, tv, lv, delta, abs_delta))

    diffs.sort(key=lambda x: x[4], reverse=True)
    for f, tv, lv, delta, abs_delta in diffs:
        flag = "*** MISMATCH" if abs_delta > 0.01 else ("  ~" if abs_delta > 0.001 else "")
        print(f"  {f:<28}  {tv:>10.5f}  {lv:>10.5f}  {delta:>+10.5f}  {abs_delta:>10.5f}  {flag}")

print("\nDone.")
