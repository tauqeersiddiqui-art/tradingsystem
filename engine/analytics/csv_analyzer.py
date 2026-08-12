# engine/analytics/csv_analyzer.py
#
# CSV-BASED PERFORMANCE ANALYZER — Works without PostgreSQL
#
# Reads trade logs from CSV files (backtest results or live trade logs)
# Computes same metrics as postgres-based analyzer
#
# NO DATABASE REQUIRED. TRUTH FROM CSV FILES.

import os
import logging
import pandas as pd
from datetime import datetime
from typing import List, Dict, Optional

logger = logging.getLogger("csv_analyzer")


def find_trade_csv_files() -> List[str]:
    """Find all trade log CSV files in the system."""

    search_paths = [
        "backtest/results/trade_log.csv",
        "data/trades/trade_log_*.csv",
        "backtest/forensic_trades.csv",
    ]

    found_files = []

    for pattern in search_paths:
        # Expand glob patterns
        import glob
        matches = glob.glob(pattern)
        found_files.extend(matches)

    # Also check weekly logs
    trade_logs = glob.glob("data/trades/trade_log_*.csv")
    found_files.extend(trade_logs)

    # Deduplicate
    found_files = list(set(found_files))

    logger.info(f"[CSV] Found {len(found_files)} trade log files")
    return found_files


def load_trades_from_csv(csv_files: List[str]) -> pd.DataFrame:
    """Load and combine all trade data from CSV files."""

    all_trades = []

    for csv_file in csv_files:
        try:
            # Use on_bad_lines='skip' to handle corrupted lines
            df = pd.read_csv(csv_file, on_bad_lines='skip')
            logger.info(f"[CSV] Loaded {len(df)} trades from {csv_file}")
            all_trades.append(df)
        except Exception as e:
            logger.warning(f"[CSV] Failed to load {csv_file}: {e}")

    if not all_trades:
        logger.warning("[CSV] No trade data found")
        return pd.DataFrame()

    # Combine all dataframes
    combined = pd.concat(all_trades, ignore_index=True)

    # Sort by date and entry_time to ensure chronological order
    if 'date' in combined.columns and 'entry_time' in combined.columns:
        combined['_dt'] = pd.to_datetime(combined['date'] + ' ' + combined['entry_time'], errors='coerce')
        combined = combined.sort_values('_dt').drop(columns=['_dt']).reset_index(drop=True)

    # Remove duplicates - create unique key from file + trade_id since each file restarts at 1
    if 'trade_id' in combined.columns:
        # Add file source to make trade_id unique across files
        # We can't easily track source file after concat, so use index-based dedup
        # or use a composite key from multiple columns
        combined['_row_id'] = range(len(combined))
        combined = combined.drop_duplicates(subset=['symbol', 'entry_time', 'exit_time', 'qty', 'pnl'])
        combined = combined.drop(columns=['_row_id'])

    logger.info(f"[CSV] Total trades loaded: {len(combined)}")

    return combined


def compute_net_pnl(df: pd.DataFrame) -> pd.DataFrame:
    """Add net PnL column (cost-adjusted)."""

    from engine.execution.cost_model import round_trip_cost

    # Handle column name differences (quantity vs qty)
    if 'qty' not in df.columns and 'quantity' in df.columns:
        df['qty'] = df['quantity']

    # Handle NaN values in qty - fill with 0 or drop
    df['qty'] = pd.to_numeric(df['qty'], errors='coerce').fillna(0).astype(int)

    # Compute cost per trade
    df['cost'] = df['qty'].apply(lambda q: round_trip_cost(int(q)))

    # Net PnL = gross PnL - cost
    df['net_pnl'] = df['pnl'] - df['cost']

    return df


