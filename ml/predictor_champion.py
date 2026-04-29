# ml/predictor_champion.py

import os
import warnings
import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

warnings.filterwarnings("ignore")

from ml.feature_config import FEATURE_COLUMNS


# ── Calibration wrapper ───────────────────────────────────────────────

class CalibratedLGBM:
    """
    Manual Platt scaling wrapper.
    """

    def __init__(self, base_model):
        self.base_model = base_model
        self.calibrator = LogisticRegression(C=1.0, max_iter=1000)
        self.feature_names_ = None

    def fit_calibration(self, X_holdout, y_holdout):
        raw = self.base_model.predict_proba(X_holdout)[:, 1]
        raw = np.clip(raw, 1e-6, 1 - 1e-6)

        logit = np.log(raw / (1 - raw)).reshape(-1, 1)
        self.calibrator.fit(logit, y_holdout)

        if hasattr(self.base_model, 'feature_name_'):
            fn = self.base_model.feature_name_
            self.feature_names_ = list(fn() if callable(fn) else fn)

        elif hasattr(self.base_model, 'feature_names_in_'):
            self.feature_names_ = list(self.base_model.feature_names_in_)

        return self

    def predict_proba(self, X):
        raw = self.base_model.predict_proba(X)[:, 1]
        raw = np.clip(raw, 1e-6, 1 - 1e-6)

        logit = np.log(raw / (1 - raw)).reshape(-1, 1)
        return self.calibrator.predict_proba(logit)

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


# ── Predictor ─────────────────────────────────────────────────────────

class ChampionPredictor:

    def __init__(self):

        ce_path = "ml/models/champion_ce_lgbm.pkl"
        pe_path = "ml/models/champion_pe_lgbm.pkl"

        for p in (ce_path, pe_path):
            if not os.path.exists(p):
                raise FileNotFoundError(f"Model not found: {p}")

        self.ce_model = joblib.load(ce_path)
        self.pe_model = joblib.load(pe_path)

        self.ce_threshold = self._load_threshold("champion_ce_lgbm", ce_path, 0.35)
        self.pe_threshold = self._load_threshold("champion_pe_lgbm", pe_path, 0.35)

        self.ce_features = self._model_features(self.ce_model, "CE")
        self.pe_features = self._model_features(self.pe_model, "PE")

        print(f"[ChampionPredictor] Loaded: LGBM | "
              f"CE={len(self.ce_features)} feats thresh={self.ce_threshold} | "
              f"PE={len(self.pe_features)} feats thresh={self.pe_threshold}")

        # 🔥 DEBUG (VERY IMPORTANT)
        print(f"[FEATURES CE] {self.ce_features}")
        print(f"[FEATURES PE] {self.pe_features}")

    # ───────────────────────────────────────── #

    def _load_threshold(self, name, model_path, default):
        t_path = os.path.join(os.path.dirname(model_path), f"{name}_threshold.txt")
        try:
            val = float(open(t_path).read().strip())
            print(f"[Predictor] {name} threshold={val}")
            return val
        except Exception:
            return default

    # ───────────────────────────────────────── #

    @staticmethod
    def _model_features(model, label):

        if hasattr(model, 'feature_names_') and isinstance(model.feature_names_, list):
            return model.feature_names_

        if hasattr(model, 'estimator'):
            inner = model.estimator

            if hasattr(inner, 'feature_name_'):
                fn = inner.feature_name_
                return list(fn() if callable(fn) else fn)

            if hasattr(inner, 'feature_names_in_'):
                return list(inner.feature_names_in_)

        if hasattr(model, 'feature_name_'):
            fn = model.feature_name_
            return list(fn() if callable(fn) else fn)

        if hasattr(model, 'feature_names_in_'):
            return list(model.feature_names_in_)

        if hasattr(model, 'calibrated_classifiers_'):
            for clf in model.calibrated_classifiers_:
                base = getattr(clf, 'estimator', getattr(clf, 'base_estimator', None))
                if base and hasattr(base, 'feature_name_'):
                    fn = base.feature_name_
                    return list(fn() if callable(fn) else fn)

        print(f"[PREDICTOR WARNING] {label}: fallback to FEATURE_COLUMNS")
        return list(FEATURE_COLUMNS)

    # ───────────────────────────────────────── #

    def predict(self, features_dict: dict, direction: str) -> float:

        model = self.ce_model if direction == "CE" else self.pe_model
        req_cols = self.ce_features if direction == "CE" else self.pe_features

        # ================= FEATURE VALIDATION ================= #

        missing = [f for f in req_cols if f not in features_dict]

        if missing:
            print(f"[PREDICTOR WARNING] Missing features ({len(missing)}): {missing[:5]}...")
            return None  # 🚨 DO NOT RETURN 0

        # ================= BUILD INPUT ================= #

        try:
            row = [float(features_dict[f]) for f in req_cols]
            X = pd.DataFrame([row], columns=req_cols)

            prob = float(model.predict_proba(X)[0][1])

            # sanity clamp
            prob = max(0.0, min(1.0, prob))

            return prob

        except Exception as e:
            print(f"[PREDICTOR ERROR] {direction}: {e}")
            return None