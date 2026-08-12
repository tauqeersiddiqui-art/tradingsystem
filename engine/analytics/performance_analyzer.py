# engine/analytics/performance_analyzer.py
#
# PERFORMANCE ANALYZER — Truth extraction from trades table.
#
# NO LOGS. NO SIMULATIONS. ONLY DATABASE TRUTH.
#
# Computes all profitability metrics directly from PostgreSQL trades table.
# Uses NET PnL (cost-adjusted) as the single source of truth.
#
# Metrics computed:
#   - Core: win rate, profit factor, expectancy, max drawdown
#   - Strategy: ORB vs ML vs HYBRID performance
#   - Time: PnL by hour, weekday, session
#   - Regime: PnL by market conditions (Risk ON/OFF, volatility)
#   - Reality: Slippage impact, actual vs theoretical edge

import os
import logging
from datetime import datetime, time as dtime, timedelta
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass, asdict
import json

logger = logging.getLogger("performance_analyzer")


@dataclass
class CoreMetrics:
    """Core profitability metrics."""
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float

    gross_pnl: float
    net_pnl: float
    total_costs: float

    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float

    profit_factor: float
    expectancy: float
    risk_reward_ratio: float

    max_drawdown: float
    max_drawdown_pct: float
    consecutive_losses: int
    recovery_time_hours: Optional[float]


@dataclass
class StrategyMetrics:
    """Per-strategy performance breakdown."""
    strategy: str
    trades: int
    win_rate: float
    net_pnl: float
    expectancy: float
    profit_factor: float
    max_drawdown: float


@dataclass
class TimeMetrics:
    """Time-based performance analysis."""
    pnl_by_hour: Dict[int, float]
    trades_by_hour: Dict[int, int]
    pnl_by_weekday: Dict[str, float]
    first_hour_pnl: float
    rest_of_day_pnl: float
    best_hour: Tuple[int, float]
    worst_hour: Tuple[int, float]


@dataclass
class RegimeMetrics:
    """Market regime performance."""
    regime: str
    trades: int
    win_rate: float
    net_pnl: float
    expectancy: float


@dataclass
class DrawdownAnalysis:
    """Detailed drawdown analysis."""
    equity_curve: List[Tuple[datetime, float]]
    max_drawdown: float
    max_drawdown_pct: float
    max_drawdown_start: datetime
    max_drawdown_end: datetime
    consecutive_losses: int
    longest_losing_streak: List[Dict]


@dataclass
class RealityCheck:
    """Slippage and execution reality."""
    theoretical_edge: float
    actual_edge: float
    slippage_cost: float
    slippage_impact_pct: float
    avg_slippage_pts: float
    execution_quality_score: float


@dataclass
class PerformanceReport:
    """Complete performance report."""
    generated_at: datetime
    date_range: Tuple[datetime, datetime]

    core: CoreMetrics
    strategies: List[StrategyMetrics]
    time: TimeMetrics
    regimes: List[RegimeMetrics]
    drawdown: DrawdownAnalysis
    reality: Optional[RealityCheck]

    verdict: str
    warnings: List[str]
    recommendations: List[str]


def analyze_performance(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    include_reality_check: bool = True
) -> PerformanceReport:
    """
    Analyze complete trading performance from database.

    Args:
        start_date: Analysis start (default: all data)
        end_date: Analysis end (default: now)
        include_reality_check: Include execution audit analysis

    Returns:
        PerformanceReport with all metrics
    """
    logger.info("[PERF] Starting performance analysis...")

    # Fetch trades from database
    trades = _fetch_trades(start_date, end_date)

    if not trades:
        logger.warning("[PERF] No trades found in database")
        return _empty_report()

    logger.info(f"[PERF] Analyzing {len(trades)} trades")

    # Compute all metrics
    core = _compute_core_metrics(trades)
    strategies = _compute_strategy_metrics(trades)
    time_metrics = _compute_time_metrics(trades)
    regimes = _compute_regime_metrics(trades)
    drawdown = _compute_drawdown_analysis(trades)

    reality = None
    if include_reality_check:
        reality = _compute_reality_check(trades)

    # Generate verdict
    verdict, warnings, recommendations = _generate_verdict(
        core, strategies, time_metrics, regimes, reality
    )

    # Build report
    date_range = (
        trades[0]["entry_time"],
        trades[-1]["exit_time"]
    )

    report = PerformanceReport(
        generated_at=datetime.now(),
        date_range=date_range,
        core=core,
        strategies=strategies,
        time=time_metrics,
        regimes=regimes,
        drawdown=drawdown,
        reality=reality,
        verdict=verdict,
        warnings=warnings,
        recommendations=recommendations
    )

    logger.info(f"[PERF] Analysis complete: {verdict}")

    return report


