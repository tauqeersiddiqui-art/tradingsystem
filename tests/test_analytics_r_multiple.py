# tests/test_analytics_r_multiple.py
#
# fix(analytics): R_multiple must be measured against the stop as of ENTRY,
# not the ratcheted/trailed live stop.  With the old code a winner whose trail
# stop rose above entry produced a NEGATIVE R even though it made money, which
# poisoned every R-based stat (expectancy by R, avg R, drift checks).
#
# The regression here: entry=100, initial_stop=95 (5pt risk), stop ratchets to
# 103 (above entry) by exit, exit=104 → pnl=+4.  Old R = 4/(100-103) = -1.333.
# Correct R = 4/(100-95) = +0.8.

import csv
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import engine.services.trade_logger as tl  # noqa: E402


@pytest.fixture
def tmp_trade_dir(tmp_path, monkeypatch):
    """Point the weekly CSV into a temp dir and reset the row sequence."""
    monkeypatch.setattr(tl, "_TRADE_DIR", str(tmp_path))
    monkeypatch.setattr(tl, "_trade_seq", 0)
    return tmp_path


def _read_last_row(tmp_path):
    path = os.path.join(tmp_path, sorted(os.listdir(tmp_path))[-1])
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-1]


def test_r_multiple_positive_when_trailed_stop_above_entry(tmp_trade_dir):
    """Regression: winner whose trail stop ratcheted above entry → +R."""
    position = {
        "entry": 100.0,
        "qty": 30,
        "side": "CE",
        "symbol": "BANKNIFTY26SEP57000CE",
        "regime": "RANGE",
        "reason": "STRUCT_CE",
        "ml_prob": 0.75,
        "max_pnl": 4.0 * 30,          # peak +4pts
        "stop_loss": 103.0,           # LIVE stop — ratcheted ABOVE entry
        "initial_stop": 95.0,         # stop at ENTRY = the true 5pt risk
        "target": 0.0,
    }
    entry_order = {"price": 100.0, "symbol": "BANKNIFTY26SEP57000CE", "qty": 30, "side": "CE"}
    t0 = datetime(2026, 9, 9, 10, 0, 0)

    tl.log_trade(entry_order, exit_price=104.0, exit_reason="TARGET_HIT",
                 position=position, entry_time=t0,
                 exit_time=datetime(2026, 9, 9, 10, 5, 0))

    row = _read_last_row(tmp_trade_dir)
    # pnl = +4 pts * 30 = +120
    assert abs(float(row["pnl"]) - 120.0) < 1e-6
    # risk = |100 - 95| * 30 = 150
    assert abs(float(row["stop_distance_pts"]) - 5.0) < 1e-6
    # R = +120 / 150 = +0.8
    assert abs(float(row["R_multiple"]) - 0.8) < 1e-6
    assert float(row["R_multiple"]) > 0


def test_r_multiple_fallback_when_initial_stop_missing(tmp_trade_dir):
    """No initial_stop recorded → fall back to live stop (backward-compat)."""
    position = {
        "entry": 100.0,
        "qty": 30,
        "side": "CE",
        "symbol": "BANKNIFTY26SEP57000CE",
        "regime": "RANGE",
        "reason": "STRUCT_CE",
        "ml_prob": 0.75,
        "max_pnl": 0.0,
        "stop_loss": 97.0,   # no initial_stop key → this is used
        "target": 0.0,
    }
    entry_order = {"price": 100.0, "symbol": "BANKNIFTY26SEP57000CE", "qty": 30, "side": "CE"}
    t0 = datetime(2026, 9, 9, 10, 0, 0)

    tl.log_trade(entry_order, exit_price=97.0, exit_reason="STOP",
                 position=position, entry_time=t0,
                 exit_time=datetime(2026, 9, 9, 10, 1, 0))

    row = _read_last_row(tmp_trade_dir)
    # pnl = -3pts*30 = -90; risk = 3pts*30 = 90 → R = -1.0
    assert abs(float(row["R_multiple"]) - (-1.0)) < 1e-6


def test_initial_stop_persisted_in_csv(tmp_trade_dir):
    """initial_stop_distance_pts column is written even for a plain loser."""
    position = {
        "entry": 100.0,
        "qty": 30,
        "side": "PE",
        "symbol": "BANKNIFTY26SEP56500PE",
        "regime": "RANGE",
        "reason": "STRUCT_PE",
        "ml_prob": 0.70,
        "max_pnl": 0.0,
        "stop_loss": 96.0,
        "initial_stop": 96.0,
        "target": 0.0,
    }
    entry_order = {"price": 100.0, "symbol": "BANKNIFTY26SEP56500PE", "qty": 30, "side": "PE"}
    t0 = datetime(2026, 9, 9, 11, 0, 0)
    tl.log_trade(entry_order, exit_price=96.0, exit_reason="STOP",
                 position=position, entry_time=t0,
                 exit_time=datetime(2026, 9, 9, 11, 5, 0))
    row = _read_last_row(tmp_trade_dir)
    assert row["initial_stop_distance_pts"] == "4.0"


def test_state_store_persists_initial_stop(tmp_path, monkeypatch):
    """initial_stop survives a position serialize/deserialize round-trip."""
    monkeypatch.setattr("engine.core.state_store._STATE_PATH",
                        str(tmp_path / "runtime_state.json"))
    from engine.core import state_store as ss

    class FakeCtx:
        pnl = 0.0
        trades_today = 0
        positions = []

    pos = {"symbol": "X", "side": "CE", "qty": 30, "entry": 100.0,
           "stop_loss": 103.0, "initial_stop": 95.0, "regime": "RANGE",
           "reason": "STRUCT_CE", "ml_prob": 0.7, "max_pnl": 0.0,
           "min_pnl": 0.0, "target": 0.0, "sl_order_id": None,
           "entry_ts": datetime(2026, 9, 9, 10, 0, 0)}
    ss.save_state(FakeCtx(), position=pos)
    snap = ss.load_state()
    restored = ss.deserialize_position(snap["open_position"])
    assert restored["initial_stop"] == 95.0
