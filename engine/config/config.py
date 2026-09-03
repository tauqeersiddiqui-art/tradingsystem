import os


# ══════════════════════════════════════════════════════════════════════
# TRADED INSTRUMENT — SINGLE SOURCE OF TRUTH (env-overridable via .env)
# No engine module may hardcode an index name, token, spot key, strike
# step or dataset path — everything imports from here.
# Default = BANKNIFTY: Kite name "NIFTY BANK", token 260105,
# 100-pt strike grid, 30 qty/lot.
# ══════════════════════════════════════════════════════════════════════
INDEX_NAME        = os.getenv("INDEX_NAME",        "NIFTY BANK")      # Kite instruments `name` (option-chain filter)
INDEX_SPOT_KEY    = os.getenv("INDEX_SPOT_KEY",    "NSE:NIFTY BANK")  # Kite quote key for spot LTP
INDEX_FEED_SYMBOL = os.getenv("INDEX_FEED_SYMBOL", "NIFTY BANK")      # symbol inside instrument_map for the WS feed
INDEX_TOKEN       = int(os.getenv("INDEX_TOKEN",   "260105"))         # Zerodha instrument token of the index
STRIKE_STEP       = int(os.getenv("STRIKE_STEP",   "100"))            # strike interval in points
HIST_CSV          = os.getenv("HIST_CSV",          "data/historical/banknifty_1m_full.csv")
ATM_DRIFT_PTS     = int(os.getenv("ATM_DRIFT_PTS", "150"))            # option re-subscribe drift threshold


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

        # Round-trip cost per 65-qty lot (buy+sell). Drives the cost-aware
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

        # Lot size: BANKNIFTY = 30 qty per lot (matches the cost model and
        # the historical trade logs). Was wrongly set to NIFTY 65.
        self.LOT_SIZE = int(os.getenv("LOT_SIZE", "30"))
        # Re-subscribe the option chain after this much ATM drift (pts)
        self.ATM_DRIFT_PTS = int(os.getenv("ATM_DRIFT_PTS", "150"))

        # ML
        self.CHAMPION_THRESHOLD = float(os.getenv("CHAMPION_THRESHOLD", 0.42))

        # Scalping layer — Aug-18 WFO fix set (from feat/obsidian-clean ee6f507):
        # adaptive SL by conviction, 20pt momentum threshold, HTF agreement,
        # exhaustion cap, no-life exit, staged BE→trail exit, ML gate,
        # circuit breaker, scalp daily cap. All env-overridable.
        self.SCALP_ENABLED            = os.getenv("SCALP_ENABLED", "1") == "1"
        # Adaptive scalp SL: strict floor / medium / wide — chosen per-entry by
        # scalp_engine.adaptive_sl_pts() based on move strength + HTF + VWAP.
        self.SCALP_SL_PTS             = float(os.getenv("SCALP_SL_PTS", "3.0"))
        self.SCALP_SL_MED_PTS         = float(os.getenv("SCALP_SL_MED_PTS", "5.0"))
        self.SCALP_SL_WIDE_PTS        = float(os.getenv("SCALP_SL_WIDE_PTS", "8.0"))
        self.SCALP_TARGET_PTS         = float(os.getenv("SCALP_TARGET_PTS", "50.0"))  # disabled — trailing SL is primary exit
        self.SCALP_MAX_HOLD_SECONDS   = int(os.getenv("SCALP_MAX_HOLD_SECONDS", "180"))
        self.SCALP_MOMENTUM_WINDOW    = int(os.getenv("SCALP_MOMENTUM_WINDOW", "30"))
        # WFO: 12pt fired on noise (most Aug-18 losses); strong 18-35pt moves won.
        self.SCALP_MOMENTUM_THRESHOLD = float(os.getenv("SCALP_MOMENTUM_THRESHOLD", "20.0"))
        self.SCALP_COOLDOWN           = int(os.getenv("SCALP_COOLDOWN", "240"))  # WFO: 60→240, patience avoids reversal re-entry
        self.SCALP_LOTS               = int(os.getenv("SCALP_LOTS", "2"))
        self.SCALP_MIN_OPT_PTS        = float(os.getenv("SCALP_MIN_OPT_PTS", "30.0"))  # skip options cheaper than this
        # Entry confirmation / exhaustion
        self.SCALP_CONFIRM_MIN_SAMPLES = int(os.getenv("SCALP_CONFIRM_MIN_SAMPLES", "6"))
        self.SCALP_EXHAUST_TAIL_FRAC   = float(os.getenv("SCALP_EXHAUST_TAIL_FRAC", "0.65"))
        self.SCALP_MAX_MOVE_PTS        = float(os.getenv("SCALP_MAX_MOVE_PTS", "25.0"))  # exhaustion cap
        self.SCALP_REQUIRE_HTF_AGREE   = os.getenv("SCALP_REQUIRE_HTF_AGREE", "1") == "1"  # 5m ST must AGREE
        # Staged exit: BE lock at +BE_PTS, trailing activates at +TRAIL_START_PTS
        self.SCALP_BE_PTS             = float(os.getenv("SCALP_BE_PTS", "2.0"))      # breakeven lock at +2pt
        self.SCALP_TRAIL_START_PTS    = float(os.getenv("SCALP_TRAIL_START_PTS", "5.0"))  # WFO: 8→5, activate sooner
        self.SCALP_TRAIL_PTS          = float(os.getenv("SCALP_TRAIL_PTS", "4.0"))   # WFO: 3→4, room for winners
        self.SCALP_LOCK_PTS           = float(os.getenv("SCALP_LOCK_PTS", "2.0"))    # legacy alias (BE_PTS supersedes)
        # No-life exit: dead trade cut before the full stop
        self.SCALP_NO_LIFE_SECONDS    = int(os.getenv("SCALP_NO_LIFE_SECONDS", "35"))
        # Risk gates
        self.SCALP_ML_MIN_PROB        = float(os.getenv("SCALP_ML_MIN_PROB", "0.42"))  # ML conviction gate
        self.SCALP_MAX_CONSEC_LOSSES  = int(os.getenv("SCALP_MAX_CONSEC_LOSSES", "5"))   # circuit breaker
        self.SCALP_MAX_TRADES_PER_DAY = int(os.getenv("SCALP_MAX_TRADES_PER_DAY", "6"))  # scalp daily cap
        self.ML_INACTIVITY_MINUTES    = int(os.getenv("ML_INACTIVITY_MINUTES", "20"))    # SAFE_SCALP trigger
        # Execution-quality gates
        self.SPREAD_THRESHOLD_PTS     = float(os.getenv("SPREAD_THRESHOLD_PTS", "1.0"))   # skip if spread wider
        self.SLIPPAGE_THRESHOLD_PTS   = float(os.getenv("SLIPPAGE_THRESHOLD_PTS", "0.5")) # slippage-spike guard

        print(f"[CONFIG] Instrument={INDEX_NAME} token={INDEX_TOKEN} "
              f"lot={self.LOT_SIZE} strike_step={STRIKE_STEP} | "
              f"Capital={self.INITIAL_CAPITAL} | DRY_RUN={self.DRY_RUN}")