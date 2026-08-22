"""
Unit tests for master_runner.should_confirm_entry — the refined
entry-confirmation gates:

  A. Structure confirmation (HH/LL) over 40s-past vs 20s-recent windows
  B. Dynamic pullback band (10-50% of range, scales with volatility)
  B2. Momentum (last 5 ticks must push the direction)
  C. HTF rule (5m SuperTrend agree; neutral blocks)
  D. Trap filters (ORB snap-back, deep give-back, micro reversal)

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


def test_ce_block_no_higher_high():
    """Recent high below past high = no continuation."""
    recent = [100.20, 100.25, 100.20, 100.25, 100.20] * 4
    ok, reason = _confirm("CE", _CE_PAST + recent)
    assert ok is False and reason == "CONFIRM_NO_HH"


def test_pe_block_no_lower_low():
    recent = [99.75, 99.70, 99.75, 99.70] * 5
    ok, reason = _confirm("PE", _PE_PAST + recent, htf5=-1)
    assert ok is False and reason == "CONFIRM_NO_LL"


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
    """Price pinned at the extreme = pullback 0% = bad pullback."""
    recent = [100.5, 100.6, 100.7, 100.8, 100.9, 101.0] * 3 + [101.0] * 2
    ok, reason = _confirm("CE", _CE_PAST + recent)
    assert ok is False and reason == "CONFIRM_BAD_PULLBACK"


def test_pe_block_chasing_at_bottom():
    recent = [99.5, 99.4, 99.3, 99.2, 99.1, 99.0] * 3 + [99.0] * 2
    ok, reason = _confirm("PE", _PE_PAST + recent, htf5=-1)
    assert ok is False and reason == "CONFIRM_BAD_PULLBACK"


def test_ce_block_deep_pullback():
    """Gave back >50% of the range = pullback failed, not a retrace."""
    # NOTE: past window = prelude[20:40] -> l1 = 100.20, so the collapse
    # must stay above 100.20 to test BAD_PULLBACK and not STRUCT_BREAK.
    recent = [100.50, 100.60, 100.70, 100.80, 100.90, 101.00, 100.85, 100.70,
              100.55, 100.45, 100.40, 100.38, 100.36, 100.34, 100.33, 100.32,
              100.31, 100.31, 100.30, 100.30]
    ok, reason = _confirm("CE", _CE_PAST + recent)
    assert ok is False and reason == "CONFIRM_BAD_PULLBACK"


# ══════════════════════════════════════════════════════════════════════
# B2. MOMENTUM CONFIRMATION (last 5 ticks)
# ══════════════════════════════════════════════════════════════════════
def test_ce_block_no_momentum():
    """Valid HH + pullback but last ticks are choppy = no momentum."""
    recent = [100.50, 100.60, 100.70, 100.80, 100.90, 101.00, 100.98, 100.95,
              100.92, 100.90, 100.88, 100.86, 100.88, 100.90, 100.92, 100.94,
              100.90, 100.88, 100.90, 100.90]
    ok, reason = _confirm("CE", _CE_PAST + recent)
    assert ok is False and reason == "CONFIRM_NO_MOMENTUM"


def test_pe_block_no_momentum():
    recent = [99.50, 99.40, 99.30, 99.20, 99.10, 99.00, 99.02, 99.05,
              99.08, 99.10, 99.12, 99.14, 99.10, 99.12, 99.14, 99.06,
              99.10, 99.12, 99.10, 99.10]
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


def test_htf_neutral_blocks():
    """htf5 == 0 = no 5m trend confirmation = block (no trade on no trend)."""
    ok, reason = _confirm("CE", _ce_pullback_confirm(), htf5=0)
    assert ok is False and reason == "CONFIRM_HTF_NEUTRAL"


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
    ok, reason = _confirm("CE", [100.0] * 10)
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


def _scalp_hist(prices):
    t0 = datetime(2026, 1, 1, 10, 0)
    return deque((t0 + timedelta(seconds=i), float(p)) for i, p in enumerate(prices))


# CE series: 15.2pt move, HH structure, cur retraced 1.3pt from h2=116.5
_CE_VALID = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0,
             110.0, 111.0, 112.0, 113.0, 114.0, 115.0, 116.5, 116.2, 116.0, 115.4,
             115.2]


def test_normal_scalp_htf_agreement_required_by_default():
    """Default (SCALP_REQUIRE_HTF_AGREE=1): HTF must AGREE even outside safe mode."""
    cfg = _ScalpCfg()
    cfg.SCALP_REQUIRE_HTF_AGREE = True
    e = ScalpEngine(cfg)
    assert e.check_entry(115.2, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False) is not None
    assert e.check_entry(115.2, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=0, safe_mode=False) is None
    assert e.check_entry(115.2, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=-1, safe_mode=False) is None


def test_safe_scalp_htf_agreement_required():
    """SAFE_SCALP: HTF must AGREE (+1 for CE); neutral (0) and opposing (-1) block."""
    e = ScalpEngine(_ScalpCfg())
    assert e.check_entry(115.2, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=True) is not None
    assert e.check_entry(115.2, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=0, safe_mode=True) is None
    assert e.check_entry(115.2, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=-1, safe_mode=True) is None


def test_normal_scalp_htf_neutral_allowed():
    """Normal scalp: HTF neutral (0) is allowed — only opposing blocks."""
    e = ScalpEngine(_ScalpCfg())
    assert e.check_entry(115.2, _scalp_hist(_CE_VALID), datetime(2026, 1, 1, 10, 0),
                         htf5=0, safe_mode=False) is not None


def test_adaptive_sl_by_conviction():
    """SL widens with movement conviction: strict on weak/no-support, wide on strong+aligned."""
    e = ScalpEngine(_ScalpCfg())
    # Weak move, HTF opposes, no VWAP, ML silent -> STRICT 3pt
    assert e.adaptive_sl_pts("CE", 12.0, htf5=-1, vwap_confirms=False, ml_active=False) == 3.0
    # Strong move + ML + VWAP -> MED 5pt
    assert e.adaptive_sl_pts("CE", 25.0, htf5=0, vwap_confirms=True, ml_active=True) == 5.0
    # Strong move + HTF agrees + VWAP + ML -> WIDE 8pt
    assert e.adaptive_sl_pts("PE", -35.0, htf5=-1, vwap_confirms=True, ml_active=True) == 8.0
    # HTF agrees but weak move -> MED
    assert e.adaptive_sl_pts("CE", 15.0, htf5=1, vwap_confirms=True, ml_active=True) == 5.0


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
    """SAFE_SCALP raises the momentum bar 25% (12pt -> 14pt)."""
    e = ScalpEngine(_ScalpCfg())
    _big = [100.0, 101.2, 102.4, 103.6, 104.8, 106.0, 107.2, 108.4, 109.6, 110.8,
            112.0, 113.2, 114.4, 115.6, 116.8, 118.0, 119.2, 118.8, 118.4, 118.0,
            117.6]  # move 17.6pt
    # 12.2pt move with 45% pullback: fires in normal mode (>=12, band 10-50%)
    # but blocked in safe mode (move<14 AND 45% > 40% tight band)
    _small = [102.0, 103.4, 104.8, 106.2, 107.6, 109.0, 110.4, 111.8, 113.2, 114.6,
              112.0, 113.0, 114.0, 115.0, 116.0, 115.6, 115.2, 114.8, 114.6, 114.4,
              114.2]
    assert e.check_entry(114.2, _scalp_hist(_small), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False) is not None
    assert e.check_entry(114.2, _scalp_hist(_small), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=True) is None
    # 17.6pt move passes the safe bar
    assert e.check_entry(117.6, _scalp_hist(_big), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=True) is not None


# ══════════════════════════════════════════════════════════════════════
# Exhaustion filter (Aug-18): no buying the tail of a fresh vertical spike
# ══════════════════════════════════════════════════════════════════════


def test_exhaustion_blocks_vertical_spike_tail():
    """A move concentrated in the last quarter of the window (fresh vertical
    spike, e.g. 11:50 +33pt one-minute candle on Aug-18) must be skipped."""
    e = ScalpEngine(_ScalpCfg())
    # 14s flat, then a +24pt explosion in the last 4s -> tail carries it all
    spike = ([100.0] * 14 + [104.0, 110.0, 118.0, 124.0])
    assert e.check_entry(124.0, _scalp_hist(spike), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False) is None


def test_exhaustion_allows_sustained_move():
    """A move that develops across the whole window (real trend, e.g. the
    10:51 -28.5pt winner that kept going) still qualifies."""
    e = ScalpEngine(_ScalpCfg())
    # steady climb 100 -> 124 spread over 17s, then a small 3pt pullback
    # (no single-burst tail: the move developed across the whole window)
    sustained = [100.0 + i * (24.0 / 17) for i in range(17)] + [121.0]
    assert e.check_entry(121.0, _scalp_hist(sustained), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False) is not None


def test_sparse_window_blocks_confirmation():
    """With fewer than SCALP_CONFIRM_MIN_SAMPLES ticks, entry is blocked —
    confirmation is mandatory (closes the old '<6 samples skips all checks' hole)."""
    e = ScalpEngine(_ScalpCfg())
    sparse = [100.0, 104.0, 110.0, 118.0]  # only 4 ticks, big move
    assert e.check_entry(118.0, _scalp_hist(sparse), datetime(2026, 1, 1, 10, 0),
                         htf5=1, safe_mode=False) is None


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