def _fetch_trades(start_date, end_date) -> List[Dict]:
    """Fetch trades from PostgreSQL trades table."""
    from engine.storage.postgres_client import get_client

    client = get_client()

    try:
        conn = client._pool.getconn()
        cur = conn.cursor()

        # Build query
        query = """
            SELECT
                id, symbol, side, entry_price, exit_price, qty,
                gross_pnl, net_pnl, strategy, ml_prob, regime,
                exit_reason, entry_time, exit_time
            FROM trades
            WHERE 1=1
        """

        params = []

        if start_date:
            query += " AND entry_time >= %s"
            params.append(start_date)

        if end_date:
            query += " AND entry_time <= %s"
            params.append(end_date)

        query += " ORDER BY entry_time ASC"

        cur.execute(query, params)

        columns = [desc[0] for desc in cur.description]
        trades = []

        for row in cur.fetchall():
            trade = dict(zip(columns, row))
            # Convert Decimal to float
            for k in ["entry_price", "exit_price", "gross_pnl", "net_pnl", "ml_prob"]:
                if trade.get(k) is not None:
                    trade[k] = float(trade[k])
            trades.append(trade)

        client._pool.putconn(conn)

        return trades

    except Exception as e:
        logger.error(f"[PERF] Failed to fetch trades: {e}")
        return []


