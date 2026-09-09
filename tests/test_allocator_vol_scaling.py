# tests/test_allocator_vol_scaling.py
#
# feat(allocator): vol-scaling + confidence sizing behind ENABLE_VOL_SCALING.
# Key invariant: when realized ATR rises, vol_adj falls, so the final size
# must NOT increase (shrinks or stays). When the flag is OFF, behaviour is
# unchanged from the base risk-sized allocator.

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from engine.portfolio.allocator import CapitalAllocator


class FakeConfig:
    CHAMPION_THRESHOLD = 0.65
    LOT_SIZE = 30


def _allocator(monkeypatch, enable=True, conf_max="1.0"):
    monkeypatch.setenv("ENABLE_VOL_SCALING", "1" if enable else "0")
    monkeypatch.setenv("CONF_MAX_FRAC", conf_max)   # isolate vol effect
    return CapitalAllocator(FakeConfig())


def _candles(atr_level: float, n: int = 60, step: float = 1.0):
    """Synthetic OHLC series whose ATR(14) ≈ atr_level."""
    base = 50000.0
    highs, lows, closes = [], [], []
    c = base
    for _ in range(n):
        c += step
        h = c + atr_level / 2.0
        l = c - atr_level / 2.0
        highs.append(h); lows.append(l); closes.append(c)
    return [{"high": h, "low": l, "close": c}
            for h, l, c in zip(highs, lows, closes)]


def test_size_decreases_when_atr_increases(monkeypatch):
    a = _allocator(monkeypatch, enable=True)
    quiet = _candles(atr_level=8.0)
    volatile = _candles(atr_level=40.0)   # 5x the ATR

    kwargs = dict(capital=100_000, ml_prob=0.70, atr=20.0, price=300.0,
                  lot_size=30, current_pnl=0, stop_pts=1.0,
                  calibrated_p=0.72)      # small stop -> large base lots
    sz_quiet = a.size_position(candles=quiet, **kwargs)
    sz_vol = a.size_position(candles=volatile, **kwargs)

    assert sz_quiet > 0
    assert sz_vol < sz_quiet   # higher vol -> smaller size


def test_vol_adj_clamped_to_range(monkeypatch):
    a = _allocator(monkeypatch, enable=True)
    very_volatile = _candles(atr_level=1000.0)   # target 20 / 1000 = 0.02 -> clamp 0.25
    assert a._vol_adjustment(very_volatile) == pytest.approx(0.25)
    flat = _candles(atr_level=2.0)               # 20 / 2 = 10 -> clamp 2.0
    assert a._vol_adjustment(flat) == pytest.approx(2.0)


def test_flag_off_is_neutral(monkeypatch):
    a = _allocator(monkeypatch, enable=False)
    assert a.vol_scaling is False
    # Flag off: helpers return the raw calc but sizing does NOT apply them,
    # so the output equals the base risk-sized lots.
    kwargs = dict(capital=100_000, ml_prob=0.70, atr=20.0, price=300.0,
                  lot_size=30, current_pnl=0, stop_pts=6.0)
    base = a.size_position(**kwargs)
    scaled_quiet = a.size_position(candles=_candles(8.0), calibrated_p=0.72, **kwargs)
    scaled_vol = a.size_position(candles=_candles(40.0), calibrated_p=0.72, **kwargs)
    assert scaled_quiet == base
    assert scaled_vol == base


def test_confidence_adjustment_edge(monkeypatch):
    a = _allocator(monkeypatch, enable=True, conf_max="0.02")
    # p=0.5 (no edge) -> 0; p=0.95 (max edge) -> clipped to max_frac
    assert a._confidence_adjustment(0.5) == 0.0
    assert a._confidence_adjustment(0.95) == pytest.approx(0.02)
    # neutral when no calibrated p supplied
    assert a._confidence_adjustment(None) == 1.0


def test_reject_below_threshold_unchanged_by_scaling(monkeypatch):
    a = _allocator(monkeypatch, enable=True)
    sz = a.size_position(capital=100_000, ml_prob=0.50, atr=20.0, price=300.0,
                         lot_size=30, current_pnl=0, stop_pts=6.0)
    assert sz == 0
