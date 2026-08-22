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
        self.MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", 8))

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

        # ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ TIER 1: Session & Regime Filters ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ
        # Warmup block: no entries until 90 min after market open (11:00)
        self.WARMUP_MINUTES = int(os.getenv("WARMUP_MINUTES", "90"))
        # Skip RANGE regime days (historically 31% WR, negative expectancy)
        self.SKIP_RANGE_REGIME = os.getenv("SKIP_RANGE_REGIME", "1") == "1"

        # Execution rules
        self.DEFAULT_SL_PCT = float(os.getenv("DEFAULT_SL_PCT", 0.10))
        self.DEFAULT_TARGET_PCT = float(os.getenv("DEFAULT_TARGET_PCT", 0.05))
        self.MAX_HOLD_SECONDS = int(os.getenv("MAX_HOLD_SECONDS", 300))

        # ── Phase-10 exit-tuning config (Task #19) ───────────────────────
        # Approved for Monday DRY_RUN: exit-grid backtest on 58 accepted
        # entries (scripts/backtest_exit_tuning.py) — baseline -Rs1,024 ->
        # recommended +Rs2,587. Fixed premium-space SL/TARGET replace the
        # ATR-derived stops at position entry; trailing = breakeven once
        # profit reaches ML_TRAIL_BE_PTS, then trail ML_TRAIL_GAP_PTS below
        # the high-water mark after ML_TRAIL_T2_PTS. MAX_HOLD stays 300s.
        self.ML_SL_PTS        = float(os.getenv("ML_SL_PTS", "3.0"))       # was ATR-derived 4-10 pts
        self.ML_TARGET_PTS    = float(os.getenv("ML_TARGET_PTS", "80.0"))  # was 3.5x SL distance
        # NO_LIFE exit (premium profit < floor at N seconds) — DISABLED by
        # Phase-10: grid shows it only hurts once trailing is active. The
        # live ML path has no NO_LIFE consumer (it was a backtest-only rule;
        # the scalp NO_LIFE below is a separate 35s rule, untouched).
        # Task #20 FIX 10: reserved — no live consumer; ML NO_LIFE is
        # disabled by absence, so setting this has no effect yet.
        self.ML_NO_LIFE_ENABLED = os.getenv("ML_NO_LIFE_ENABLED", "0") == "1"
        self.ML_TRAIL_ENABLED   = os.getenv("ML_TRAIL_ENABLED", "1") == "1"
        self.ML_TRAIL_BE_PTS    = float(os.getenv("ML_TRAIL_BE_PTS", "10.0"))  # profit pts -> stop to breakeven
        self.ML_TRAIL_T2_PTS    = float(os.getenv("ML_TRAIL_T2_PTS", "20.0"))  # profit pts -> trailing mode
        self.ML_TRAIL_GAP_PTS   = float(os.getenv("ML_TRAIL_GAP_PTS", "8.0"))  # stop = HWM - gap

        # Lot size: BANK NIFTY = 30 qty per lot (matches cost_model default
        # and the training data). Was wrongly set to NIFTY 65.
        self.LOT_SIZE = int(os.getenv("LOT_SIZE", 30))

        # ML
        self.CHAMPION_THRESHOLD = float(os.getenv("CHAMPION_THRESHOLD", 0.42))

        # ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ TIER 1: Entry Confirmation Gates ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ
        # Wider initial stop: 1.5x ATR instead of 1.0x (reduces noise stops)
        self.INITIAL_SL_MULT = float(os.getenv("INITIAL_SL_MULT", "1.5"))
        # Require VWAP alignment (price on correct side of VWAP)
        self.REQUIRE_VWAP_ALIGN = os.getenv("REQUIRE_VWAP_ALIGN", "1") == "1"
        # Require 5m SuperTrend alignment with trade direction
        self.REQUIRE_5M_TREND = os.getenv("REQUIRE_5M_TREND", "1") == "1"
        # Maximum slippage allowed at entry (points) ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ skip if spread > 1pt
        self.MAX_ENTRY_SLIP_PTS = float(os.getenv("MAX_ENTRY_SLIP_PTS", "1.0"))

        # ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ TIER 1: Entry Confirmation & Timing Gates ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ
        # Confirmation window seconds (wait for next candle or 5-15 sec)
        self.CONFIRMATION_WINDOW_SECONDS = int(os.getenv("CONFIRMATION_WINDOW_SECONDS", "10"))
        # Break + hold seconds (wait for price to hold above breakout level)
        self.BREAK_HOLD_SECONDS = int(os.getenv("BREAK_HOLD_SECONDS", "3"))
        # Micro-trend alignment candles (check last N candles for alignment)
        self.MICRO_TREND_CANDLES = int(os.getenv("MICRO_TREND_CANDLES", "3"))
        # Spread threshold in points (skip if spread > threshold)
        self.SPREAD_THRESHOLD_PTS = float(os.getenv("SPREAD_THRESHOLD_PTS", "1.0"))
        # Slippage threshold in points (skip if slippage spike detected)
        self.SLIPPAGE_THRESHOLD_PTS = float(os.getenv("SLIPPAGE_THRESHOLD_PTS", "0.5"))
        # Adaptive threshold increment per loss (increase ML threshold after losses)
        self.ADAPTIVE_THRESHOLD_INCREMENT_PER_LOSS = float(os.getenv("ADAPTIVE_THRESHOLD_INCREMENT_PER_LOSS", "0.02"))
        # Micro-trend alignment required flag
        self.MICRO_TREND_ALIGNMENT_REQUIRED = os.getenv("MICRO_TREND_ALIGNMENT_REQUIRED", "1") == "1"
        # Second brain strictness factor (multiply thresholds when ML prob dropping)
        self.SECOND_BRAIN_STRICTNESS_FACTOR = float(os.getenv("SECOND_BRAIN_STRICTNESS_FACTOR", "1.2"))

        # ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ TIER 2: Trailing & Scale-Out ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ
        # Trailing activates at +2pt profit (was immediate)
        self.TRAIL_ACTIVATION_PTS = float(os.getenv("TRAIL_ACTIVATION_PTS", "2.0"))
        # Trail distance: 2pt behind peak (was tighter)
        self.TRAIL_DISTANCE_PTS = float(os.getenv("TRAIL_DISTANCE_PTS", "2.0"))
        # Scale out 50% at +2pt profit
        self.SCALE_OUT_PCT = float(os.getenv("SCALE_OUT_PCT", "0.5"))
        self.SCALE_OUT_PTS = float(os.getenv("SCALE_OUT_PTS", "2.0"))

        # Scalping layer
        self.SCALP_ENABLED            = os.getenv("SCALP_ENABLED", "1") == "1"
        # Adaptive scalp SL (Aug-18): stop matches the system's movement
        # conviction. SCALP_SL_PTS = strict floor (no follow-through expected).
        # Higher conviction (strong move + HTF/VWAP agree + ML active) widens
        # the stop to SCALP_SL_MED_PTS / SCALP_SL_WIDE_PTS so real moves get
        # room instead of being stopped by noise in the first seconds.
        self.SCALP_SL_PTS             = float(os.getenv("SCALP_SL_PTS", "3.0"))
        self.SCALP_SL_MED_PTS         = float(os.getenv("SCALP_SL_MED_PTS", "5.0"))
        self.SCALP_SL_WIDE_PTS        = float(os.getenv("SCALP_SL_WIDE_PTS", "8.0"))
        # ATR-adaptive SL (Aug-20): replace fixed 3/5/8pt tiers with ATR-
        # relative multipliers so the stop scales with live volatility.
        # SL = max(ATR * mult, floor_pts) ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ the floor prevents micro-ATR
        # environments from producing impossibly tight stops.
        self.SCALP_ATR_SL_STRICT_MULT = float(os.getenv("SCALP_ATR_SL_STRICT_MULT", "0.20"))  # 20% of ATR
        self.SCALP_ATR_SL_MED_MULT    = float(os.getenv("SCALP_ATR_SL_MED_MULT", "0.35"))    # 35% of ATR
        self.SCALP_ATR_SL_WIDE_MULT   = float(os.getenv("SCALP_ATR_SL_WIDE_MULT", "0.55"))   # 55% of ATR
        # Open-volatility penalty (Aug-20): widen SL during the first N
        # seconds after ORB unlock (default 9:30).  The first 15 minutes
        # have 2-3x normal volatility and gamma; entries here need extra room.
        self.SCALP_OPEN_VOL_WINDOW_S  = int(os.getenv("SCALP_OPEN_VOL_WINDOW_S", "900"))   # 15 min
        self.SCALP_OPEN_VOL_SL_MULT   = float(os.getenv("SCALP_OPEN_VOL_SL_MULT", "1.5"))   # 1.5x wider
        self.SCALP_TARGET_PTS         = float(os.getenv("SCALP_TARGET_PTS", "50.0"))  # disabled ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ trailing SL is primary exit
        self.SCALP_MAX_HOLD_SECONDS   = int(os.getenv("SCALP_MAX_HOLD_SECONDS", "180"))
        # Entry quality (Aug-18): weak 12-13pt moves (barely above old 12pt
        # threshold) produced most of the losses; strong 18-35pt moves won.
        # Raised threshold to 20pt, require HTF agreement ALWAYS (was safe-mode
        # only), and lengthen cooldown so scalps wait for quality setups.
        self.SCALP_MOMENTUM_WINDOW    = int(os.getenv("SCALP_MOMENTUM_WINDOW", "30"))
        self.SCALP_MOMENTUM_THRESHOLD = float(os.getenv("SCALP_MOMENTUM_THRESHOLD", "20.0"))
        # Exhaustion filter (Aug-18): don't buy the TAIL of a fresh vertical
        # spike. A one-minute burst that closes at its extreme (11:50 +33pt,
        # 12:07 +20pt) reverses instantly; a real trend develops across the
        # window. If the last quarter of the window carries most of the total
        # move, the burst is still exploding -> skip. Also: never enter on
        # sparse data (<MIN_SAMPLES ticks) ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ confirmation must be mandatory.
        self.SCALP_CONFIRM_MIN_SAMPLES   = int(os.getenv("SCALP_CONFIRM_MIN_SAMPLES", "6"))
        self.SCALP_EXHAUST_TAIL_FRAC     = float(os.getenv("SCALP_EXHAUST_TAIL_FRAC", "0.65"))
        # No-life exit (Aug-18): if a scalp hasn't reached the breakeven zone
        # (+SCALP_BE_PTS) within this many seconds, the entry was wrong ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ cut
        # at market instead of bleeding the full stop. Data: today's losers
        # sat dead 59-81s then lost the full 8pt; every winner showed life in
        # 9-34s, so this never fires on live trades.
        self.SCALP_NO_LIFE_SECONDS     = int(os.getenv("SCALP_NO_LIFE_SECONDS", "35"))
        self.SCALP_COOLDOWN           = int(os.getenv("SCALP_COOLDOWN", "240"))  # WFO-optimized: 180->240: more patience avoids re-entry into reversals
        self.SCALP_REQUIRE_HTF_AGREE  = os.getenv("SCALP_REQUIRE_HTF_AGREE", "1") == "1"  # htf5 must AGREE, not merely not-oppose
        self.SCALP_LOTS               = int(os.getenv("SCALP_LOTS", "2"))
        # SAFE_SCALP: if the ML engine produces no signal for this many
        # minutes, scalp trades require STRICTER filters (HTF agreement,
        # tighter pullback band, higher momentum bar).
        self.ML_INACTIVITY_MINUTES    = int(os.getenv("ML_INACTIVITY_MINUTES", "20"))
        # Staged scalp profit management (Aug-18: fixed tiny-win/big-loss asymmetry).
        # Stage 1: initial SL only until +BE_PTS.
        # Stage 2: at +BE_PTS move SL to breakeven (entry+0.25) ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ a trade that is
        #          meaningfully in profit can never become a loss.
        # Stage 3: at +TRAIL_START_PTS trailing activates ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ SL trails
        #          SCALP_TRAIL_PTS below the peak, ratchet up only.
        self.SCALP_BE_PTS             = float(os.getenv("SCALP_BE_PTS", "5.0"))     # profit lock at +2.5pt (=+150 pnl with qty=60)
        self.SCALP_TRAIL_START_PTS    = float(os.getenv("SCALP_TRAIL_START_PTS", "6.0"))  # trail sooner: +3pt -> lock gains earlier
        self.SCALP_TRAIL_PTS          = float(os.getenv("SCALP_TRAIL_PTS", "3.5"))  # trail 2.5pt behind peak to lock profit
        self.SCALP_MIN_OPT_PTS        = float(os.getenv("SCALP_MIN_OPT_PTS", "30.0"))  # skip options cheaper than this
        # -- Scalp risk controls (Aug-18 loss analysis) ---------------------
        # Max NIFTY move (pts) allowed for scalp entry -- block entries after
        # 25pt move to avoid chasing exhaustion. Aug-18 data: entries at
        # 20-41pt moves all lost (27% WR -> 0% at >20pt moves).
        self.SCALP_MAX_MOVE_PTS       = float(os.getenv("SCALP_MAX_MOVE_PTS", "25.0"))
        # Max scalp trades per day -- prevents afternoon overtrading. Aug-18:
        # 6 of 11 trades happened in the last 30min (all losses).
        self.SCALP_MAX_TRADES_PER_DAY = int(os.getenv("SCALP_MAX_TRADES_PER_DAY", "6"))
        # Min ML probability required for scalp entry -- re-enable ML gating.
        # Aug-18: all 11 scalp trades had ml_prob=0.0 (no ML validation).
        self.SCALP_ML_MIN_PROB        = float(os.getenv("SCALP_ML_MIN_PROB", "0.42"))
        # Consecutive scalp loss circuit breaker -- stop after N losses.
        self.SCALP_MAX_CONSEC_LOSSES  = int(os.getenv("SCALP_MAX_CONSEC_LOSSES", "5"))  # WFO-optimized: 3->5: slightly looser to allow recovery trades

        print(f"[CONFIG] Capital={self.INITIAL_CAPITAL} | DRY_RUN={self.DRY_RUN} | LOT_SIZE={self.LOT_SIZE}")