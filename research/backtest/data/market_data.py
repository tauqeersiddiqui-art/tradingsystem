"""
Market data loader for research backtest.

Loads historical Bank Nifty 1-minute data and prepares it for backtesting.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os


def load_banknifty_data(csv_path: str = None):
    """
    Load Bank Nifty 1-minute historical data.

    Args:
        csv_path: Path to CSV file (defaults to data/historical/banknifty_1m_full.csv)

    Returns:
        DataFrame with columns: date, open, high, low, close, volume
    """
    if csv_path is None:
        # Default to the historical data file
        csv_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "data", "historical", "banknifty_1m_full.csv"
        )

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Historical data not found: {csv_path}")

    df = pd.read_csv(csv_path)

    # Ensure date column is parsed
    df['date'] = pd.to_datetime(df['date'])

    # Sort by date (ascending)
    df = df.sort_values('date').reset_index(drop=True)

    print(f"Loaded {len(df)} rows from {csv_path}")
    print(f"Date range: {df['date'].min()} to {df['date'].max()}")

    return df


def filter_date_range(df: pd.DataFrame, start_date: datetime, end_date: datetime):
    """
    Filter DataFrame to date range.

    Args:
        df: Input DataFrame
        start_date: Start datetime (inclusive)
        end_date: End datetime (inclusive)

    Returns:
        Filtered DataFrame
    """
    mask = (df['date'] >= start_date) & (df['date'] <= end_date)
    filtered = df[mask].copy()
    print(f"Filtered to {len(filtered)} rows from {start_date.date()} to {end_date.date()}")
    return filtered


def validate_ohlcv(df: pd.DataFrame):
    """
    Validate OHLC data integrity.

    Args:
        df: DataFrame to validate

    Returns:
        True if valid, raises AssertionError if invalid
    """
    assert 'open' in df.columns, "Missing 'open' column"
    assert 'high' in df.columns, "Missing 'high' column"
    assert 'low' in df.columns, "Missing 'low' column"
    assert 'close' in df.columns, "Missing 'close' column"

    # Check OHLC relationships
    invalid_high = (df['high'] < df['low']) | (df['high'] < df['open']) | (df['high'] < df['close'])
    invalid_low = (df['low'] > df['high']) | (df['low'] > df['open']) | (df['low'] > df['close'])

    assert not invalid_high.any(), f"Invalid high values at rows: {invalid_high[invalid_high].index.tolist()[:10]}"
    assert not invalid_low.any(), f"Invalid low values at rows: {invalid_low[invalid_low].index.tolist()[:10]}"

    print("OHLC validation passed")
    return True


if __name__ == "__main__":
    # Test loading
    df = load_banknifty_data()
    validate_ohlcv(df)

    # Test date filtering
    start = datetime(2026, 7, 1)
    end = datetime(2026, 7, 31)
    filtered = filter_date_range(df, start, end)