# engine/analytics/strategy_tracker.py
"""
STRATEGY PERFORMANCE TRACKER — Non-Intrusive Intelligence Layer

Tracks per-strategy performance metrics for decision intelligence.
IMPORTANT: This is OBSERVATION ONLY - does NOT auto-kill strategies.

Metrics tracked:
- Win rate (per side: CE/PE)
- Average P&L
- Maximum drawdown
- Last 20 trades performance
- Consecutive wins/losses

All data is in-memory only (resets on restart).
"""

import logging
from dataclasses import dataclass, field
from typing import Optional
from collections import deque
from datetime import datetime

logger = logging.getLogger("strategy_tracker")

# Strategy identifiers
STRATEGY_ORB = "ORB"
STRATEGY_ML = "ML"
STRATEGY_SCALP = "SCALP"


@dataclass
class StrategyMetrics:
    """Performance metrics for a single strategy."""
    name: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    current_drawdown: float = 0.0
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    last_20_pnl: deque = field(default_factory=lambda: deque(maxlen=20))

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return (self.wins / self.total_trades) * 100

    @property
    def avg_pnl(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl / self.total_trades

    @property
    def last_20_pnl_sum(self) -> float:
        return sum(self.last_20_pnl)

    @property
    def last_20_win_rate(self) -> float:
        if len(self.last_20_pnl) == 0:
            return 0.0
        wins = sum(1 for p in self.last_20_pnl if p > 0)
        return (wins / len(self.last_20_pnl)) * 100

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 1),
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl": round(self.avg_pnl, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "last_20_pnl": round(self.last_20_pnl_sum, 2),
            "last_20_wr": round(self.last_20_win_rate, 1),
        }


class StrategyTracker:
    """
    Tracks performance across trading strategies.

    IMPORTANT: This tracker is OBSERVATION ONLY.
    - Logs and exposes metrics
    - Does NOT auto-disable strategies
    - Does NOT block trades based on performance
    """

    # Number of consecutive losses to trigger warning
    CONSECUTIVE_LOSS_WARNING = 3
    # Lookback window for recent performance
    RECENT_WINDOW = 20

    def __init__(self):
        self._strategies: dict[str, StrategyMetrics] = {
            STRATEGY_ORB: StrategyMetrics(name=STRATEGY_ORB),
            STRATEGY_ML: StrategyMetrics(name=STRATEGY_ML),
            STRATEGY_SCALP: StrategyMetrics(name=STRATEGY_SCALP),
        }
        self._last_warning_time = 0.0

        logger.info("[StrategyTracker] Initialized")

    def record_trade(
        self,
        strategy: str,
        pnl: float,
        side: str = "CE",
    ) -> None:
        """
        Record a trade outcome for a strategy.

        Args:
            strategy: Strategy identifier (ORB, ML, SCALP)
            pnl: Profit/Loss from the trade
            side: Trade side (CE or PE)
        """
        if strategy not in self._strategies:
            logger.warning(f"[StrategyTracker] Unknown strategy: {strategy}")
            return

        metrics = self._strategies[strategy]
        metrics.total_trades += 1
        metrics.total_pnl += pnl
        metrics.last_20_pnl.append(pnl)

        # Update win/loss
        if pnl > 0:
            metrics.wins += 1
            metrics.consecutive_wins += 1
            metrics.consecutive_losses = 0
        elif pnl < 0:
            metrics.losses += 1
            metrics.consecutive_losses += 1
            metrics.consecutive_wins = 0
        else:
            # Break even - reset both
            metrics.consecutive_wins = 0
            metrics.consecutive_losses = 0

        # Update drawdown
        if metrics.total_pnl < metrics.current_drawdown:
            metrics.current_drawdown = metrics.total_pnl
            metrics.max_drawdown = min(metrics.max_drawdown, metrics.current_drawdown)
        elif metrics.total_pnl > 0:
            # Recovered some drawdown
            metrics.current_drawdown = min(0, metrics.current_drawdown)

        logger.info(
            f"[StrategyTracker] {strategy} recorded: PnL={pnl:+.0f} "
            f"WR={metrics.win_rate:.1f}% Last20={metrics.last_20_pnl_sum:+.0f}"
        )

        # Check for consecutive loss warning
        if metrics.consecutive_losses >= self.CONSECUTIVE_LOSS_WARNING:
            self._log_warning(strategy, metrics)

    def _log_warning(self, strategy: str, metrics: StrategyMetrics) -> None:
        """Log a warning for poor performance."""
        logger.warning(
            f"[StrategyTracker] ⚠️ {strategy} has {metrics.consecutive_losses} "
            f"consecutive losses. Win rate: {metrics.win_rate:.1f}%, "
            f"Last 20 PnL: {metrics.last_20_pnl_sum:+.0f}"
        )

    def get_metrics(self, strategy: str) -> Optional[dict]:
        """Get metrics for a specific strategy."""
        if strategy not in self._strategies:
            return None
        return self._strategies[strategy].to_dict()

    def get_all_metrics(self) -> dict:
        """Get metrics for all strategies."""
        return {name: m.to_dict() for name, m in self._strategies.items()}

    def get_confidence_adjustment(self, strategy: str) -> float:
        """
        Get confidence multiplier based on recent performance.

        Returns:
            Multiplier from 0.5 to 1.5 (1.0 = neutral)
        """
        if strategy not in self._strategies:
            return 1.0

        metrics = self._strategies[strategy]

        # Base multiplier
        mult = 1.0

        # Reduce confidence after consecutive losses
        if metrics.consecutive_losses >= 3:
            mult -= 0.1 * min(metrics.consecutive_losses, 3)
        elif metrics.consecutive_losses >= 1:
            mult -= 0.05

        # Boost confidence after consecutive wins
        if metrics.consecutive_wins >= 3:
            mult += 0.1 * min(metrics.consecutive_wins, 3)

        # Adjust based on recent window performance
        if len(metrics.last_20_pnl) >= 10:
            recent_pnl = metrics.last_20_pnl_sum
            if recent_pnl < -500:
                mult -= 0.1
            elif recent_pnl > 1000:
                mult += 0.1

        # Clamp to safe range
        return max(0.5, min(1.5, mult))

    def reset_day(self) -> None:
        """Reset daily counters (call at start of each trading day)."""
        for metrics in self._strategies.values():
            metrics.total_trades = 0
            metrics.wins = 0
            metrics.losses = 0
            metrics.total_pnl = 0.0
            metrics.current_drawdown = 0.0
            metrics.max_drawdown = 0.0
            metrics.consecutive_wins = 0
            metrics.consecutive_losses = 0
            metrics.last_20_pnl.clear()
        logger.info("[StrategyTracker] Reset for new day")


# ── SINGLETON INSTANCE ─────────────────────────────────────────────────────
_tracker: Optional[StrategyTracker] = None


def get_strategy_tracker() -> StrategyTracker:
    """Get or create the strategy tracker singleton."""
    global _tracker
    if _tracker is None:
        _tracker = StrategyTracker()
    return _tracker