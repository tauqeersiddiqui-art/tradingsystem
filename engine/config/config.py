import os


class Config:

    def __init__(self):

        # Modes
        self.PAPER_MODE = os.getenv("PAPER_MODE", "1") == "1"
        self.DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

        # Capital / risk
        self.INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", 100000))
        self.RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", 0.02))
        self.DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", -2000))
        self.MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 10))

        # Execution rules
        self.DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", 0.10))
        self.DEFAULT_TARGET_PCT = float(os.getenv("DEFAULT_TARGET_PCT", 0.05))
        self.MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", 300))

        # Lot size: NIFTY changed from 75 → 65 from January 2026
        self.LOT_SIZE = int(os.getenv("LOT_SIZE", 65))

        # ML
        self.CHAMPION_THRESHOLD = float(os.getenv("CHAMPION_THRESHOLD", 0.42))

        print(f"[CONFIG] Capital={self.INITIAL_CAPITAL} | DRY_RUN={self.DRY_RUN} | LOT_SIZE={self.LOT_SIZE}")