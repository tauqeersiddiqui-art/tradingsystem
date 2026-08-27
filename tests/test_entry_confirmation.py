"""
Unit tests for master_runner.should_confirm_entry — the refined
entry-confirmation gates:

  A. Structure confirmation over the past/recent window split (a full
     give-back of the past window = CONFIRM_STRUCT_BREAK)
  B. Anti-chasing (price pinned at the extreme = CONFIRM_CHASING_SPIKE)
  B2. Momentum (last tick must still push the direction)
  C. HTF rule (5m SuperTrend: opposing blocks; neutral (0) is ALLOWED)
  D. Trap filters (ORB snap-back, >85% give-back)

Each test feeds a synthetic 1s NIFTY-spot tick history and asserts the
gate blocks or confirms as designed.
"""

import sys
import os
from collections import deque
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import master_runner as m


class _FakeLE:
    def __init__(self, htf5=1, orb_done=False, orb_high=None, orb_low=None):
        self._htf5_dir = htf5
        self.orb_done = orb_done
        self.orb_high = orb_high
        self.orb_low = orb_low


class _FakeCtx:
    def __init__(self, le):
        self.live_engine = le


def _hist(prices):
    t0 = datetime(2026, 1, 1, 10, 0)
    return deque((t0 + timedelta(seconds=i), float(p)) for i, p in enumerate(prices))


def _confirm(side, prices, htf5=1, orb_done=False, orb_high=None, orb_low=None):
    le = _FakeLE(htf5, orb_done, orb_high, orb_low)
    return m.should_confirm_entry({"side": side}, datetime.now(), _hist(prices), _FakeCtx(le))


# ── helpers: 40-tick past window + 20-tick recent window ───────────────
_CE_PAST = [100.0 + i * 0.01 for i in range(40)]      # 100.00..100.39 (h1=100.39, l1=100.00)
_PE_PAST = [100.0 - i * 0.01 for i in range(40)]      # 100.00..99.61  (h1=100.00, l1=99.61)


def _ce_pullback_confirm():
    """Past: low range. Recent: HH 101.0 then small pullback, rising tail."""
    recent = [100.50, 100.55, 100.60, 100.65, 100.70, 100.75, 100.80, 100.85,
              100.90, 100.95, 101.00, 100.98, 100.94, 100.90, 100.88, 100.86,
              100.88, 100.90, 100.92, 100.94]
    return _CE_PAST + recent


def _pe_pullback_confirm():
    """Past: high range. Recent: LL 99.0 then small pullback, falling tail."""
    recent = [99.50, 99.45, 99.40, 99.35, 99.30, 99.25, 99.20, 99.15,
              99.10, 99.05, 99.00, 99.02, 99.06, 99.10, 99.12, 99.14,
              99.12, 99.10, 99.08, 99.06]
    return _PE_PAST + recent


# ══════════════════════════════════════════════════════════════════════
# A. STRUCTURE CONFIRMATION
# ══════════════════════════════════════════════════════════════════════
def test_ce_confirm_on_higher_high():
    ok, reason = _confirm("CE", _ce_pullback_confirm())
    assert ok is True and reason == "CONFIRMED"


def test_pe_confirm_on_lower_low():
    ok, reason = _confirm("PE", _pe_pullback_confirm(), htf5=-1)
    assert ok is True and reason == "CONFIRMED"


def test_ce_block_stalled_move_give_back():
    """Stalled move that ends at the bottom of its own recent range: the old
    NO_HH gate no longer exists — under the current relaxed gate order this
    shape is rejected as a >85% give-back (CONFIRM_SPIKE_TRAP)."""
    recent = [100.20, 100.25, 100.20, 100.25, 100.20] * 4
    ok, reason = _confirm("CE", _CE_PAST + recent)
    assert ok is False and reason == "CONFIRM_SPIKE_TRAP"


def test_pe_block_pinned_at_extreme():
    """Price pinned at the low of the recent window (pullback = 0%): the old
    NO_LL gate no longer exists — the anti-chasing gate fires first."""
    recent = [99.75, 99.70, 99.75, 99.70] * 5
    ok, reason = _confirm("PE", _PE_PAST + recent, htf5=-1)
    assert ok is False and reason == "CONFIRM_CHASING_SPIKE"


