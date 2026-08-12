# engine/intelligence/global_market_engine.py
"""
GLOBAL MARKET CONTEXT ENGINE — Failsafe Decision Intelligence Layer

Fetches global market data to provide risk state, volatility, and trend signals.
Designed for robustness: always returns safe defaults if data unavailable.

Data sources (free, low-frequency, cached):
- Yahoo Finance: ^GSPC (S&P 500), ^VIX, DX-Y.NYB (DXY), BTC-USD

Design rules:
- Fetch every 5-10 minutes (NOT per tick)
- Cache results in memory
- If API fails → return last known state or NEUTRAL
- Never block execution: all calls are non-blocking
- NEVER raise exceptions to calling code
"""

import logging
import time
import os
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timedelta
import threading

logger = logging.getLogger("global_market_engine")

# Try importing yfinance - it's a free, standard library
try:
    import yfinance as yf
    _YFINANCE_AVAILABLE = True
except ImportError:
    _YFINANCE_AVAILABLE = False
    logger.warning("yfinance not available - global market engine disabled")


# ── OUTPUT STATES ──────────────────────────────────────────────────────────
RISK_ON     = "RISK_ON"
RISK_OFF    = "RISK_OFF"
NEUTRAL     = "NEUTRAL"

VOL_LOW     = "LOW"
VOL_NORMAL  = "NORMAL"
VOL_HIGH    = "HIGH"

TREND_UP       = "UP"
TREND_DOWN     = "DOWN"
TREND_SIDEWAYS = "SIDEWAYS"

# Default output (fail-safe)
DEFAULT_CONTEXT = {
    "risk_state": NEUTRAL,
    "volatility": VOL_NORMAL,
    "global_trend": TREND_SIDEWAYS,
    "sp500_change": 0.0,
    "vix_level": 0.0,
    "dxy_change": 0.0,
    "btc_change": 0.0,
    "last_updated": None,
    "data_stale": True,
}


@dataclass
class GlobalMarketState:
    """Current global market context."""
    risk_state: str = NEUTRAL
    volatility: str = VOL_NORMAL
    global_trend: str = TREND_SIDEWAYS
    sp500_change: float = 0.0
    vix_level: float = 0.0
    dxy_change: float = 0.0
    btc_change: float = 0.0
    last_updated: Optional[float] = None
    data_stale: bool = True

    def to_dict(self) -> dict:
        return {
            "risk_state": self.risk_state,
            "volatility": self.volatility,
            "global_trend": self.global_trend,
            "sp500_change": self.sp500_change,
            "vix_level": self.vix_level,
            "dxy_change": self.dxy_change,
            "btc_change": self.btc_change,
            "last_updated": self.last_updated,
            "data_stale": self.data_stale,
        }

    def get_risk_score(self) -> float:
        """Convert risk state to numeric score for weighted scoring."""
        if self.risk_state == RISK_ON:
            return 1.0
        elif self.risk_state == RISK_OFF:
            return -1.0
        return 0.0

    def get_volatility_factor(self) -> float:
        """Convert volatility state to factor (0.8-1.2 range)."""
        if self.volatility == VOL_HIGH:
            return 0.8   # reduce confidence
        elif self.volatility == VOL_LOW:
            return 1.1   # slight boost
        return 1.0