def _compute_core_metrics(trades: List[Dict]) -> CoreMetrics:
    """Compute core profitability metrics."""

    total_trades = len(trades)

    winners = [t for t in trades if t["net_pnl"] > 0]
    losers = [t for t in trades if t["net_pnl"] <= 0]

    winning_trades = len(winners)
    losing_trades = len(losers)
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    # PnL
    gross_pnl = sum(t["gross_pnl"] for t in trades)
    net_pnl = sum(t["net_pnl"] for t in trades)
    total_costs = gross_pnl - net_pnl

    # Win/Loss stats
    avg_win = sum(t["net_pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["net_pnl"] for t in losers) / len(losers) if losers else 0
    largest_win = max((t["net_pnl"] for t in winners), default=0)
    largest_loss = min((t["net_pnl"] for t in losers), default=0)

    # Profit factor
    total_wins = sum(t["net_pnl"] for t in winners)
    total_losses = abs(sum(t["net_pnl"] for t in losers))
    profit_factor = (total_wins / total_losses) if total_losses > 0 else 0

    # Expectancy
    expectancy = net_pnl / total_trades if total_trades > 0 else 0

    # Risk/Reward
    risk_reward_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    # Drawdown
    dd_analysis = _compute_drawdown_metrics(trades)

    return CoreMetrics(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=round(win_rate, 2),
        gross_pnl=round(gross_pnl, 2),
        net_pnl=round(net_pnl, 2),
        total_costs=round(total_costs, 2),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        largest_win=round(largest_win, 2),
        largest_loss=round(largest_loss, 2),
        profit_factor=round(profit_factor, 2),
        expectancy=round(expectancy, 2),
        risk_reward_ratio=round(risk_reward_ratio, 2),
        max_drawdown=dd_analysis["max_dd"],
        max_drawdown_pct=dd_analysis["max_dd_pct"],
        consecutive_losses=dd_analysis["consecutive_losses"],
        recovery_time_hours=dd_analysis["recovery_time_hours"]
    )


def _compute_drawdown_metrics(trades: List[Dict]) -> Dict:
    """Compute drawdown metrics from trade sequence."""

    equity = 0
    peak = 0
    max_dd = 0
    max_dd_pct = 0

    consecutive_losses = 0
    max_consecutive = 0

    for trade in trades:
        equity += trade["net_pnl"]

        if equity > peak:
            peak = equity

        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak * 100) if peak > 0 else 0

        if trade["net_pnl"] <= 0:
            consecutive_losses += 1
            max_consecutive = max(max_consecutive, consecutive_losses)
        else:
            consecutive_losses = 0

    # Recovery time (hours from max DD to recovery)
    recovery_time_hours = None
    # TODO: Implement recovery time calculation

    return {
        "max_dd": round(max_dd, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "consecutive_losses": max_consecutive,
        "recovery_time_hours": recovery_time_hours
    }


def _compute_strategy_metrics(trades: List[Dict]) -> List[StrategyMetrics]:
    """Compute per-strategy performance."""

    strategies = {}

    for trade in trades:
        strategy = trade.get("strategy") or "UNKNOWN"

        if strategy not in strategies:
            strategies[strategy] = []

        strategies[strategy].append(trade)

    results = []

    for strategy, strat_trades in strategies.items():
        total = len(strat_trades)
        winners = [t for t in strat_trades if t["net_pnl"] > 0]

        win_rate = (len(winners) / total * 100) if total > 0 else 0
        net_pnl = sum(t["net_pnl"] for t in strat_trades)
        expectancy = net_pnl / total if total > 0 else 0

        # Profit factor
        total_wins = sum(t["net_pnl"] for t in winners)
        losers = [t for t in strat_trades if t["net_pnl"] <= 0]
        total_losses = abs(sum(t["net_pnl"] for t in losers))
        profit_factor = (total_wins / total_losses) if total_losses > 0 else 0

        # Max DD for this strategy
        dd = _compute_drawdown_metrics(strat_trades)

        results.append(StrategyMetrics(
            strategy=strategy,
            trades=total,
            win_rate=round(win_rate, 2),
            net_pnl=round(net_pnl, 2),
            expectancy=round(expectancy, 2),
            profit_factor=round(profit_factor, 2),
            max_drawdown=dd["max_dd"]
        ))

    # Sort by net PnL descending
    results.sort(key=lambda x: x.net_pnl, reverse=True)

    return results


def _compute_time_metrics(trades: List[Dict]) -> TimeMetrics:
    """Compute time-based performance."""

    pnl_by_hour = {}
    trades_by_hour = {}
    pnl_by_weekday = {
        "Monday": 0, "Tuesday": 0, "Wednesday": 0,
        "Thursday": 0, "Friday": 0
    }

    first_hour_trades = []
    rest_trades = []

    for trade in trades:
        entry_time = trade["entry_time"]
        hour = entry_time.hour
        weekday = entry_time.strftime("%A")

        # By hour
        pnl_by_hour[hour] = pnl_by_hour.get(hour, 0) + trade["net_pnl"]
        trades_by_hour[hour] = trades_by_hour.get(hour, 0) + 1

        # By weekday
        if weekday in pnl_by_weekday:
            pnl_by_weekday[weekday] += trade["net_pnl"]

        # First hour vs rest
        if 9 <= hour < 10:
            first_hour_trades.append(trade)
        else:
            rest_trades.append(trade)

    first_hour_pnl = sum(t["net_pnl"] for t in first_hour_trades)
    rest_of_day_pnl = sum(t["net_pnl"] for t in rest_trades)

    # Best/worst hour
    if pnl_by_hour:
        best_hour = max(pnl_by_hour.items(), key=lambda x: x[1])
        worst_hour = min(pnl_by_hour.items(), key=lambda x: x[1])
    else:
        best_hour = (0, 0)
        worst_hour = (0, 0)

    return TimeMetrics(
        pnl_by_hour={k: round(v, 2) for k, v in pnl_by_hour.items()},
        trades_by_hour=trades_by_hour,
        pnl_by_weekday={k: round(v, 2) for k, v in pnl_by_weekday.items()},
        first_hour_pnl=round(first_hour_pnl, 2),
        rest_of_day_pnl=round(rest_of_day_pnl, 2),
        best_hour=(best_hour[0], round(best_hour[1], 2)),
        worst_hour=(worst_hour[0], round(worst_hour[1], 2))
    )


def _compute_regime_metrics(trades: List[Dict]) -> List[RegimeMetrics]:
    """Compute performance by market regime."""

    regimes = {}

    for trade in trades:
        regime = trade.get("regime") or "UNKNOWN"

        if regime not in regimes:
            regimes[regime] = []

        regimes[regime].append(trade)

    results = []

    for regime, regime_trades in regimes.items():
        total = len(regime_trades)
        winners = [t for t in regime_trades if t["net_pnl"] > 0]

        win_rate = (len(winners) / total * 100) if total > 0 else 0
        net_pnl = sum(t["net_pnl"] for t in regime_trades)
        expectancy = net_pnl / total if total > 0 else 0

        results.append(RegimeMetrics(
            regime=regime,
            trades=total,
            win_rate=round(win_rate, 2),
            net_pnl=round(net_pnl, 2),
            expectancy=round(expectancy, 2)
        ))

    results.sort(key=lambda x: x.net_pnl, reverse=True)

    return results


def _compute_drawdown_analysis(trades: List[Dict]) -> DrawdownAnalysis:
    """Detailed drawdown analysis with equity curve."""

    equity_curve = []
    equity = 0
    peak = 0
    max_dd = 0
    max_dd_pct = 0
    dd_start = None
    dd_end = None

    consecutive_losses = 0
    current_streak = []
    longest_streak = []

    for trade in trades:
        equity += trade["net_pnl"]
        equity_curve.append((trade["exit_time"], equity))

        if equity > peak:
            peak = equity
            dd_start = trade["exit_time"]

        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = (dd / peak * 100) if peak > 0 else 0
            dd_end = trade["exit_time"]

        if trade["net_pnl"] <= 0:
            consecutive_losses += 1
            current_streak.append({
                "symbol": trade["symbol"],
                "net_pnl": trade["net_pnl"],
                "time": trade["exit_time"]
            })
        else:
            if len(current_streak) > len(longest_streak):
                longest_streak = current_streak[:]
            current_streak = []
            consecutive_losses = 0

    # Check final streak
    if len(current_streak) > len(longest_streak):
        longest_streak = current_streak

    return DrawdownAnalysis(
        equity_curve=equity_curve,
        max_drawdown=round(max_dd, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        max_drawdown_start=dd_start,
        max_drawdown_end=dd_end,
        consecutive_losses=len(longest_streak),
        longest_losing_streak=longest_streak
    )


def _compute_reality_check(trades: List[Dict]) -> Optional[RealityCheck]:
    """Compute execution reality vs theoretical edge."""

    try:
        from engine.analytics.execution_audit import get_today_audit_summary

        audit = get_today_audit_summary()

        if audit["status"] != "ok":
            return None

        # Theoretical edge (gross PnL)
        gross_pnl = sum(t["gross_pnl"] for t in trades)

        # Actual edge (net PnL)
        net_pnl = sum(t["net_pnl"] for t in trades)

        # Slippage cost
        slippage_cost = audit.get("total_slippage_cost_rs", 0)

        # Total impact
        total_costs = gross_pnl - net_pnl
        slippage_impact_pct = (slippage_cost / abs(gross_pnl) * 100) if gross_pnl != 0 else 0

        # Quality score (0-100)
        avg_slippage = audit.get("avg_entry_slippage_pts", 0) + audit.get("avg_exit_slippage_pts", 0)
        quality_score = max(0, 100 - (avg_slippage * 10))

        return RealityCheck(
            theoretical_edge=round(gross_pnl, 2),
            actual_edge=round(net_pnl, 2),
            slippage_cost=round(slippage_cost, 2),
            slippage_impact_pct=round(slippage_impact_pct, 2),
            avg_slippage_pts=round(avg_slippage, 2),
            execution_quality_score=round(quality_score, 2)
        )

    except Exception as e:
        logger.warning(f"[PERF] Reality check unavailable: {e}")
        return None


def _generate_verdict(
    core: CoreMetrics,
    strategies: List[StrategyMetrics],
    time: TimeMetrics,
    regimes: List[RegimeMetrics],
    reality: Optional[RealityCheck]
) -> Tuple[str, List[str], List[str]]:
    """Generate verdict, warnings, and recommendations."""

    warnings = []
    recommendations = []

    # Critical checks
    if core.expectancy < 0:
        verdict = "LOSING SYSTEM"
        warnings.append("CRITICAL: Negative expectancy — system loses money on average")
        recommendations.append("STOP TRADING — fundamental strategy flaw")
        return verdict, warnings, recommendations

    if core.profit_factor < 1.0:
        verdict = "LOSING SYSTEM"
        warnings.append("CRITICAL: Profit factor < 1.0 — total losses exceed total wins")
        recommendations.append("STOP TRADING — strategy is not profitable")
        return verdict, warnings, recommendations

    # Profitability with warnings
    if core.profit_factor < 1.3:
        warnings.append(f"Weak profit factor: {core.profit_factor} (threshold: 1.3)")

    if core.win_rate < 35 and core.risk_reward_ratio < 1.5:
        warnings.append(f"Low win rate ({core.win_rate}%) with poor RR ({core.risk_reward_ratio})")

    if core.max_drawdown_pct > 40:
        warnings.append(f"High drawdown: {core.max_drawdown_pct}% of peak equity")

    # Reality check warnings
    if reality and reality.slippage_impact_pct > 30:
        warnings.append(f"Slippage eats {reality.slippage_impact_pct}% of gross profits")

    # Stability assessment
    unstable = core.max_drawdown_pct > 40 or (core.max_drawdown / abs(core.net_pnl) > 0.4 if core.net_pnl != 0 else True)

    if core.net_pnl > 0:
        if unstable or len(warnings) > 2:
            verdict = "PROFITABLE BUT UNSTABLE"
        else:
            verdict = "PROFITABLE & STABLE"
    else:
        verdict = "BREAK-EVEN"

    # Recommendations
    if strategies:
        best = strategies[0]
        worst = strategies[-1]

        if best.net_pnl > 0 and worst.net_pnl < 0:
            recommendations.append(f"Focus on {best.strategy} (₹{best.net_pnl:.0f}), avoid {worst.strategy} (₹{worst.net_pnl:.0f})")

    if time.first_hour_pnl > time.rest_of_day_pnl * 2:
        recommendations.append("Strong first-hour edge — consider exit by 10:30")

    if regimes:
        for regime in regimes:
            if regime.net_pnl < 0:
                recommendations.append(f"Avoid trading in {regime.regime} regime (losing ₹{abs(regime.net_pnl):.0f})")

    if core.consecutive_losses > 5:
        recommendations.append(f"Implement circuit breaker after {core.consecutive_losses} losses")

    return verdict, warnings, recommendations


def _empty_report() -> PerformanceReport:
    """Return empty report when no trades available."""
    return PerformanceReport(
        generated_at=datetime.now(),
        date_range=(datetime.now(), datetime.now()),
        core=CoreMetrics(
            total_trades=0, winning_trades=0, losing_trades=0, win_rate=0,
            gross_pnl=0, net_pnl=0, total_costs=0,
            avg_win=0, avg_loss=0, largest_win=0, largest_loss=0,
            profit_factor=0, expectancy=0, risk_reward_ratio=0,
            max_drawdown=0, max_drawdown_pct=0, consecutive_losses=0,
            recovery_time_hours=None
        ),
        strategies=[],
        time=TimeMetrics(
            pnl_by_hour={}, trades_by_hour={}, pnl_by_weekday={},
            first_hour_pnl=0, rest_of_day_pnl=0,
            best_hour=(0, 0), worst_hour=(0, 0)
        ),
        regimes=[],
        drawdown=DrawdownAnalysis(
            equity_curve=[], max_drawdown=0, max_drawdown_pct=0,
            max_drawdown_start=datetime.now(), max_drawdown_end=datetime.now(),
            consecutive_losses=0, longest_losing_streak=[]
        ),
        reality=None,
        verdict="NO DATA",
        warnings=["No trades found in database"],
        recommendations=["Trade first, then analyze"]
    )