def test_ce_block_structure_break():
    """HH then reversal below the past-window low = full give-back."""
    recent = [100.50, 100.60, 100.70, 100.80, 100.90, 101.00, 100.90, 100.70,
              100.50, 100.30, 100.10, 99.95, 99.90, 99.85, 99.80, 99.75,
              99.70, 99.65, 99.60, 99.55]
    ok, reason = _confirm("CE", _CE_PAST + recent)
    assert ok is False and reason == "CONFIRM_STRUCT_BREAK"


# ══════════════════════════════════════════════════════════════════════
# B. DYNAMIC PULLBACK BAND (10-50% of range)
# ══════════════════════════════════════════════════════════════════════
def test_ce_block_chasing_at_top():
    """Price pinned at the extreme = pullback 0% = chasing the spike."""
    recent = [100.5, 100.6, 100.7, 100.8, 100.9, 101.0] * 3 + [101.0] * 2
    ok, reason = _confirm("CE", _CE_PAST + recent)
    assert ok is False and reason == "CONFIRM_CHASING_SPIKE"


def test_pe_block_chasing_at_bottom():
    recent = [99.5, 99.4, 99.3, 99.2, 99.1, 99.0] * 3 + [99.0] * 2
    ok, reason = _confirm("PE", _PE_PAST + recent, htf5=-1)
    assert ok is False and reason == "CONFIRM_CHASING_SPIKE"


def test_ce_block_deep_pullback():
    """Gave back the whole move: the current relaxed gate set has no separate
    deep-pullback reason — collapsing below the past-window low is rejected
    as CONFIRM_STRUCT_BREAK (gate A fires before the pullback gate)."""
    recent = [100.50, 100.60, 100.70, 100.80, 100.90, 101.00, 100.85, 100.70,
              100.55, 100.45, 100.40, 100.38, 100.36, 100.34, 100.33, 100.32,
              100.31, 100.31, 100.30, 100.30]
    ok, reason = _confirm("CE", _CE_PAST + recent)
    assert ok is False and reason == "CONFIRM_STRUCT_BREAK"


# ══════════════════════════════════════════════════════════════════════
# B2. MOMENTUM CONFIRMATION (last 5 ticks)
# ══════════════════════════════════════════════════════════════════════
def test_ce_block_no_momentum():
    """Valid structure + pullback but the final tick stops pushing: the
    current soft gate compares the last tick against the tick 3 back."""
    recent = [100.50, 100.60, 100.70, 100.80, 100.90, 101.00, 100.98, 100.95,
              100.92, 100.90, 100.88, 100.86, 100.88, 100.90, 100.92, 100.94,
              100.96, 100.95, 100.93, 100.92]
    ok, reason = _confirm("CE", _CE_PAST + recent)
    assert ok is False and reason == "CONFIRM_NO_MOMENTUM"


def test_pe_block_no_momentum():
    recent = [99.50, 99.40, 99.30, 99.20, 99.10, 99.00, 99.02, 99.05,
              99.08, 99.10, 99.12, 99.14, 99.12, 99.10, 99.08, 99.06,
              99.04, 99.05, 99.07, 99.08]
    ok, reason = _confirm("PE", _PE_PAST + recent, htf5=-1)
    assert ok is False and reason == "CONFIRM_NO_MOMENTUM"


# ══════════════════════════════════════════════════════════════════════
# C. HTF RULE
# ══════════════════════════════════════════════════════════════════════
def test_ce_block_htf_opposes():
    ok, reason = _confirm("CE", _ce_pullback_confirm(), htf5=-1)
    assert ok is False and reason == "CONFIRM_HTF_OPPOSES"


def test_pe_block_htf_opposes():
    ok, reason = _confirm("PE", _pe_pullback_confirm(), htf5=1)
    assert ok is False and reason == "CONFIRM_HTF_OPPOSES"


def test_htf_neutral_allowed():
    """CURRENT intended behavior: htf5 == 0 (no 5m trend) does NOT block —
    only an OPPOSING HTF vetoes entry (gate C rejects htf5 == -1 for CE /
    +1 for PE only). NOTE: this is a behavior decision worth revisiting."""
    ok, reason = _confirm("CE", _ce_pullback_confirm(), htf5=0)
    assert ok is True and reason == "CONFIRMED"


