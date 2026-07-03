"""Quick ML diagnostic — run once to identify why probabilities are near zero."""
import joblib, numpy as np, pandas as pd, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ml.feature_config import FEATURE_COLUMNS

def test_model(path, label):
    print(f"\n{'='*50}\n{label}: {path}")
    m = joblib.load(path)
    print(f"  Outer type: {type(m).__name__}")

    # Build a realistic feature row
    row = {f: 0.0 for f in FEATURE_COLUMNS}
    row.update({
        'supertrend_dir': 1.0, 'adx': 28.0, 'rsi': 58.0,
        'ema20': 58050.0, 'ema50': 57950.0, 'trend_strength': 0.0017,
        'hour': 13, 'weekday': 3, 'mins_since_open': 240.0,
        'mins_to_close': 135.0, 'time_to_expiry_min': 135.0,
        'volume_ratio': 1.2, 'ema_alignment': 1.0,
        'close_position': 0.65, 'body_efficiency': 0.6,
        'returns': 0.0012, 'return_1': 0.0012, 'return_3': 0.0018,
        'volatility': 0.003, 'atr': 45.0, 'candle_body_pct': 0.5,
        'price_vs_vwap': 0.0005, 'supertrend_dist': 0.003,
        'di_spread': 8.0, 'range_break_strength': 0.3,
    })
    X = pd.DataFrame([row], columns=FEATURE_COLUMNS)

    # Via wrapper predict_proba
    p_wrap = float(m.predict_proba(X)[0][1])
    print(f"  via wrapper predict_proba: {p_wrap:.6f}")

    # Via base_model directly (skip Platt)
    base = getattr(m, 'base_model', None)
    if base is not None:
        print(f"  base_model type: {type(base).__name__}")
        p_base = float(base.predict_proba(X)[0][1])
        print(f"  via base_model raw: {p_base:.6f}")
    else:
        print("  NO base_model attribute")

    # Calibrator coefficients
    cal = getattr(m, 'calibrator', None)
    if cal is not None:
        print(f"  calibrator type: {type(cal).__name__}")
        coef = getattr(cal, 'coef_', None)
        intercept = getattr(cal, 'intercept_', None)
        print(f"  calibrator coef={coef}  intercept={intercept}")
        # Manually apply Platt to base prob to see squash
        if base is not None and coef is not None:
            raw = p_base
            raw = np.clip(raw, 1e-6, 1-1e-6)
            logit = np.log(raw/(1-raw))
            platt_input = logit * coef[0][0] + intercept[0]
            platt_prob = 1 / (1 + np.exp(-platt_input))
            print(f"  manual Platt(raw={raw:.3f}) => {platt_prob:.6f}  [shows squash severity]")

for path, label in [
    ("ml/models/champion_ce_lgbm.pkl", "CE LGBM"),
    ("ml/models/champion_pe_lgbm.pkl", "PE LGBM"),
    ("ml/models/champion_ce_cat.pkl",  "CE CatBoost"),
    ("ml/models/champion_pe_cat.pkl",  "PE CatBoost"),
]:
    if os.path.exists(path):
        try:
            test_model(path, label)
        except Exception as e:
            print(f"  ERROR: {e}")
    else:
        print(f"\n{label}: FILE NOT FOUND — {path}")

print("\nDone.")
