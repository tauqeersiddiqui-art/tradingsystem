# core/context.py

class TradingContext:
    """
    Central runtime container.
    Every module talks through this.
    No direct cross-imports allowed.
    """

    def __init__(self):

        # Market / Data
        self.market = None        # websocket + candles
        self.features = None      # FeatureCache
        self.regime = None        # RegimeDetector
        self.sentiment = None     # SentimentEngine

        # Intelligence
        self.strategies = None    # StrategyHub
        self.meta_ai = None       # MetaAI (RF/XGB)

        # Trading Infra
        self.broker = None
        self.executor = None
        self.risk = None
        self.state = None
        self.options = None
        self.scalp_engine = None

        # Config / Env
        self.config = None

        # Runtime
        self.last_trade = None
        self.cycle_count = 0


    def ready(self):
        """
        Check if all critical modules are loaded
        """

        required = [
            self.market,
            self.features,
            self.regime,
            self.strategies,
            self.meta_ai,
            self.risk,
            self.broker,
            self.executor,
            self.options,
            self.config
        ]

        return all(x is not None for x in required)


    def heartbeat(self):
        """
        For debugging / monitoring
        """

        self.cycle_count += 1

        return {
            "cycle": self.cycle_count,
            "regime": getattr(self.regime, "state", None),
            "sentiment": getattr(self.sentiment, "score", None),
            "last_trade": self.last_trade
        }
