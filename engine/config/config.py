import os


def _env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Config:

    def __init__(self):

        # Modes
        self.PAPER_MODE = os.getenv("PAPER_MODE", "1") == "1"
        self.DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

        # Capital / risk
        self.INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", 100000))
        self.RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", 0.02))
        self.DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", -2000))
        self.DAILY_PROFIT_TARGET = float(os.getenv("DAILY_PROFIT_TARGET", "800"))
        self.DAILY_PROFIT_LOCK_ENABLED = os.getenv("DAILY_PROFIT_LOCK_ENABLED", "1") == "1"
        self.TRADE_LIVE_UPDATE_SECONDS = float(os.getenv("TRADE_LIVE_UPDATE_SECONDS", "2.0"))
        self.TRAIL_ARM_PTS = float(os.getenv("TRAIL_ARM_PTS", "10.0"))
        self.TRAIL_GAP_PTS = float(os.getenv("TRAIL_GAP_PTS", "5.0"))
        # Session trade limit. Scalp + ML share this counter.
        self.MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 20))

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
        self.ENABLE_PHASE55_FILTERS = _env_bool("ENABLE_PHASE55_FILTERS", False)
        self.ENABLE_PHASE55_CE_THRESHOLD = _env_bool("ENABLE_PHASE55_CE_THRESHOLD", True)
        self.ENABLE_PHASE55_PE_THRESHOLD = _env_bool("ENABLE_PHASE55_PE_THRESHOLD", True)
        # Mixed-regime CE block was anti-selective in shadow (blocked 65% winners,
        # 2026-07-06) — default OFF so it never blocks unnecessarily. Opt in via env.
        self.ENABLE_PHASE55_REGIME_FILTER = _env_bool("ENABLE_PHASE55_REGIME_FILTER", False)
        self.ENABLE_PHASE55_TELEMETRY = _env_bool("ENABLE_PHASE55_TELEMETRY", True)
        self.PHASE55_TELEMETRY_DIR = os.getenv("PHASE55_TELEMETRY_DIR", "data/phase55")
        self.PHASE55_CE_QUALITY_THRESHOLD = float(os.getenv("PHASE55_CE_QUALITY_THRESHOLD", "0.4358"))
        self.PHASE55_PE_DIRECTIONAL_THRESHOLD = float(os.getenv("PHASE55_PE_DIRECTIONAL_THRESHOLD", "0.4645"))

        # Scalping layer
        self.SCALP_ENABLED            = os.getenv("SCALP_ENABLED", "1") == "1"
        self.SCALP_SL_PTS             = float(os.getenv("SCALP_SL_PTS", "3.0"))
        self.SCALP_TARGET_PTS         = float(os.getenv("SCALP_TARGET_PTS", "50.0"))  # disabled — trailing SL is primary exit
        self.SCALP_MAX_HOLD_SECONDS   = int(os.getenv("SCALP_MAX_HOLD_SECONDS", "180"))
        self.SCALP_MOMENTUM_WINDOW    = int(os.getenv("SCALP_MOMENTUM_WINDOW", "30"))
        self.SCALP_MOMENTUM_THRESHOLD = float(os.getenv("SCALP_MOMENTUM_THRESHOLD", "12.0"))
        self.SCALP_COOLDOWN           = int(os.getenv("SCALP_COOLDOWN", "60"))
        self.SCALP_LOTS               = int(os.getenv("SCALP_LOTS", "2"))
        self.SCALP_LOCK_PTS           = float(os.getenv("SCALP_LOCK_PTS", "10.0"))  # first lock after +10pt
        self.SCALP_TRAIL_PTS          = float(os.getenv("SCALP_TRAIL_PTS", "5.0"))  # trail SL 5pt below peak
        self.SCALP_STOP_MODIFY_MIN_STEP = float(os.getenv("SCALP_STOP_MODIFY_MIN_STEP", "1.0"))
        self.SCALP_BANK_MFE_RS        = float(os.getenv("SCALP_BANK_MFE_RS", "0.0"))
        self.SCALP_BANK_LOCK_PCT      = float(os.getenv("SCALP_BANK_LOCK_PCT", "0.70"))
        self.SCALP_BANK_MIN_LOCK_RS   = float(os.getenv("SCALP_BANK_MIN_LOCK_RS", "30.0"))
        self.SCALP_PROFIT_SLIPPAGE_BUFFER_RS = float(os.getenv("SCALP_PROFIT_SLIPPAGE_BUFFER_RS", str(self.COST_PER_LOT)))
        self.SCALP_MIN_NET_PROFIT_RS  = float(os.getenv("SCALP_MIN_NET_PROFIT_RS", "0.0"))
        self.SCALP_MIN_TRAIL_MFE_COST_MULT = float(os.getenv("SCALP_MIN_TRAIL_MFE_COST_MULT", "3.0"))
        self.SCALP_MIN_TRAIL_MFE_RS   = float(os.getenv("SCALP_MIN_TRAIL_MFE_RS", "0.0"))
        self.SCALP_DAILY_PROFIT_TARGET = float(os.getenv("SCALP_DAILY_PROFIT_TARGET", "2000.0"))
        self.SCALP_MIN_OPT_PTS        = float(os.getenv("SCALP_MIN_OPT_PTS", "30.0"))  # skip options cheaper than this
        # Max scalp trades per session — reserves remaining slots for ML engine
        self.SCALP_MAX_TRADES         = int(os.getenv("SCALP_MAX_TRADES", "10"))
        # ML filter: skip scalp if opposite ML prob exceeds side's prob by this margin
        self.SCALP_ML_DISAGREE_MARGIN = float(os.getenv("SCALP_ML_DISAGREE_MARGIN", "0.12"))
        # Min absolute ML prob for the trade side before scalp entry is allowed.
        # Scalp is an execution layer, so keep it close to the main AI floor.
        self.SCALP_ML_MIN_PROB        = float(os.getenv("SCALP_ML_MIN_PROB", "0.65"))
        # Min positive ML edge (side_prob - opp_prob) required for scalp entry.
        # Blocks near-neutral entries where models have no view (e.g. right after restart
        # before sufficient candles accumulate, or in choppy open conditions).
        self.SCALP_ML_MIN_EDGE        = float(os.getenv("SCALP_ML_MIN_EDGE", "0.08"))
        self.SCALP_USE_MAIN_AI_THRESHOLD = os.getenv("SCALP_USE_MAIN_AI_THRESHOLD", "1") == "1"
        self.SCALP_REQUIRE_SUPERTREND_CONFIRM = os.getenv("SCALP_REQUIRE_SUPERTREND_CONFIRM", "1") == "1"
        self.SCALP_REQUIRE_HTF5_CONFIRM = os.getenv("SCALP_REQUIRE_HTF5_CONFIRM", "1") == "1"
        self.SCALP_REQUIRE_VWAP_CONFIRM = os.getenv("SCALP_REQUIRE_VWAP_CONFIRM", "1") == "1"
        self.SCALP_VWAP_TOLERANCE       = float(os.getenv("SCALP_VWAP_TOLERANCE", "0.0015"))
        # RANGE_DAY: require 30m HTF bullish before CE scalp (forensic: 3 CE scalp losses on flat days)
        self.SCALP_REQUIRE_HTF30_BULLISH = os.getenv("SCALP_REQUIRE_HTF30_BULLISH", "1") == "1"
        # RANGE_DAY: require stronger spot move before scalp fires (reduces chop noise)
        self.SCALP_RANGE_MOM_THRESH   = float(os.getenv("SCALP_RANGE_MOM_THRESH", "15.0"))

        print(f"[CONFIG] Capital={self.INITIAL_CAPITAL} | DRY_RUN={self.DRY_RUN} | LOT_SIZE={self.LOT_SIZE}")
