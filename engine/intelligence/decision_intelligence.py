# engine/intelligence/decision_intelligence.py
"""
DECISION INTELLIGENCE — Weighted Scoring Layer

Adds weighted scoring to the decision process:
  FINAL_SCORE = (ML_CONFIDENCE * 0.5) + (ORB_SIGNAL * 0.2) +
                (GLOBAL_STATE_SCORE * 0.2) + (VOLATILITY_FILTER * 0.1)

Features:
- Dynamic threshold based on volatility
- Global context integration
- Risk-aware filtering
- All signals optional and non-blocking

CONSTRAINTS:
- NEVER blocks execution engine
- Failsafe: missing data → neutral defaults
- No latency impact on trade execution
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("decision_intelligence")

# Default weights (configurable via env)
DEFAULT_WEIGHTS = {
    "ml_confidence": 0.5,
    "orb_signal": 0.2,
    "global_state": 0.2,
    "volatility_filter": 0.1,
}


@dataclass
class DecisionScore:
    """Breakdown of decision score components."""
    final_score: float
    ml_confidence: float
    ml_weight: float
    ml_contribution: float

    orb_signal: float
    orb_weight: float
    orb_contribution: float

    global_state: float
    global_weight: float
    global_contribution: float

    volatility_factor: float
    volatility_weight: float
    volatility_contribution: float

    threshold: float
    decision: str  # "ALLOW" or "SKIP"
    skip_reason: str = ""


class DecisionIntelligence:
    """
    Weighted scoring for trade decisions.

    Integrates:
    - ML confidence score
    - ORB signal strength
    - Global market context
    - Volatility regime

    Decision is ALLOW or SKIP based on threshold comparison.
    """

    def __init__(self, config=None):
        # Load weights from config or env
        self._weights = DEFAULT_WEIGHTS.copy()

        if config:
            self._weights["ml_confidence"] = getattr(
                config, "DECISION_ML_WEIGHT", self._weights["ml_confidence"])
            self._weights["orb_signal"] = getattr(
                config, "DECISION_ORB_WEIGHT", self._weights["orb_signal"])
            self._weights["global_state"] = getattr(
                config, "DECISION_GLOBAL_WEIGHT", self._weights["global_state"])
            self._weights["volatility_filter"] = getattr(
                config, "DECISION_VOLATILITY_WEIGHT", self._weights["volatility_filter"])

        # Override with env vars if present
        self._weights["ml_confidence"] = float(os.getenv(
            "DECISION_ML_WEIGHT", self._weights["ml_confidence"]))
        self._weights["orb_signal"] = float(os.getenv(
            "DECISION_ORB_WEIGHT", self._weights["orb_signal"]))
        self._weights["global_state"] = float(os.getenv(
            "DECISION_GLOBAL_WEIGHT", self._weights["global_state"]))
        self._weights["volatility_filter"] = float(os.getenv(
            "DECISION_VOLATILITY_WEIGHT", self._weights["volatility_filter"]))

        # Base threshold (adjustable)
        self._base_threshold = float(os.getenv("DECISION_THRESHOLD", "0.35"))

        # Volatility adjustment
        self._vol_high_threshold = float(os.getenv("DECISION_THRESHOLD_HIGH_VOL", "0.45"))
        self._vol_low_threshold = float(os.getenv("DECISION_THRESHOLD_LOW_VOL", "0.25"))

        # Enable/disable components
        self._global_enabled = os.getenv("ENABLE_GLOBAL_CONTEXT", "1") == "1"
        self._orb_enabled = os.getenv("ENABLE_ORB_SCORING", "1") == "1"

        logger.info(
            f"[DecisionIntelligence] Weights: ML={self._weights['ml_confidence']:.2f} "
            f"ORB={self._weights['orb_signal']:.2f} "
            f"Global={self._weights['global_state']:.2f} "
            f"Vol={self._weights['volatility_filter']:.2f}"
        )

    def evaluate(
        self,
        ml_probability: float,
        side: str,
        global_market_state=None,
        volatility_state: str = "NORMAL",
        orb_breakout: bool = False,
        orb_direction: int = 0,  # +1 for CE, -1 for PE
    ) -> DecisionScore:
        """
        Evaluate a trade candidate using weighted scoring.

        Args:
            ml_probability: ML model probability (0-1)
            side: Trade side ("CE" or "PE")
            global_market_state: GlobalMarketState from global_market_engine
            volatility_state: "LOW", "NORMAL", or "HIGH"
            orb_breakout: Whether ORB breakout occurred
            orb_direction: +1 for bullish, -1 for bearish

        Returns:
            DecisionScore with breakdown and final decision
        """

        # 1. ML Confidence (0-1 normalized)
        ml_conf = max(0.0, min(1.0, ml_probability))
        ml_weight = self._weights["ml_confidence"]
        ml_contribution = ml_conf * ml_weight

        # 2. ORB Signal (0-1)
        # +1 if breakout in correct direction, 0.5 if ORB formed but no breakout
        orb_sig = 0.0
        if self._orb_enabled and orb_breakout:
            # Check if ORB direction matches trade side
            expected_dir = 1 if side == "CE" else -1
            if orb_direction == expected_dir:
                orb_sig = 1.0
            else:
                orb_sig = 0.0  # Wrong direction
        elif self._orb_enabled:
            orb_sig = 0.3  # ORB active but no breakout
        else:
            orb_sig = 0.5  # Neutral if disabled

        orb_weight = self._weights["orb_signal"]
        orb_contribution = orb_sig * orb_weight

        # 3. Global State Score (-1 to +1)
        global_score = 0.0
        if self._global_enabled and global_market_state:
            global_score = global_market_state.get_risk_score()
        # If disabled or no data, stay neutral (0)

        global_weight = self._weights["global_state"]
        global_contribution = global_score * global_weight

        # 4. Volatility Factor (0.8-1.2)
        vol_factor = 1.0
        if volatility_state == "HIGH":
            vol_factor = 0.8  # Reduce score
        elif volatility_state == "LOW":
            vol_factor = 1.1  # Slight boost

        vol_weight = self._weights["volatility_filter"]
        vol_contribution = (vol_factor - 1.0) * vol_weight  # Deviation from neutral

        # Calculate final score
        final_score = (
            ml_contribution +
            orb_contribution +
            global_contribution +
            vol_contribution
        )

        # Determine dynamic threshold
        threshold = self._get_dynamic_threshold(volatility_state, global_market_state)

        # Make decision
        if final_score >= threshold:
            decision = "ALLOW"
            skip_reason = ""
        else:
            decision = "SKIP"
            skip_reason = self._get_skip_reason(
                ml_conf, orb_sig, global_score, vol_factor, threshold, final_score
            )

        result = DecisionScore(
            final_score=final_score,
            ml_confidence=ml_conf,
            ml_weight=ml_weight,
            ml_contribution=ml_contribution,
            orb_signal=orb_sig,
            orb_weight=orb_weight,
            orb_contribution=orb_contribution,
            global_state=global_score,
            global_weight=global_weight,
            global_contribution=global_contribution,
            volatility_factor=vol_factor,
            volatility_weight=vol_weight,
            volatility_contribution=vol_contribution,
            threshold=threshold,
            decision=decision,
            skip_reason=skip_reason,
        )

        logger.debug(
            f"[DecisionIntelligence] {side}: Score={final_score:.3f} "
            f"Threshold={threshold:.3f} Decision={decision}"
        )

        return result

    def _get_dynamic_threshold(
        self,
        volatility_state: str,
        global_market_state=None
    ) -> float:
        """Calculate dynamic threshold based on conditions."""
        threshold = self._base_threshold

        # Adjust for volatility
        if volatility_state == "HIGH":
            threshold = self._vol_high_threshold
        elif volatility_state == "LOW":
            threshold = self._vol_low_threshold

        # Adjust for global risk state
        if self._global_enabled and global_market_state:
            if global_market_state.risk_state == "RISK_OFF":
                threshold += 0.1  # Stricter in risk-off
            elif global_market_state.risk_state == "RISK_ON":
                threshold -= 0.05  # More lenient in risk-on

        return threshold

    def _get_skip_reason(
        self,
        ml_conf: float,
        orb_sig: float,
        global_score: float,
        vol_factor: float,
        threshold: float,
        final_score: float,
    ) -> str:
        """Generate human-readable skip reason."""
        reasons = []

        if ml_conf < 0.5:
            reasons.append(f"low_ml({ml_conf:.2f})")
        if orb_sig < 0.3:
            reasons.append("weak_orb")
        if global_score < 0:
            reasons.append("risk_off")
        if vol_factor < 0.9:
            reasons.append("high_vol")

        if not reasons:
            reasons.append(f"below_threshold({final_score:.2f}<{threshold:.2f})")

        return " | ".join(reasons)

    def get_position_size_adjustment(
        self,
        volatility_state: str = "NORMAL",
        global_market_state=None,
        confidence_multiplier: float = 1.0,
    ) -> float:
        """
        Calculate position size adjustment factor.

        Returns multiplier from 0.7 to 1.0 (1.0 = full size).
        """
        size_mult = 1.0 * confidence_multiplier

        # Reduce size in high volatility
        if volatility_state == "HIGH":
            size_mult *= 0.7  # 30% reduction
        elif volatility_state == "NORMAL":
            size_mult *= 0.85  # 15% reduction

        # Reduce in risk-off
        if self._global_enabled and global_market_state:
            if global_market_state.risk_state == "RISK_OFF":
                size_mult *= 0.8

        return max(0.7, min(1.0, size_mult))


# ── SINGLETON INSTANCE ─────────────────────────────────────────────────────
_intelligence: Optional[DecisionIntelligence] = None


def get_decision_intelligence(config=None) -> DecisionIntelligence:
    """Get or create decision intelligence singleton."""
    global _intelligence
    if _intelligence is None:
        _intelligence = DecisionIntelligence(config=config)
    return _intelligence