# ══════════════════════════════════════════════════════════════════════
# D. TRAP FILTER
# ══════════════════════════════════════════════════════════════════════
def test_ce_block_breakout_trap():
    """Broke ORB high then snapped back below it = failed breakout."""
    past = [99.50 + i * 0.01 for i in range(40)]        # 99.50..99.89
    recent = [100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.7,
              100.8, 100.9, 101.0, 100.8, 100.6, 100.55, 100.52, 100.50,
              100.52, 100.54, 100.56, 100.58]
    ok, reason = _confirm("CE", past + recent,
                          orb_done=True, orb_high=100.6, orb_low=99.0)
    assert ok is False and reason == "CONFIRM_BREAKOUT_TRAP"


def test_pe_block_breakout_trap():
    """Broke ORB low then snapped back above it = failed breakout."""
    past = [100.50 - i * 0.01 for i in range(40)]       # 100.50..99.61
    recent = [100.0, 99.9, 99.8, 99.7, 99.6, 99.5, 99.4, 99.3,
              99.2, 99.1, 99.0, 99.2, 99.4, 99.45, 99.48, 99.50,
              99.48, 99.46, 99.44, 99.42]
    ok, reason = _confirm("PE", past + recent, htf5=-1,
                          orb_done=True, orb_high=100.0, orb_low=99.4)
    assert ok is False and reason == "CONFIRM_BREAKOUT_TRAP"


# ══════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════
def test_no_history_blocks():
    ok, reason = m.should_confirm_entry({"side": "CE"}, datetime.now(),
                                        deque(), _FakeCtx(_FakeLE()))
    assert ok is False and reason == "CONFIRM_NO_HISTORY"


def test_short_history_blocks():
    # Gate is len(prices) < 10, so feed 9 ticks to exercise it.
    ok, reason = _confirm("CE", [100.0] * 9)
    assert ok is False and reason == "CONFIRM_NO_HISTORY"


# ══════════════════════════════════════════════════════════════════════
# SAFE_SCALP mode tests (ML engine silent -> stricter scalp filters)
# ══════════════════════════════════════════════════════════════════════

from engine.scalping.scalp_engine import ScalpEngine


class _ScalpCfg:
    SCALP_SL_PTS = 3.0
    SCALP_SL_MED_PTS = 5.0
    SCALP_SL_WIDE_PTS = 8.0
    SCALP_TARGET_PTS = 50.0
    SCALP_MAX_HOLD_SECONDS = 180
    SCALP_MOMENTUM_WINDOW = 30
    SCALP_MOMENTUM_THRESHOLD = 12.0
    SCALP_COOLDOWN = 60
    SCALP_REQUIRE_HTF_AGREE = False  # tests keep old "neutral allowed" semantics
    SCALP_CONFIRM_MIN_SAMPLES = 6
    SCALP_EXHAUST_TAIL_FRAC = 0.65
    SCALP_NO_LIFE_SECONDS = 35
    SCALP_BE_PTS = 2.0
    # Cost-model inputs: ScalpEngine.__init__ computes
    # round_trip_cost(LOT_SIZE, config) for the EQ NOT_PROFITABLE rule
    # (cost_model.lot_qty reads config.LOT_SIZE directly). Production
    # defaults from engine.config.Config.
    LOT_SIZE = 30          # BANKNIFTY qty per lot
    COST_PER_LOT = 66.0    # Rs round-trip per lot


def _scalp_hist(prices):
    t0 = datetime(2026, 1, 1, 10, 0)
    return deque((t0 + timedelta(seconds=i), float(p)) for i, p in enumerate(prices))


# CE acceptance series must also survive the rejection-first ENTRY QUALITY
# gate (engine.execution.filters.compute_entry_quality) that check_entry now
# runs last: realistic ~25000 base keeps move_pct under EQ_MOVE_PCT_MAX and
# the ~12pt chop per synthetic bar keeps the NOT_PROFITABLE cost check happy.
# Net +22pt move with HH structure; ltp 25022.0 = 27.8% pullback off h2.
_CE_VALID = [25000.0, 25008.0, 25012.0, 25003.0,
             25003.0, 25011.0, 25015.0, 25006.0,
             25006.0, 25014.0, 25018.0, 25009.0,
             25009.0, 25017.0, 25021.0, 25012.0,
             25012.0, 25020.0, 25024.0, 25015.0,
             25015.0, 25023.0, 25027.0, 25024.0]


