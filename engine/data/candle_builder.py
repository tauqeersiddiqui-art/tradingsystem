# engine/data/candle_builder.py
# Real-time 1-minute OHLC candle aggregator from WebSocket ticks
# Drop-in replacement for CSV-based DataManager.get_latest_window()

import logging
import threading
from collections import deque
from datetime import datetime, timedelta
import os

import pandas as pd

logger = logging.getLogger("candle_builder")

_CANDLE_INTERVAL_SECONDS = 60   # 1-minute candles


class CandleBuilder:
    """
    Converts raw WebSocket ticks (_last_ticks) into a rolling buffer
    of completed 1-minute OHLC candles.

    Usage (from engine_loop):
        builder = CandleBuilder(broker, token, max_candles=200)
        builder.start()

        # every loop cycle:
        candle   = builder.latest_candle()      # most recent completed candle dict
        df       = builder.get_window(120)       # DataFrame of last N candles
        ltp      = builder.ltp()                 # latest tick price
    """

    def __init__(self, broker, instrument_token: int, max_candles: int = 300):
        """
        broker           : ZerodhaBroker instance (has ._last_ticks)
        instrument_token : int token for the index/option being watched
        max_candles      : rolling buffer depth
        """
        self.broker           = broker
        self.instrument_token = instrument_token
        self.max_candles      = max_candles

        # Rolling buffer of completed candles (dicts)
        self._candles: deque = deque(maxlen=max_candles)
        self._lock    = threading.Lock()

        # Current (incomplete) candle being built
        self._wip: dict | None = None          # work-in-progress candle
        self._wip_minute: datetime | None = None

        # Last known tick price
        self._ltp: float | None = None
        self._last_tick_time: float = 0.0

        logger.info(
            f"[CandleBuilder] token={instrument_token} "
            f"interval=1m buffer={max_candles}"
        )

    # ══════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════════════

    def ltp(self) -> float | None:
        """Latest tick price. Falls back to broker REST if no tick yet."""
        if self._ltp is not None:
            return self._ltp
        # Fallback: REST call (slow, only on cold start)
        try:
            from engine.config.config import INDEX_SPOT_KEY
            sym = INDEX_SPOT_KEY
            data = self.broker.kite.ltp(sym)
            price = data[sym]["last_price"]
            self._ltp = float(price)
            return self._ltp
        except Exception as e:
            logger.warning(f"[CandleBuilder] REST ltp fallback failed: {e}")
            return None

    def latest_candle(self) -> dict | None:
        """Most recently *completed* 1-minute candle."""
        with self._lock:
            return dict(self._candles[-1]) if self._candles else None

    def get_window(self, n: int = 120) -> pd.DataFrame | None:
        """
        Returns a DataFrame of the last N completed candles.
        Columns: open, high, low, close, volume, ts
        Returns None if fewer than 26 candles available.
        """
        with self._lock:
            if len(self._candles) < 26:
                return None
            candles = list(self._candles)[-n:]

        df = pd.DataFrame(candles)
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.sort_values("ts").reset_index(drop=True)
        return df

    def current_wip(self) -> dict | None:
        """Current in-progress (incomplete) candle for the active minute.
        Returns a copy of the WIP dict or None if not started yet."""
        with self._lock:
            return dict(self._wip) if self._wip is not None else None

    def candle_count(self) -> int:
        with self._lock:
            return len(self._candles)

    # ══════════════════════════════════════════════════════════════════
    # TICK PROCESSOR  (call this every engine loop cycle)
    # ══════════════════════════════════════════════════════════════════

    def process_tick(self, ts: datetime | None = None) -> bool:
        """
        Reads the latest tick from broker._last_ticks for this token.
        Aggregates into current-minute WIP candle.
        Finalises (seals) the WIP candle when the minute rolls over.

        Returns True if a new candle was completed this call.
        """
        if ts is None:
            ts = datetime.now()

        tick = self.broker._last_ticks.get(self.instrument_token)

        if tick is None:
            # WebSocket has not delivered a tick for this token yet.
            # Fall back to REST once to avoid a cold-start stall, but this
            # WILL produce flat candles (high==low) until the WS feeds.
            # Log at WARNING so the operator can see the feed is not live.
            price = self.ltp()
            if price is None:
                return False
            logger.warning(
                f"[CandleBuilder] No WS tick for token={self.instrument_token} "
                f"— using cached/REST price={price:.2f}. "
                "Candles will be flat until WebSocket feed recovers."
            )
            tick = {
                "instrument_token": self.instrument_token,
                "last_price": price,
                "volume": 0,
            }

        price  = float(tick.get("last_price", 0))
        volume = int(tick.get("volume_traded", tick.get("volume", 0)))

        if price <= 0:
            return False

        self._ltp = price

        # Current candle minute boundary (floor to minute)
        current_minute = ts.replace(second=0, microsecond=0)

        new_candle_completed = False

        with self._lock:
            if self._wip is None or current_minute != self._wip_minute:
                # Seal the previous WIP candle (if any)
                if self._wip is not None:
                    self._candles.append(dict(self._wip))
                    new_candle_completed = True
                    logger.debug(
                        f"[CandleBuilder] Sealed: "
                        f"{self._wip['ts']} "
                        f"O={self._wip['open']:.2f} "
                        f"H={self._wip['high']:.2f} "
                        f"L={self._wip['low']:.2f} "
                        f"C={self._wip['close']:.2f}"
                    )

                # Start new WIP candle
                self._wip = {
                    "ts":     current_minute,
                    "open":   price,
                    "high":   price,
                    "low":    price,
                    "close":  price,
                    "volume": volume,
                }
                self._wip_minute = current_minute

            else:
                # Update existing WIP candle
                self._wip["high"]   = max(self._wip["high"],  price)
                self._wip["low"]    = min(self._wip["low"],   price)
                self._wip["close"]  = price
                self._wip["volume"] = max(self._wip["volume"], volume)

        return new_candle_completed

    # ══════════════════════════════════════════════════════════════════
    # HISTORICAL SEED  (warm the buffer at startup from CSV)
    # ══════════════════════════════════════════════════════════════════

    def seed_from_csv(self, csv_path: str, n: int = 200):
        """
        At startup, pre-fill the candle buffer from the historical CSV.
        Includes today's intraday candles (up to 2 minutes ago) so
        indicators warm up immediately after update_historical_data runs.
        """
        if not os.path.exists(csv_path):
            logger.warning(f"[CandleBuilder] Seed CSV not found: {csv_path}")
            return

        try:
            df = pd.read_csv(csv_path)
            date_col = next(
                (c for c in ["date", "datetime", "timestamp", "time"]
                 if c in df.columns), None
            )
            if date_col is None:
                logger.warning("[CandleBuilder] Seed CSV: no date column found")
                return

            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
            df = df.dropna(subset=[date_col])
            df = df.sort_values(date_col)

            # Seed all candles up to 2 minutes ago (avoids incomplete current candle).
            # Includes today's data because update_historical_data() runs before this.
            cutoff = datetime.now() - timedelta(minutes=2)
            df = df[df[date_col] <= cutoff]

            df = df.tail(n)

            with self._lock:
                for _, row in df.iterrows():
                    candle = {
                        "ts":     row[date_col],
                        "open":   float(row.get("open",  row.get("close", 0))),
                        "high":   float(row.get("high",  row.get("close", 0))),
                        "low":    float(row.get("low",   row.get("close", 0))),
                        "close":  float(row.get("close", 0)),
                        "volume": int(row.get("volume",  0)),
                    }
                    if candle["close"] > 0:
                        self._candles.append(candle)

            logger.info(
                f"[CandleBuilder] Seeded {len(self._candles)} historical candles"
            )

        except Exception as e:
            logger.error(f"[CandleBuilder] Seed failed: {e}")

    # ══════════════════════════════════════════════════════════════════
    # PAPER MODE SEED  (replay from CSV for paper trading)
    # ══════════════════════════════════════════════════════════════════

    def seed_paper_mode(self, csv_path: str, n: int = 200):
        """
        In paper mode there are no WebSocket ticks.
        Loads the last N candles from CSV into buffer (including today's data).
        process_tick() will still update from mock broker's static price.
        """
        import os
        if not os.path.exists(csv_path):
            logger.warning(f"[CandleBuilder] Paper seed CSV not found: {csv_path}")
            return

        try:
            df = pd.read_csv(csv_path)
            date_col = next(
                (c for c in ["date", "datetime", "timestamp", "time"]
                 if c in df.columns), None
            )
            if date_col:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
                df = df.dropna(subset=[date_col]).sort_values(date_col)

            df = df.tail(n)

            with self._lock:
                for _, row in df.iterrows():
                    ts_val = row[date_col] if date_col else datetime.now()
                    candle = {
                        "ts":     ts_val,
                        "open":   float(row.get("open",  row.get("close", 0))),
                        "high":   float(row.get("high",  row.get("close", 0))),
                        "low":    float(row.get("low",   row.get("close", 0))),
                        "close":  float(row.get("close", 0)),
                        "volume": int(row.get("volume",  0)),
                    }
                    if candle["close"] > 0:
                        self._candles.append(candle)

            # Set initial ltp from last candle
            if self._candles:
                self._ltp = self._candles[-1]["close"]

            logger.info(
                f"[CandleBuilder] Paper mode seeded {len(self._candles)} candles | "
                f"LTP={self._ltp}"
            )

        except Exception as e:
            logger.error(f"[CandleBuilder] Paper seed failed: {e}")

    # ══════════════════════════════════════════════════════════════════
    # NIFTY INSTRUMENT TOKEN HELPER
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def index_token() -> int:
        """Zerodha instrument token of the traded index (config-driven)."""
        from engine.config.config import INDEX_TOKEN
        return INDEX_TOKEN

    # Backward-compatible alias
    nifty_token = index_token