def csv_to_trades_dict(df: pd.DataFrame) -> List[Dict]:
    """Convert DataFrame to list of trade dicts (compatible with postgres analyzer)."""

    from engine.execution.cost_model import round_trip_cost

    trades = []

    for _, row in df.iterrows():
        # Combine date + entry_time for full datetime
        date_str = str(row.get('date', ''))
        entry_time_str = str(row.get('entry_time', ''))
        exit_time_str = str(row.get('exit_time', ''))

        try:
            if date_str and entry_time_str and date_str != 'nan' and entry_time_str != 'nan':
                entry_time = pd.to_datetime(f"{date_str} {entry_time_str}")
            else:
                entry_time = pd.NaT
        except Exception:
            entry_time = pd.NaT

        try:
            if date_str and exit_time_str and date_str != 'nan' and exit_time_str != 'nan':
                exit_time = pd.to_datetime(f"{date_str} {exit_time_str}")
            else:
                exit_time = pd.NaT
        except Exception:
            exit_time = pd.NaT

        # Skip trades with NaT times or NaN pnl
        if pd.isna(entry_time) or pd.isna(exit_time):
            continue

        # Handle NaN in numeric fields
        pnl = row.get('pnl', 0)
        if pd.isna(pnl):
            continue

        entry_price = row.get('entry_price', 0)
        if pd.isna(entry_price):
            entry_price = 0

        exit_price = row.get('exit_price', 0)
        if pd.isna(exit_price):
            exit_price = 0

        qty = row.get('qty', 0)
        if pd.isna(qty):
            qty = 0

        net_pnl = row.get('net_pnl', 0)
        if pd.isna(net_pnl):
            net_pnl = float(pnl) - round_trip_cost(int(qty))

        ml_prob = row.get('ml_prob', None)
        if pd.isna(ml_prob):
            ml_prob = None

        trade = {
            'symbol': str(row.get('symbol', 'UNKNOWN')),
            'side': str(row.get('side', 'UNKNOWN')),
            'entry_price': float(entry_price),
            'exit_price': float(exit_price),
            'qty': int(qty),
            'gross_pnl': float(pnl),
            'net_pnl': float(net_pnl),
            'strategy': str(row.get('entry_reason', 'UNKNOWN')),
            'ml_prob': float(ml_prob) if ml_prob is not None else None,
            'regime': str(row.get('regime', 'UNKNOWN')),
            'exit_reason': str(row.get('exit_reason', 'UNKNOWN')),
            'entry_time': entry_time,
            'exit_time': exit_time,
        }
        trades.append(trade)

    return trades


def analyze_from_csv() -> Optional[object]:
    """
    Analyze performance from CSV files (fallback when PostgreSQL unavailable).

    Returns:
        PerformanceReport object (same as postgres analyzer)
    """

    logger.info("[CSV] Starting CSV-based analysis...")

    # Find all CSV files
    csv_files = find_trade_csv_files()

    if not csv_files:
        logger.warning("[CSV] No trade log CSV files found")
        return None

    # Load trades
    df = load_trades_from_csv(csv_files)

    if df.empty:
        logger.warning("[CSV] No trades loaded")
        return None

    # Compute net PnL
    df = compute_net_pnl(df)

    logger.info(f"[CSV] Gross PnL: Rs.{df['pnl'].sum():,.2f}")
    logger.info(f"[CSV] Total Costs: Rs.{df['cost'].sum():,.2f}")
    logger.info(f"[CSV] Net PnL: Rs.{df['net_pnl'].sum():,.2f}")

    # Convert to dict format
    trades = csv_to_trades_dict(df)

    # Use existing performance analyzer
    from engine.analytics.performance_analyzer import (
        _compute_core_metrics,
        _compute_strategy_metrics,
        _compute_time_metrics,
        _compute_regime_metrics,
        _compute_drawdown_analysis,
        _generate_verdict,
        PerformanceReport
    )

    core = _compute_core_metrics(trades)
    strategies = _compute_strategy_metrics(trades)
    time_metrics = _compute_time_metrics(trades)
    regimes = _compute_regime_metrics(trades)
    drawdown = _compute_drawdown_analysis(trades)

    verdict, warnings, recommendations = _generate_verdict(
        core, strategies, time_metrics, regimes, None
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
        reality=None,  # Not available from CSV
        verdict=verdict,
        warnings=warnings,
        recommendations=recommendations
    )

    return report
