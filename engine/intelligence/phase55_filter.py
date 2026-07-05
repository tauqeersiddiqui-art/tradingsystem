from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


CE_QUALITY_THRESHOLD = 0.4358
PE_DIRECTIONAL_THRESHOLD = 0.4645


@dataclass(frozen=True)
class Phase55FilterConfig:
    enabled: bool = False
    ce_threshold_enabled: bool = True
    pe_threshold_enabled: bool = True
    regime_filter_enabled: bool = True
    ce_quality_threshold: float = CE_QUALITY_THRESHOLD
    pe_directional_threshold: float = PE_DIRECTIONAL_THRESHOLD

    @classmethod
    def from_config(cls, config: object | None) -> "Phase55FilterConfig":
        if config is None:
            return cls()
        return cls(
            enabled=bool(getattr(config, "ENABLE_PHASE55_FILTERS", False)),
            ce_threshold_enabled=bool(getattr(config, "ENABLE_PHASE55_CE_THRESHOLD", True)),
            pe_threshold_enabled=bool(getattr(config, "ENABLE_PHASE55_PE_THRESHOLD", True)),
            regime_filter_enabled=bool(getattr(config, "ENABLE_PHASE55_REGIME_FILTER", True)),
            ce_quality_threshold=float(getattr(config, "PHASE55_CE_QUALITY_THRESHOLD", CE_QUALITY_THRESHOLD)),
            pe_directional_threshold=float(
                getattr(config, "PHASE55_PE_DIRECTIONAL_THRESHOLD", PE_DIRECTIONAL_THRESHOLD)
            ),
        )


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _lookup_score(mapping: Mapping[str, Any], keys: tuple[str, ...], default: float = 0.0) -> float:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return _to_float(mapping[key], default)
    return default


def infer_regime_from_features(features: Mapping[str, Any]) -> str:
    adx = _to_float(features.get("adx"), 0.0)
    di_spread = abs(_to_float(features.get("di_spread"), 0.0))
    volatility = _to_float(features.get("volatility"), 0.0)

    if adx >= 35.0 or volatility >= 0.000545304417118:
        return "volatile_trend"
    if adx >= 25.0 and di_spread >= 10.0:
        return "trend"
    if adx < 18.0:
        return "range"
    return "mixed"


def normalize_regime(regime: Any, features: Mapping[str, Any]) -> str:
    text = str(regime or "").strip().lower()
    if text in {"", "unknown", "none", "nan"}:
        return infer_regime_from_features(features)
    text = text.replace("_day", "").replace(" day", "").replace("-", "_").replace(" ", "_")
    if text in {"mixed", "trend", "range", "volatile_trend"}:
        return text
    if text == "volatile":
        return "volatile_trend"
    if text in {"expansion", "gap"}:
        return "volatile_trend"
    return text


def _blocked_response(
    confidence: float,
    reason: str,
    recommendation: str,
    applied_filters: list[str],
) -> dict[str, Any]:
    return {
        "allow_trade": False,
        "confidence_adjustment": -abs(confidence),
        "blocking_reason": reason,
        "recommendation": recommendation,
        "applied_filters": applied_filters,
    }


def evaluate_phase55_filter(
    *,
    market_features: Mapping[str, Any],
    ml_predictions: Mapping[str, Any],
    current_regime: Any,
    confidence_scores: Mapping[str, Any],
    direction: str,
    config: Phase55FilterConfig | None = None,
    symbol: str | None = None,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Apply validated Phase 5.5 decision filters.

    The return schema is intentionally plain dict data so callers can log,
    persist, or send it to dashboards without importing dataclasses.
    """
    cfg = config or Phase55FilterConfig()
    side = str(direction or "").upper()
    features = market_features or {}
    predictions = ml_predictions or {}
    scores = confidence_scores or {}
    regime = normalize_regime(current_regime, features)
    side_confidence = _lookup_score(
        scores,
        ("side_confidence", "confidence", f"{side.lower()}_confidence"),
        _lookup_score(predictions, (side, side.lower(), f"{side.lower()}_prob"), 0.0),
    )

    applied_filters: list[str] = []
    if not cfg.enabled:
        return {
            "allow_trade": True,
            "confidence_adjustment": 0.0,
            "blocking_reason": "",
            "recommendation": "phase55_disabled",
            "applied_filters": applied_filters,
        }

    if side == "CE":
        if cfg.ce_threshold_enabled:
            quality_confidence = _lookup_score(
                scores,
                (
                    "quality_confidence",
                    "ce_quality_confidence",
                    "quality_confidence_ce",
                    "side_confidence",
                    "ce_confidence",
                ),
                side_confidence,
            )
            applied_filters.append("PHASE55_CE_QUALITY_THRESHOLD")
            if quality_confidence < cfg.ce_quality_threshold:
                return _blocked_response(
                    confidence=quality_confidence,
                    reason=(
                        f"PHASE55_CE_QUALITY_THRESHOLD "
                        f"({quality_confidence:.4f} < {cfg.ce_quality_threshold:.4f})"
                    ),
                    recommendation="Block CE until quality confidence clears Phase 5.5 threshold.",
                    applied_filters=applied_filters,
                )

        if cfg.regime_filter_enabled:
            applied_filters.append("PHASE55_CE_MIXED_REGIME_FILTER")
            if regime == "mixed":
                return _blocked_response(
                    confidence=side_confidence,
                    reason="PHASE55_CE_MIXED_REGIME",
                    recommendation="Reduce or block CE trades during mixed regime.",
                    applied_filters=applied_filters,
                )

    elif side == "PE" and cfg.pe_threshold_enabled:
        directional_confidence = _lookup_score(
            scores,
            (
                "directional_confidence",
                "pe_directional_confidence",
                "directional_confidence_pe",
                "side_confidence",
                "pe_confidence",
            ),
            side_confidence,
        )
        applied_filters.append("PHASE55_PE_DIRECTIONAL_THRESHOLD")
        if directional_confidence < cfg.pe_directional_threshold:
            return _blocked_response(
                confidence=directional_confidence,
                reason=(
                    f"PHASE55_PE_DIRECTIONAL_THRESHOLD "
                    f"({directional_confidence:.4f} < {cfg.pe_directional_threshold:.4f})"
                ),
                recommendation="Block PE until directional confidence clears Phase 5.5 threshold.",
                applied_filters=applied_filters,
            )

    return {
        "allow_trade": True,
        "confidence_adjustment": 0.0,
        "blocking_reason": "",
        "recommendation": "allow_phase55",
        "applied_filters": applied_filters,
    }
