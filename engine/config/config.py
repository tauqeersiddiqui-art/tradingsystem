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
        # Overtrading guard: was 10 (today fired 10 trades = Rs1320 cost drag).
        # 4 keeps cost bounded; scalp + ML share this counter.
        self.MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 4))

        # Round-trip cost per 30-qty BANKNIFTY lot (buy+sell). Drives the cost-aware
        # profit ladder so it never locks a profit below the trade's own cost.
        self.COST_PER_LOT = float(os.getenv("COST_PER_LOT", 66.0))

        # Lunch chop filter (11:00-12:30). Kept OFF for dry-run testing.
        # Flip LUNCH_FILTER_ENABLED=1 when switching to real money.
        self.LUNCH_FILTER_ENABLED = os.getenv("LUNCH_FILTER_ENABLED", "0") == "1"

        # Re-entry cooldown after any exit (seconds). Raised 180->300 so a
        # stopped option is not re-entered while it is still reversing
        # (this is what produced trade #3's -Rs481 on 2026-06-17).
        self.REENTRY_COOLDOWN = int(os.getenv("REENTRY_COOLDOWN", 300))
        self.SAME_SYMBOL_COOLDOWN = int(os.getenv("SAME_SYMBOL_COOLDOWN", 300))

        # Execution rules
        self.DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", 0.10))
        self.DEFAULT_TARGET_PCT = float(os.getenv("DEFAULT_TARGET_PCT", 0.05))
        self.MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", 300))

        # Lot size: BANKNIFTY = 30 (target instrument)
        self.LOT_SIZE = int(os.getenv("LOT_SIZE", 30))

        # ML
        self.CHAMPION_THRESHOLD = float(os.getenv("CHAMPION_THRESHOLD", 0.42))

        # Scalping layer
        self.SCALP_ENABLED            = os.getenv("SCALP_ENABLED", "1") == "1"
        self.SCALP_SL_PTS             = float(os.getenv("SCALP_SL_PTS", "3.0"))
        self.SCALP_TARGET_PTS         = float(os.getenv("SCALP_TARGET_PTS", "50.0"))  # disabled — trailing SL is primary exit
        self.SCALP_MAX_HOLD_SECONDS   = int(os.getenv("SCALP_MAX_HOLD_SECONDS", "180"))
        self.SCALP_MOMENTUM_WINDOW    = int(os.getenv("SCALP_MOMENTUM_WINDOW", "30"))
        self.SCALP_MOMENTUM_THRESHOLD = float(os.getenv("SCALP_MOMENTUM_THRESHOLD", "12.0"))
        self.SCALP_COOLDOWN           = int(os.getenv("SCALP_COOLDOWN", "60"))
        self.SCALP_LOTS               = int(os.getenv("SCALP_LOTS", "2"))
        self.SCALP_LOCK_PTS           = float(os.getenv("SCALP_LOCK_PTS", "2.0"))   # lock SL at entry after +2pt
        self.SCALP_TRAIL_PTS          = float(os.getenv("SCALP_TRAIL_PTS", "2.0"))  # trail SL 2pt below peak
        self.SCALP_MIN_OPT_PTS        = float(os.getenv("SCALP_MIN_OPT_PTS", "30.0"))  # skip options cheaper than this

        print(f"[CONFIG] Capital={self.INITIAL_CAPITAL} | DRY_RUN={self.DRY_RUN} | LOT_SIZE={self.LOT_SIZE}")