def test_normal_scalp_htf_agreement_required_by_default():
    """Default (SCALP_REQUIRE_HTF_AGREE=1): HTF must AGREE even outside safe mode."""
    cfg = _ScalpCfg()
    cfg.SCALP_REQUIRE_HTF_AGREE = True
    e = ScalpEngine(cfg)
    assert e.check_entry(25022.0, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False, vwap_confirms=True) is not None
    assert e.check_entry(25022.0, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=0, safe_mode=False, vwap_confirms=True) is None
    assert e.check_entry(25022.0, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=-1, safe_mode=False, vwap_confirms=True) is None


def test_safe_scalp_htf_agreement_required():
    """SAFE_SCALP: HTF must AGREE (+1 for CE); neutral (0) and opposing (-1) block."""
    e = ScalpEngine(_ScalpCfg())
    assert e.check_entry(25022.0, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=True, vwap_confirms=True) is not None
    assert e.check_entry(25022.0, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=0, safe_mode=True, vwap_confirms=True) is None
    assert e.check_entry(25022.0, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=-1, safe_mode=True, vwap_confirms=True) is None


def test_normal_scalp_htf_neutral_allowed():
    """Normal scalp with SCALP_REQUIRE_HTF_AGREE=0: HTF neutral (0) is allowed
    — only opposing blocks."""
    e = ScalpEngine(_ScalpCfg())
    assert e.check_entry(25022.0, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=0, safe_mode=False, vwap_confirms=True) is not None


def test_adaptive_sl_by_conviction():
    """SL widens with movement conviction: strict on weak/no-support, wide on
    strong+aligned. Returns (pts, tier) since the ATR-adaptive upgrade."""
    e = ScalpEngine(_ScalpCfg())
    # Weak move, HTF opposes, no VWAP, ML silent -> STRICT 3pt
    assert e.adaptive_sl_pts("CE", 12.0, htf5=-1, vwap_confirms=False, ml_active=False) == (3.0, "STRICT")
    # Strong move + ML + VWAP -> MED 5pt
    assert e.adaptive_sl_pts("CE", 25.0, htf5=0, vwap_confirms=True, ml_active=True) == (5.0, "MED")
    # Strong move + HTF agrees + VWAP + ML -> WIDE 8pt
    assert e.adaptive_sl_pts("PE", -35.0, htf5=-1, vwap_confirms=True, ml_active=True) == (8.0, "WIDE")
    # HTF agrees but weak move -> MED
    assert e.adaptive_sl_pts("CE", 15.0, htf5=1, vwap_confirms=True, ml_active=True) == (5.0, "MED")


def test_check_exit_honors_position_stop():
    """check_exit must use scalp_pos['stop_loss'] (adaptive), not hardcoded 3pt."""
    e = ScalpEngine(_ScalpCfg())
    t0 = datetime(2026, 1, 1, 10, 0)
    pos = {"entry": 100.0, "stop_loss": 92.0, "entry_ts": t0, "target": 0.0}
    # LTP above the wide stop (95) but below entry-3 -> must NOT stop (would have
    # with the old hardcoded 3pt logic).
    assert e.check_exit(pos, 95.0, t0) == (False, "")
    # LTP at/below the active stop -> STOP
    assert e.check_exit(pos, 92.0, t0) == (True, "STOP")


def test_safe_scalp_higher_momentum_bar():
    """SAFE_SCALP raises the momentum bar 25% (12pt -> 15pt)."""
    e = ScalpEngine(_ScalpCfg())
    # Net +13.5pt move with HH structure: fires in normal mode (>=12) but is
    # blocked in safe mode (13.5 < 15 raised bar).
    _small = [25000.0, 25008.0, 25012.0, 25001.5,
              25001.5, 25009.5, 25013.5, 25003.0,
              25003.0, 25011.0, 25015.0, 25004.5,
              25004.5, 25012.5, 25016.5, 25006.0,
              25006.0, 25014.0, 25018.0, 25007.5,
              25007.5, 25015.5, 25019.5, 25013.5]
    # Net +20pt move passes the safe bar (>=15).
    _big = [25000.0, 25008.0, 25012.0, 25002.9,
            25002.9, 25010.9, 25014.9, 25005.8,
            25005.8, 25013.8, 25017.8, 25008.7,
            25008.7, 25016.7, 25020.7, 25011.6,
            25011.6, 25019.6, 25023.6, 25014.5,
            25010.0, 25025.0, 25008.0, 25017.6]
    assert e.check_entry(25013.5, _scalp_hist(_small), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False, vwap_confirms=True) is not None
    assert e.check_entry(25013.5, _scalp_hist(_small), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=True, vwap_confirms=True) is None
    # 20pt move passes the safe bar
    assert e.check_entry(25020.0, _scalp_hist(_big), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=True, vwap_confirms=True) is not None


