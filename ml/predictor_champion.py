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

        if hasattr(self.base_model, "feature_name_"):
            fn = self.base_model.feature_name_
            self.feature_names_ = list(fn() if callable(fn) else fn)
        elif hasattr(self.base_model, "feature_names_in_"):
            self.feature_names_ = list(self.base_model.feature_names_in_)

        return self

    def predict_proba(self, X):
        raw = self.base_model.predict_proba(X)[:, 1]
        raw = np.clip(raw, 1e-6, 1 - 1e-6)
        logit = np.log(raw / (1 - raw)).reshape(-1, 1)
        return self.calibrator.predict_proba(logit)

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


class ChampionPredictor:

    def __init__(self):
        ce_path = "ml/models/champion_ce_lgbm.pkl"
        pe_path = "ml/models/champion_pe_lgbm.pkl"

        for path in (ce_path, pe_path):
            if not os.path.exists(path):
                raise FileNotFoundError(f"Model not found: {path}")

        self.ce_model = joblib.load(ce_path)
        self.pe_model = joblib.load(pe_path)

        self.ce_threshold = self._load_threshold("champion_ce_lgbm", ce_path, 0.35)
        self.pe_threshold = self._load_threshold("champion_pe_lgbm", pe_path, 0.35)
        self._midday_fallback_enabled = os.getenv("PREDICTOR_MIDDAY_TIME_FALLBACK", "1") == "1"
        self._midday_collapse_max_prob = float(
            os.getenv("PREDICTOR_TIME_COLLAPSE_MAX_PROB", "0.01")
        )
        self._midday_recovery_min_prob = float(
            os.getenv("PREDICTOR_TIME_RECOVERY_MIN_PROB", "0.05")
        )
        self._last_midday_fallback_key = ""

        self.ce_features = self._model_features(self.ce_model, "CE")
        self.pe_features = self._model_features(self.pe_model, "PE")

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

    @staticmethod
    def _threshold_cap_for(name: str):
        upper = str(name or "").upper()
        if "CHAMPION_CE" in upper:
            return float(os.getenv("PREDICTOR_CE_THRESHOLD_CAP", "0.72"))
        if "CHAMPION_PE" in upper:
            return float(os.getenv("PREDICTOR_PE_THRESHOLD_CAP", "0.64"))
        return None

    def _load_threshold(self, name, model_path, default):
        t_path = os.path.join(os.path.dirname(model_path), f"{name}_threshold.txt")
        try:
            with open(t_path, "r", encoding="utf-8") as handle:
                val = float(handle.read().strip())
            cap = self._threshold_cap_for(name)
            if cap is not None and val > cap:
                logger.warning(
                    f"[Predictor] {name} threshold={val} exceeds cap={cap}; "
                    "using capped value for live scoring"
                )
                val = cap
            logger.info(f"[Predictor] {name} threshold={val}")
            return val
        except Exception:
            return default

    @staticmethod
    def _model_features(model, label):
        if hasattr(model, "feature_names_") and isinstance(model.feature_names_, list):
            return model.feature_names_

        if hasattr(model, "estimator"):
            inner = model.estimator

            if hasattr(inner, "feature_name_"):
                fn = inner.feature_name_
                return list(fn() if callable(fn) else fn)

            if hasattr(inner, "feature_names_in_"):
                return list(inner.feature_names_in_)

        if hasattr(model, "feature_name_"):
            fn = model.feature_name_
            return list(fn() if callable(fn) else fn)

        if hasattr(model, "feature_names_in_"):
            return list(model.feature_names_in_)

        if hasattr(model, "calibrated_classifiers_"):
            for clf in model.calibrated_classifiers_:
                base = getattr(clf, "estimator", getattr(clf, "base_estimator", None))
                if base and hasattr(base, "feature_name_"):
                    fn = base.feature_name_
                    return list(fn() if callable(fn) else fn)

        logger.warning(f"[PREDICTOR WARNING] {label}: fallback to FEATURE_COLUMNS")
        return list(FEATURE_COLUMNS)

    @staticmethod
    def _is_midday_time_regime(features_dict: dict) -> bool:
        try:
            hour = int(float(features_dict.get("hour", -1)))
            mins_since_open = float(features_dict.get("mins_since_open", -1.0))
        except Exception:
            return False
        return hour in (11, 12, 13) and mins_since_open >= 105.0

    @staticmethod
    def _with_midday_time_fallback(features_dict: dict) -> dict:
        adjusted = dict(features_dict)
        adjusted.update(
            {
                "hour": 10.0,
                "mins_since_open": 104.0,
                "mins_to_close": 271.0,
                "time_to_expiry_min": 271.0,
                "session_open": 0.0,
                "session_close": 0.0,
            }
        )
        return adjusted

    @staticmethod
    def _build_input_frame(features_dict: dict, req_cols):
        missing = [feature_name for feature_name in req_cols if feature_name not in features_dict]
        if missing:
            logger.warning(f"[PREDICTOR WARNING] Missing features ({len(missing)}): {missing[:5]}...")
            return None

        row = []
        for feature_name in req_cols:
            val = float(features_dict[feature_name])
            if np.isnan(val) or np.isinf(val):
                logger.warning(f"[PREDICTOR WARNING] Invalid feature {feature_name}={val}")
                return None
            row.append(val)

        return pd.DataFrame([row], columns=req_cols)

    def _predict_ensemble_prob(self, X, direction: str) -> float:
        model = self.ce_model if direction == "CE" else self.pe_model
        lgbm_prob = float(model.predict_proba(X)[0][1])
        lgbm_prob = max(0.0, min(1.0, lgbm_prob))

        if not self._ensemble:
            return lgbm_prob

        cat_model = self.ce_cat_model if direction == "CE" else self.pe_cat_model
        try:
            cat_prob = float(cat_model.predict_proba(X)[0][1])
            cat_prob = max(0.0, min(1.0, cat_prob))
            return (lgbm_prob + cat_prob) / 2.0
        except Exception as exc:
            logger.warning(f"[PREDICTOR] CatBoost predict failed ({exc}), using LGBM only")
            return lgbm_prob

    def predict(self, features_dict: dict, direction: str) -> float:
        req_cols = self.ce_features if direction == "CE" else self.pe_features

        try:
            X = self._build_input_frame(features_dict, req_cols)
            if X is None:
                return None

            prob = self._predict_ensemble_prob(X, direction)

            if (
                self._midday_fallback_enabled
                and self._is_midday_time_regime(features_dict)
                and prob <= self._midday_collapse_max_prob
            ):
                fallback_features = self._with_midday_time_fallback(features_dict)
                fallback_X = self._build_input_frame(fallback_features, req_cols)
                if fallback_X is not None:
                    fallback_prob = self._predict_ensemble_prob(fallback_X, direction)
                    recovered_enough = (
                        fallback_prob >= self._midday_recovery_min_prob
                        and fallback_prob > max(prob * 5.0, prob + 0.05)
                    )
                    if recovered_enough:
                        fallback_key = (
                            f"{direction}:"
                            f"{int(float(features_dict.get('hour', -1)))}:"
                            f"{int(float(features_dict.get('mins_since_open', -1)))}"
                        )
                        if fallback_key != self._last_midday_fallback_key:
                            logger.warning(
                                f"[PREDICTOR] Midday time fallback rescued {direction}: "
                                f"raw={prob:.6f} fallback={fallback_prob:.6f}"
                            )
                            self._last_midday_fallback_key = fallback_key
                        prob = fallback_prob

            return round(prob, 6)

        except Exception as exc:
            logger.error(f"[PREDICTOR ERROR] {direction}: {exc}")
            return None

    def passes_threshold(self, prob: float, direction: str) -> bool:
        threshold = self.ce_threshold if direction == "CE" else self.pe_threshold
        return prob >= threshold
