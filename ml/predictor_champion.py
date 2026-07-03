# ml/predictor_champion.py

import os
import warnings
import logging
import joblib
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

warnings.filterwarnings("ignore")
logger = logging.getLogger("predictor_champion")
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
        # FIX 2026-07-03: Platt calibrator was fitted on OLD saturated model
        # outputs (0.80-0.96 range). After trainer_v3 de-saturation, base model
        # outputs are 0.40-0.75 — the stale Platt logit mapping squashes these
        # to near-zero (0.001-0.003), killing all signals. Bypass Platt and
        # return base model probabilities directly. Thresholds in live_engine
        # (0.72 CE / 0.64 PE) are calibrated for these raw outputs.
        raw = self.base_model.predict_proba(X)[:, 1]
        raw = np.clip(raw, 1e-6, 1 - 1e-6)
        # Return in sklearn-compatible (N, 2) format: [prob_class0, prob_class1]
        return np.column_stack([1 - raw, raw])

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


# ── Predictor ─────────────────────────────────────────────────────────

class ChampionPredictor:

    def __init__(self):

        # ── LightGBM (required) ───────────────────────────────────────
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

        # ── CatBoost (optional — enables ensemble if both files exist) ─
        ce_cat_path = "ml/models/champion_ce_cat.pkl"
        pe_cat_path = "ml/models/champion_pe_cat.pkl"
        cat_both = os.path.exists(ce_cat_path) and os.path.exists(pe_cat_path)

        if cat_both:
            self.ce_cat_model = joblib.load(ce_cat_path)
            self.pe_cat_model = joblib.load(pe_cat_path)
            self._ensemble = True
            logger.info(
                f"[ChampionPredictor] Mode: LGBM+CAT_ENSEMBLE | "
                f"CE={len(self.ce_features)} feats thresh={self.ce_threshold} | "
                f"PE={len(self.pe_features)} feats thresh={self.pe_threshold}"
            )
        else:
            self.ce_cat_model = None
            self.pe_cat_model = None
            self._ensemble = False
            logger.info(
                f"[ChampionPredictor] Mode: LGBM_ONLY | "
                f"CE={len(self.ce_features)} feats thresh={self.ce_threshold} | "
                f"PE={len(self.pe_features)} feats thresh={self.pe_threshold}"
            )


    # ───────────────────────────────────────── #

    def _load_threshold(self, name, model_path, default):
        t_path = os.path.join(os.path.dirname(model_path), f"{name}_threshold.txt")
        try:
            val = float(open(t_path).read().strip())
            logger.info(f"[Predictor] {name} threshold={val}")
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

        logger.warning(f"[PREDICTOR WARNING] {label}: fallback to FEATURE_COLUMNS")
        return list(FEATURE_COLUMNS)

    # ───────────────────────────────────────── #

    def predict(self, features_dict: dict, direction: str) -> float:

        model    = self.ce_model    if direction == "CE" else self.pe_model
        req_cols = self.ce_features if direction == "CE" else self.pe_features

        # ================= FEATURE VALIDATION ================= #

        missing = [f for f in req_cols if f not in features_dict]

        if missing:
            logger.warning(f"[PREDICTOR WARNING] Missing features ({len(missing)}): {missing[:5]}...")
            return None  # DO NOT RETURN 0

        # ================= BUILD INPUT ================= #

        try:
            row = []

            for f in req_cols:

                val = float(features_dict[f])

                if np.isnan(val) or np.isinf(val):
                    logger.warning(f"[PREDICTOR WARNING] Invalid feature {f}={val}")
                    return None

                row.append(val)
            X = pd.DataFrame([row], columns=req_cols)

            lgbm_prob = float(model.predict_proba(X)[0][1])
            lgbm_prob = max(0.0, min(1.0, lgbm_prob))

            # ── Ensemble: average LGBM + CatBoost when both loaded ────
            # FIX 2026-07-03: cat_model is also a CalibratedLGBM wrapper.
            # Its predict_proba() now returns raw base_model outputs (Platt
            # bypass applied above). But cat_model.base_model is a CatBoost
            # which may output near-zero on current features — bypass its
            # Platt layer too by calling base_model.predict_proba() directly.
            if self._ensemble:
                cat_model = self.ce_cat_model if direction == "CE" else self.pe_cat_model
                try:
                    # Try raw base_model first (bypasses stale Platt on CatBoost)
                    _cat_base = getattr(cat_model, "base_model", None)
                    if _cat_base is not None:
                        cat_prob = float(_cat_base.predict_proba(X)[0][1])
                    else:
                        cat_prob = float(cat_model.predict_proba(X)[0][1])
                    cat_prob = max(0.0, min(1.0, cat_prob))
                    prob = (lgbm_prob + cat_prob) / 2.0
                except Exception as e:
                    logger.warning(f"[PREDICTOR] CatBoost predict failed ({e}), using LGBM only")
                    prob = lgbm_prob
            else:
                prob = lgbm_prob

            return round(prob, 6)

        except Exception as e:
            logger.error(f"[PREDICTOR ERROR] {direction}: {e}")
            return None

    def passes_threshold(self, prob: float, direction: str) -> bool:

        threshold = (
            self.ce_threshold
            if direction == "CE"
            else self.pe_threshold
        )

        return prob >= threshold
