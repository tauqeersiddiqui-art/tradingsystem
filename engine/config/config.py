import os


def _env_bool(name, default=False, env=None):
    if env is not None:
        value = env.get(name)
    else:
        value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name, default=0, env=None):
    if env is not None:
        value = env.get(name)
    else:
        value = os.getenv(name)
    try:
        return int(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def _env_float(name, default=0.0, env=None):
    if env is not None:
        value = env.get(name)
    else:
        value = os.getenv(name)
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


class Config:
    """
    Single source of truth for all trading-system configuration.

    Every value here mirrors the authoritative runtime defaults; production
    values MUST NOT be changed here.  The optional `env` dict (used only by
    tests) injects values without touching the real process environment.
    """
    _frozen = False

    def __init__(self, env=None):
        _get = env.get if env is not None else None

        def _str(name, default):
            return (_get(name) if _get is not None else os.getenv(name)) or default

        # Modes
        self.PAPER_MODE = _env_bool("PAPER_MODE", default=True, env=env)
        self.DRY_RUN = _env_bool("DRY_RUN", default=True, env=env)
        self.ALLOW_BROKER_POSITION_ON_START = _env_bool("ALLOW_BROKER_POSITION_ON_START", default=False, env=env)
        self.TEST_MODE = _env_bool("TEST_MODE", default=False, env=env)

        # Capital / risk
        self.INITIAL_CAPITAL = _env_float("INITIAL_CAPITAL", default=100000.0, env=env)
        self.RISK_PER_TRADE = _env_float("RISK_PER_TRADE", default=0.02, env=env)
        self.DAILY_LOSS_LIMIT = _env_float("DAILY_LOSS_LIMIT", default=-2000.0, env=env)
        self.DAILY_PROFIT_TARGET = _env_float("DAILY_PROFIT_TARGET", default=0.0, env=env)
        self.DAILY_PROFIT_LOCK_ENABLED = _env_bool("DAILY_PROFIT_LOCK_ENABLED", default=False, env=env)
        self.TARGET_EXIT_ENABLED = _env_bool("TARGET_EXIT_ENABLED", default=False, env=env)
        self.TRADE_LIVE_UPDATE_SECONDS = _env_float("TRADE_LIVE_UPDATE_SECONDS", default=2.0, env=env)
        self.TRAIL_ARM_PTS = _env_float("TRAIL_ARM_PTS", default=10.0, env=env)
        self.TRAIL_GAP_PTS = _env_float("TRAIL_GAP_PTS", default=5.0, env=env)
        # Session trade limit. Scalp + ML share this counter.
        self.MAX_TRADES_PER_DAY = _env_int("MAX_TRADES_PER_DAY", default=20, env=env)

        # Lot size: BANKNIFTY = 30 (target instrument)
        self.LOT_SIZE = _env_int("LOT_SIZE", default=30, env=env)

        # Round-trip cost per 30-qty BANKNIFTY lot (buy+sell). Drives the
        # cost-aware profit ladder so it never locks a profit below the trade's
        # own cost.
        self.COST_PER_LOT = _env_float("COST_PER_LOT", default=66.0, env=env)
        self.PROFIT_LOCK_SLIPPAGE_BUFFER_RS = _env_float("PROFIT_LOCK_SLIPPAGE_BUFFER_RS", default=0.0, env=env)
        self.PROFIT_LOCK_MIN_NET_PROFIT_RS = _env_float("PROFIT_LOCK_MIN_NET_PROFIT_RS", default=0.0, env=env)

        # Lunch chop filter (11:00-12:30). Kept OFF for dry-run testing.
        # Flip LUNCH_FILTER_ENABLED=1 when switching to real money.
        self.LUNCH_FILTER_ENABLED = _env_bool("LUNCH_FILTER_ENABLED", default=False, env=env)

        # Re-entry cooldown after any exit (seconds). Raised 180->300 so a
        # stopped option is not re-entered while it is still reversing
        # (this is what produced trade #3's -Rs481 on 2026-06-17).
        self.REENTRY_COOLDOWN = _env_int("REENTRY_COOLDOWN", default=300, env=env)
        self.SAME_SYMBOL_COOLDOWN = _env_int("SAME_SYMBOL_COOLDOWN", default=300, env=env)

        # Execution rules
        self.DEFAULT_SL_PCT = _env_float("DEFAULT_SL_PCT", default=0.10, env=env)
        self.DEFAULT_TARGET_PCT = _env_float("DEFAULT_TARGET_PCT", default=0.05, env=env)
        self.MAX_HOLD_SECONDS = _env_int("MAX_HOLD_SECONDS", default=300, env=env)

        # ML
        self.CHAMPION_THRESHOLD = _env_float("CHAMPION_THRESHOLD", default=0.42, env=env)
        self.ENABLE_PHASE55_FILTERS = _env_bool("ENABLE_PHASE55_FILTERS", default=False, env=env)
        self.ENABLE_PHASE55_CE_THRESHOLD = _env_bool("ENABLE_PHASE55_CE_THRESHOLD", default=True, env=env)
        self.ENABLE_PHASE55_PE_THRESHOLD = _env_bool("ENABLE_PHASE55_PE_THRESHOLD", default=True, env=env)
        # Mixed-regime CE block was anti-selective in shadow (blocked 65% winners,
        # 2026-07-06) — default OFF so it never blocks unnecessarily. Opt in via env.
        self.ENABLE_PHASE55_REGIME_FILTER = _env_bool("ENABLE_PHASE55_REGIME_FILTER", default=False, env=env)
        self.ENABLE_PHASE55_TELEMETRY = _env_bool("ENABLE_PHASE55_TELEMETRY", default=True, env=env)
        self.PHASE55_TELEMETRY_DIR = _str("PHASE55_TELEMETRY_DIR", "data/phase55")
        self.PHASE55_CE_QUALITY_THRESHOLD = _env_float("PHASE55_CE_QUALITY_THRESHOLD", default=0.4358, env=env)
        self.PHASE55_PE_DIRECTIONAL_THRESHOLD = _env_float("PHASE55_PE_DIRECTIONAL_THRESHOLD", default=0.4645, env=env)

        # Scalping layer
        self.SCALP_ENABLED            = _env_bool("SCALP_ENABLED", default=True, env=env)
        self.SCALP_SL_PTS             = _env_float("SCALP_SL_PTS", default=3.0, env=env)
        self.SCALP_TARGET_PTS         = _env_float("SCALP_TARGET_PTS", default=50.0, env=env)  # disabled — trailing SL is primary exit
        self.SCALP_TARGET_EXIT_ENABLED = _env_bool("SCALP_TARGET_EXIT_ENABLED", default=False, env=env)
        self.SCALP_MAX_HOLD_SECONDS   = _env_int("SCALP_MAX_HOLD_SECONDS", default=180, env=env)
        self.SCALP_MOMENTUM_WINDOW    = _env_int("SCALP_MOMENTUM_WINDOW", default=30, env=env)
        self.SCALP_MOMENTUM_THRESHOLD = _env_float("SCALP_MOMENTUM_THRESHOLD", default=12.0, env=env)
        self.SCALP_COOLDOWN           = _env_int("SCALP_COOLDOWN", default=60, env=env)
        self.SCALP_LOTS               = _env_int("SCALP_LOTS", default=2, env=env)
        self.SCALP_LOCK_PTS           = _env_float("SCALP_LOCK_PTS", default=10.0, env=env)  # first lock after +10pt
        self.SCALP_TRAIL_PTS          = _env_float("SCALP_TRAIL_PTS", default=5.0, env=env)  # trail SL 5pt below peak
        self.SCALP_STOP_MODIFY_MIN_STEP = _env_float("SCALP_STOP_MODIFY_MIN_STEP", default=1.0, env=env)
        self.SCALP_BANK_MFE_RS        = _env_float("SCALP_BANK_MFE_RS", default=0.0, env=env)
        self.SCALP_BANK_LOCK_PCT      = _env_float("SCALP_BANK_LOCK_PCT", default=0.70, env=env)
        self.SCALP_BANK_MIN_LOCK_RS   = _env_float("SCALP_BANK_MIN_LOCK_RS", default=30.0, env=env)
        self.SCALP_PROFIT_SLIPPAGE_BUFFER_RS = _env_float("SCALP_PROFIT_SLIPPAGE_BUFFER_RS", default=self.COST_PER_LOT, env=env)
        self.SCALP_MIN_NET_PROFIT_RS  = _env_float("SCALP_MIN_NET_PROFIT_RS", default=0.0, env=env)
        self.SCALP_MIN_TRAIL_MFE_COST_MULT = _env_float("SCALP_MIN_TRAIL_MFE_COST_MULT", default=3.0, env=env)
        self.SCALP_MIN_TRAIL_MFE_RS   = _env_float("SCALP_MIN_TRAIL_MFE_RS", default=0.0, env=env)
        self.SCALP_DAILY_PROFIT_TARGET = _env_float("SCALP_DAILY_PROFIT_TARGET", default=0.0, env=env)
        self.SCALP_MIN_OPT_PTS        = _env_float("SCALP_MIN_OPT_PTS", default=30.0, env=env)  # skip options cheaper than this
        # Max scalp trades per session — reserves remaining slots for ML engine
        self.SCALP_MAX_TRADES         = _env_int("SCALP_MAX_TRADES", default=10, env=env)
        # ML filter: skip scalp if opposite ML prob exceeds side's prob by this margin
        self.SCALP_ML_DISAGREE_MARGIN = _env_float("SCALP_ML_DISAGREE_MARGIN", default=0.12, env=env)
        # Min absolute ML prob for the trade side before scalp entry is allowed.
        # Scalp is an execution layer, so keep it close to the main AI floor.
        self.SCALP_ML_MIN_PROB        = _env_float("SCALP_ML_MIN_PROB", default=0.65, env=env)
        self.SCALP_MAIN_AI_THRESHOLD_CAP = _env_float("SCALP_MAIN_AI_THRESHOLD_CAP", default=0.65, env=env)
        # Min positive ML edge (side_prob - opp_prob) required for scalp entry.
        self.SCALP_ML_MIN_EDGE        = _env_float("SCALP_ML_MIN_EDGE", default=0.08, env=env)
        self.SCALP_USE_MAIN_AI_THRESHOLD = _env_bool("SCALP_USE_MAIN_AI_THRESHOLD", default=True, env=env)
        self.SCALP_REQUIRE_SUPERTREND_CONFIRM = _env_bool("SCALP_REQUIRE_SUPERTREND_CONFIRM", default=True, env=env)
        self.SCALP_REQUIRE_HTF5_CONFIRM = _env_bool("SCALP_REQUIRE_HTF5_CONFIRM", default=True, env=env)
        self.SCALP_REQUIRE_VWAP_CONFIRM = _env_bool("SCALP_REQUIRE_VWAP_CONFIRM", default=True, env=env)
        self.SCALP_VWAP_TOLERANCE       = _env_float("SCALP_VWAP_TOLERANCE", default=0.0015, env=env)
        # RANGE_DAY: require 30m HTF bullish before CE scalp (forensic: 3 CE scalp losses on flat days)
        self.SCALP_REQUIRE_HTF30_BULLISH = _env_bool("SCALP_REQUIRE_HTF30_BULLISH", default=True, env=env)
        # RANGE_DAY: require stronger spot move before scalp fires (reduces chop noise)
        self.SCALP_RANGE_MOM_THRESH   = _env_float("SCALP_RANGE_MOM_THRESH", default=15.0, env=env)

        # Decision-intelligence weighted scoring (new optional layer, fail-open)
        self.DECISION_ML_WEIGHT        = _env_float("DECISION_ML_WEIGHT", default=0.5, env=env)
        self.DECISION_ORB_WEIGHT       = _env_float("DECISION_ORB_WEIGHT", default=0.2, env=env)
        self.DECISION_GLOBAL_WEIGHT    = _env_float("DECISION_GLOBAL_WEIGHT", default=0.2, env=env)
        self.DECISION_VOLATILITY_WEIGHT = _env_float("DECISION_VOLATILITY_WEIGHT", default=0.1, env=env)

        # ORB reconstruction fault-tolerance (Phase 7)
        self.ORB_RECONSTRUCT_RETRIES = _env_int("ORB_RECONSTRUCT_RETRIES", default=3, env=env)
        self.ORB_RECONSTRUCT_BACKOFF = _env_float("ORB_RECONSTRUCT_BACKOFF", default=5.0, env=env)
        self.ORB_MIN_CANDLES         = _env_int("ORB_MIN_CANDLES", default=10, env=env)

        # Object is now frozen; prevent further attribute changes.
        object.__setattr__(self, "_frozen", True)

        # Validate configuration
        self._validate()

        # Log the (non-secret) startup summary once
        print(f"[CONFIG] Capital={self.INITIAL_CAPITAL} | DRY_RUN={self.DRY_RUN} | LOT_SIZE={self.LOT_SIZE}")

    def _validate(self):
        """Validate configuration values. Fail fast on invalid config."""
        errors = []

        if self.LOT_SIZE <= 0:
            errors.append("LOT_SIZE must be > 0")

        target = self.DAILY_PROFIT_TARGET
        if target < 0:
            errors.append("DAILY_PROFIT_TARGET must be >= 0")

        if self.COST_PER_LOT < 0:
            errors.append("COST_PER_LOT must be >= 0")

        if self.DAILY_LOSS_LIMIT >= 0:
            errors.append("DAILY_LOSS_LIMIT must be < 0")

        if self.MAX_TRADES_PER_DAY <= 0:
            errors.append("MAX_TRADES_PER_DAY must be > 0")

        if self.LOT_SIZE <= 0:
            errors.append("LOT_SIZE must be > 0")

        if self.SCALP_LOTS <= 0:
            errors.append("SCALP_LOTS must be > 0")

        if self.SCALP_MAX_TRADES < 0:
            errors.append("SCALP_MAX_TRADES must be >= 0")

        if self.REENTRY_COOLDOWN < 0 or self.SAME_SYMBOL_COOLDOWN < 0:
            errors.append("REENTRY_COOLDOWN / SAME_SYMBOL_COOLDOWN must be >= 0")

        if self.MAX_HOLD_SECONDS <= 0:
            errors.append("MAX_HOLD_SECONDS must be > 0")

        if self.RISK_PER_TRADE <= 0 or self.RISK_PER_TRADE >= 1:
            errors.append("RISK_PER_TRADE must be in (0, 1)")

        if self.SCALP_SL_PTS < 0:
            errors.append("SCALP_SL_PTS must be >= 0")

        if self.SCALP_TARGET_PTS < 0:
            errors.append("SCALP_TARGET_PTS must be >= 0")

        if self.ORB_RECONSTRUCT_RETRIES < 0:
            errors.append("ORB_RECONSTRUCT_RETRIES must be >= 0")

        if self.ORB_RECONSTRUCT_BACKOFF < 0:
            errors.append("ORB_RECONSTRUCT_BACKOFF must be >= 0")

        if self.ORB_MIN_CANDLES <= 0:
            errors.append("ORB_MIN_CANDLES must be > 0")

        if errors:
            raise ValueError("Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False) and not name.startswith("_"):
            raise AttributeError(f"Cannot modify attribute '{name}' after Config initialization. Configuration is immutable.")
        super().__setattr__(name, value)