class GlobalMarketEngine:
    """
    Global market context provider.

    Fetches global indices to determine overall market risk appetite.
    Designed for fail-safe operation - always returns valid defaults.
    """

    # Configuration
    REFRESH_INTERVAL_SECONDS = 300  # 5 minutes (low frequency)
    STALE_THRESHOLD_SECONDS = 900   # 15 minutes

    # Yahoo Finance tickers
    TICKERS = {
        "sp500": "^GSPC",
        "vix": "^VIX",
        "dxy": "DX-Y.NYB",
        "btc": "BTC-USD",
    }

    def __init__(self, enabled: bool = True):
        self._enabled = enabled and _YFINANCE_AVAILABLE
        self._state = GlobalMarketState()
        self._last_fetch = 0.0
        self._lock = threading.Lock()

        # Configurable thresholds
        self._vix_high = float(os.getenv("GLOBAL_VIX_HIGH", "25.0"))
        self._vix_low = float(os.getenv("GLOBAL_VIX_LOW", "12.0"))
        self._sp500_drop_threshold = float(os.getenv("GLOBAL_SP500_DROP", "-0.5"))
        self._sp500_rally_threshold = float(os.getenv("GLOBAL_SP500_RALLY", "0.5"))

        logger.info(f"[GlobalMarketEngine] Initialized (enabled={self._enabled})")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_state(self) -> GlobalMarketState:
        """
        Get current global market state.
        Non-blocking, returns cached state if stale or disabled.
        """
        with self._lock:
            # Check if refresh needed
            now = time.time()
            if self._enabled and (now - self._last_fetch) >= self.REFRESH_INTERVAL_SECONDS:
                self._fetch_and_update()

            # Check staleness
            if self._state.last_updated is not None:
                age = now - self._state.last_updated
                self._state.data_stale = age > self.STALE_THRESHOLD_SECONDS

            return self._state

    def get_context_dict(self) -> dict:
        """Get state as dictionary for logging/telegram."""
        return self.get_state().to_dict()

    def _fetch_and_update(self) -> None:
        """Fetch latest data from Yahoo Finance. Never raises exceptions."""
        if not self._enabled:
            return

        try:
            sp500 = self._fetch_ticker(self.TICKERS["sp500"])
            vix = self._fetch_ticker(self.TICKERS["vix"])
            dxy = self._fetch_ticker(self.TICKERS["dxy"])
            btc = self._fetch_ticker(self.TICKERS["btc"])

            # Calculate changes
            sp500_change = self._calc_change(sp500)
            dxy_change = self._calc_change(dxy)
            btc_change = self._calc_change(btc)
            vix_level = vix.get("current", 0) if vix else 0

            # Determine states
            risk_state = self._determine_risk_state(sp500_change, vix_level)
            volatility = self._determine_volatility(vix_level)
            trend = self._determine_trend(sp500_change, vix_level)

            with self._lock:
                self._state = GlobalMarketState(
                    risk_state=risk_state,
                    volatility=volatility,
                    global_trend=trend,
                    sp500_change=sp500_change,
                    vix_level=vix_level,
                    dxy_change=dxy_change,
                    btc_change=btc_change,
                    last_updated=time.time(),
                    data_stale=False,
                )
                self._last_fetch = time.time()

            logger.info(
                f"[GlobalMarket] RISK={risk_state} VOL={volatility} TREND={trend} "
                f"SP500={sp500_change:+.2f}% VIX={vix_level:.1f}"
            )

        except Exception as e:
            logger.warning(f"[GlobalMarket] Fetch failed: {e} - using cached state")
            # State remains unchanged (last known or default)

    def _fetch_ticker(self, ticker: str) -> Optional[dict]:
        """Fetch single ticker data. Returns None on failure."""
        if not _YFINANCE_AVAILABLE:
            return None

        try:
            data = yf.Ticker(ticker)
            hist = data.history(period="2d", interval="1d", timeout=10)
            if hist is None or hist.empty or len(hist) < 2:
                return None

            current = hist["Close"].iloc[-1]
            previous = hist["Close"].iloc[-2]
            return {"current": current, "previous": previous}
        except Exception as e:
            logger.debug(f"[GlobalMarket] Failed to fetch {ticker}: {e}")
            return None

    def _calc_change(self, data: Optional[dict]) -> float:
        """Calculate percentage change. Returns 0 on failure."""
        if data is None or data.get("previous", 0) == 0:
            return 0.0
        try:
            return ((data["current"] - data["previous"]) / data["previous"]) * 100
        except (TypeError, ZeroDivisionError):
            return 0.0

    def _determine_risk_state(self, sp500_change: float, vix_level: float) -> str:
        """Determine risk appetite based on S&P 500 and VIX."""
        # High VIX = risk off
        if vix_level >= self._vix_high:
            return RISK_OFF
        # Low VIX + positive S&P = risk on
        if vix_level <= self._vix_low and sp500_change >= self._sp500_rally_threshold:
            return RISK_ON
        # Strong S&P drop = risk off
        if sp500_change <= self._sp500_drop_threshold:
            return RISK_OFF
        # Default to neutral
        return NEUTRAL

    def _determine_volatility(self, vix_level: float) -> str:
        """Determine volatility regime from VIX."""
        if vix_level >= self._vix_high:
            return VOL_HIGH
        if vix_level <= self._vix_low:
            return VOL_LOW
        return VOL_NORMAL

    def _determine_trend(self, sp500_change: float, vix_level: float) -> str:
        """Determine global trend from S&P 500."""
        if sp500_change >= 0.3:
            return TREND_UP
        if sp500_change <= -0.3:
            return TREND_DOWN
        return TREND_SIDEWAYS

    def force_refresh(self) -> None:
        """Force immediate refresh (e.g., at market open)."""
        with self._lock:
            self._last_fetch = 0.0
        self._fetch_and_update()


# ── SINGLETON INSTANCE ─────────────────────────────────────────────────────
_engine: Optional[GlobalMarketEngine] = None


def get_global_market_engine(enabled: bool = True) -> GlobalMarketEngine:
    """Get or create the global market engine singleton."""
    global _engine
    if _engine is None:
        _engine = GlobalMarketEngine(enabled=enabled)
    return _engine