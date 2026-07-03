from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.neural_network import MLPClassifier, MLPRegressor


@dataclass(frozen=True)
class ModelSpec:
    name: str
    task: str
    available: bool
    builder: Callable[[], object] | None
    reason: str = ""


def classifier_candidates(random_state: int = 42) -> list[ModelSpec]:
    specs: list[ModelSpec] = []

    try:
        import lightgbm as lgb

        specs.append(
            ModelSpec(
                "lgbm",
                "classifier",
                True,
                lambda: lgb.LGBMClassifier(
                    n_estimators=500,
                    learning_rate=0.02,
                    num_leaves=31,
                    max_depth=6,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    min_child_samples=50,
                    reg_alpha=0.05,
                    reg_lambda=0.1,
                    random_state=random_state,
                    n_jobs=-1,
                    verbose=-1,
                ),
            )
        )
    except Exception as exc:
        specs.append(ModelSpec("lgbm", "classifier", False, None, str(exc)))

    try:
        from catboost import CatBoostClassifier

        specs.append(
            ModelSpec(
                "catboost",
                "classifier",
                True,
                lambda: CatBoostClassifier(
                    iterations=600,
                    learning_rate=0.03,
                    depth=6,
                    l2_leaf_reg=3.0,
                    random_seed=random_state,
                    verbose=0,
                    thread_count=-1,
                ),
            )
        )
    except Exception as exc:
        specs.append(ModelSpec("catboost", "classifier", False, None, str(exc)))

    try:
        from xgboost import XGBClassifier

        specs.append(
            ModelSpec(
                "xgboost",
                "classifier",
                True,
                lambda: XGBClassifier(
                    n_estimators=500,
                    max_depth=5,
                    learning_rate=0.03,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    objective="binary:logistic",
                    eval_metric="logloss",
                    random_state=random_state,
                    n_jobs=-1,
                ),
            )
        )
    except Exception as exc:
        specs.append(ModelSpec("xgboost", "classifier", False, None, str(exc)))

    specs.append(
        ModelSpec(
            "random_forest",
            "classifier",
            True,
            lambda: RandomForestClassifier(
                n_estimators=400,
                max_depth=12,
                min_samples_leaf=50,
                class_weight="balanced_subsample",
                random_state=random_state,
                n_jobs=-1,
            ),
        )
    )
    specs.append(
        ModelSpec(
            "mlp",
            "classifier",
            True,
            lambda: MLPClassifier(
                hidden_layer_sizes=(64, 32),
                alpha=1e-4,
                max_iter=300,
                random_state=random_state,
            ),
        )
    )
    return specs


def regressor_candidates(random_state: int = 42) -> list[ModelSpec]:
    specs: list[ModelSpec] = []

    try:
        import lightgbm as lgb

        specs.append(
            ModelSpec(
                "lgbm",
                "regressor",
                True,
                lambda: lgb.LGBMRegressor(
                    n_estimators=500,
                    learning_rate=0.02,
                    num_leaves=31,
                    max_depth=6,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    min_child_samples=50,
                    reg_alpha=0.05,
                    reg_lambda=0.1,
                    random_state=random_state,
                    n_jobs=-1,
                    verbose=-1,
                ),
            )
        )
    except Exception as exc:
        specs.append(ModelSpec("lgbm", "regressor", False, None, str(exc)))

    try:
        from catboost import CatBoostRegressor

        specs.append(
            ModelSpec(
                "catboost",
                "regressor",
                True,
                lambda: CatBoostRegressor(
                    iterations=600,
                    learning_rate=0.03,
                    depth=6,
                    l2_leaf_reg=3.0,
                    random_seed=random_state,
                    verbose=0,
                    thread_count=-1,
                ),
            )
        )
    except Exception as exc:
        specs.append(ModelSpec("catboost", "regressor", False, None, str(exc)))

    specs.append(
        ModelSpec(
            "random_forest",
            "regressor",
            True,
            lambda: RandomForestRegressor(
                n_estimators=400,
                max_depth=12,
                min_samples_leaf=50,
                random_state=random_state,
                n_jobs=-1,
            ),
        )
    )
    specs.append(
        ModelSpec(
            "mlp",
            "regressor",
            True,
            lambda: MLPRegressor(
                hidden_layer_sizes=(64, 32),
                alpha=1e-4,
                max_iter=300,
                random_state=random_state,
            ),
        )
    )
    return specs

