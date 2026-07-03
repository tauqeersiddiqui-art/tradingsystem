from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
EXPERIMENT_ROOT = REPO_ROOT / "experimental" / "ml_pipeline_v2"


try:
    from ml.feature_config import FEATURE_COLUMNS as PRODUCTION_FEATURE_COLUMNS
except Exception:
    PRODUCTION_FEATURE_COLUMNS = [
        "supertrend_dir",
        "supertrend_dist",
        "price_vs_vwap",
        "adx",
        "di_spread",
        "ema_alignment",
        "volume_ratio",
        "ema20",
        "ema50",
        "macd",
        "returns",
        "volatility",
        "rsi",
        "atr",
        "trend_strength",
        "hour",
        "weekday",
        "return_1",
        "return_3",
        "candle_body_pct",
        "range_break_strength",
        "mins_since_open",
        "mins_to_close",
        "session_open",
        "session_close",
        "time_to_expiry_min",
        "moneyness",
        "momentum_velocity",
        "range_compression",
        "wick_ratio",
        "body_efficiency",
        "mom3_strength",
        "upper_wick",
        "lower_wick",
        "close_position",
    ]


FEATURE_COLUMNS = list(PRODUCTION_FEATURE_COLUMNS)


@dataclass(frozen=True)
class PipelinePaths:
    historical_1m: Path = REPO_ROOT / "data" / "historical" / "banknifty_1m_full.csv"
    dataset_v2: Path = REPO_ROOT / "ml" / "models" / "training_dataset_v2.csv"
    dataset_v3: Path = REPO_ROOT / "ml" / "models" / "training_dataset_v3.csv"
    output_dir: Path = EXPERIMENT_ROOT / "artifacts"


@dataclass(frozen=True)
class LabelConfig:
    directional_lookahead_bars: int = 12
    directional_target_points: float = 15.0
    directional_max_adverse_multiple: float = 2.0
    quality_lookahead_bars: int = 12
    spread_points_round_trip: float = 1.0
    lot_units: int = 30
    brokerage_round_trip_rs: float = 132.0


@dataclass(frozen=True)
class ValidationConfig:
    min_train_rows: int = 50_000
    calibration_fraction: float = 0.20
    test_fraction: float = 0.20
    walkforward_folds: int = 6
    purge_bars: int = 12
    embargo_bars: int = 12
    random_seed: int = 42


@dataclass(frozen=True)
class RiskConfig:
    max_risk_per_trade_pct: float = 0.50
    max_daily_loss_pct: float = 2.00
    max_open_exposure_pct: float = 5.00
    min_expected_net_pnl_rs: float = 150.0
    max_risk_of_ruin_pct: float = 1.00


@dataclass(frozen=True)
class PipelineConfig:
    paths: PipelinePaths = PipelinePaths()
    labels: LabelConfig = LabelConfig()
    validation: ValidationConfig = ValidationConfig()
    risk: RiskConfig = RiskConfig()


def ensure_output_dirs(config: PipelineConfig) -> None:
    for subdir in ("reports", "models", "predictions", "drift"):
        (config.paths.output_dir / subdir).mkdir(parents=True, exist_ok=True)