# ══════════════════════════════════════════════════════════════════════
# Exhaustion filter (Aug-18): no buying the tail of a fresh vertical spike
# ══════════════════════════════════════════════════════════════════════


def test_exhaustion_blocks_vertical_spike_tail():
    """A move concentrated in the last few seconds of the window (fresh
    vertical spike, e.g. 11:50 +33pt one-minute candle on Aug-18) must be
    skipped: the tail carries >65% of the total move."""
    e = ScalpEngine(_ScalpCfg())
    # 15s flat, then a +24pt explosion in 3s; the small retrace keeps the
    # pullback band satisfied so the EXHAUSTION gate is the one that fires.
    spike = [25000.0] * 15 + [25012.0, 25022.0, 25024.0]
    assert e.check_entry(25020.0, _scalp_hist(spike), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False) is None


def test_exhaustion_allows_sustained_move():
    """A move that develops across the whole window (real trend, e.g. the
    10:51 -28.5pt winner that kept going) still qualifies: the tail carries
    only a small share of the total move."""
    e = ScalpEngine(_ScalpCfg())
    assert e.check_entry(25022.0, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False, vwap_confirms=True) is not None


def test_sparse_window_blocks_confirmation():
    """With fewer than SCALP_CONFIRM_MIN_SAMPLES ticks, entry is blocked —
    confirmation is mandatory (closes the old '<6 samples skips all checks' hole)."""
    e = ScalpEngine(_ScalpCfg())
    sparse = [25000.0, 25004.0, 25010.0, 25018.0]  # only 4 ticks, big move
    assert e.check_entry(25018.0, _scalp_hist(sparse), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False, vwap_confirms=True) is None


# ══════════════════════════════════════════════════════════════════════
# No-life exit (Aug-18): cut dead trades at market instead of full stop
# ══════════════════════════════════════════════════════════════════════


def test_no_life_exit_cuts_dead_trade():
    """A scalp that never reached +BE_PTS after NO_LIFE seconds exits at market
    (instead of bleeding the full 8pt stop)."""
    e = ScalpEngine(_ScalpCfg())
    pos = {"entry": 350.0, "stop_loss": 342.0, "target": 400.0,
           "entry_ts": datetime(2026, 1, 1, 10, 0), "be_triggered": False}
    t = datetime(2026, 1, 1, 10, 0, 40)  # 40s held, no life
    should, reason = e.check_exit(pos, 348.5, t)
    assert should is True and reason == "NO_LIFE"


def test_no_life_not_fired_within_window():
    """Before NO_LIFE seconds, the trade keeps its room (no premature cut)."""
    e = ScalpEngine(_ScalpCfg())
    pos = {"entry": 350.0, "stop_loss": 342.0, "target": 400.0,
           "entry_ts": datetime(2026, 1, 1, 10, 0), "be_triggered": False}
    t = datetime(2026, 1, 1, 10, 0, 20)  # 20s held
    should, reason = e.check_exit(pos, 348.5, t)
    assert should is False


def test_no_life_not_fired_when_live():
    """A trade that reached the breakeven zone (be_triggered=True) is alive —
    NO_LIFE must never fire on it, even after 35s."""
    e = ScalpEngine(_ScalpCfg())
    pos = {"entry": 350.0, "stop_loss": 352.25, "target": 400.0,
           "entry_ts": datetime(2026, 1, 1, 10, 0), "be_triggered": True}
    t = datetime(2026, 1, 1, 10, 1, 0)  # 60s held but showed life
    should, reason = e.check_exit(pos, 352.5, t)  # above stop, alive
    assert should is False
