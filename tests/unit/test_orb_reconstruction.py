# tests/unit/test_orb_reconstruction.py
# Phase 7 — ORB reconstruction must be fault-tolerant.
#
# Covers: success, transient API failure, repeated failure, malformed candle,
# empty response, wrong date, incomplete window, and eventual recovery.

import time
from datetime import datetime, time as dtime, date, timedelta

import pytest

from engine.live_engine import LiveEngine


# ── helpers ──────────────────────────────────────────────────────────────

def _make_candle(minute, high, low, day=None, tzinfo=None):
    """Build a zerodha-style candle dict for a 9:15-9:29 window minute."""
    d = day if day is not None else date.today()
    base = datetime.combine(d, dtime(9, minute))
    dt = base.replace(tzinfo=tzinfo) if tzinfo else base
    return {"date": dt, "open": (high + low) / 2.0, "close": (high + low) / 2.0,
            "high": high, "low": low, "volume": 100}


def _full_window(day=None, tzinfo=None, minutes=None):
    """15 candles, 9:15-9:29."""
    minutes = minutes if minutes is not None else list(range(15, 30))
    return [_make_candle(m, 100.0 + m, 90.0 + m * 0.5, day=day, tzinfo=tzinfo) for m in minutes]


class _FakeKite:
    def __init__(self, result_loader):
        self.result_loader = result_loader
        self.calls = 0

    def historical_data(self, *a, **k):
        self.calls += 1
        return self.result_loader(self.calls)


class _FakeBroker:
    def __init__(self, result_loader):
        self.kite = _FakeKite(result_loader)


def _make_engine(retries=3, backoff=0.0, min_candles=10):
    """Lightweight LiveEngine without heavy model init."""
    eng = LiveEngine.__new__(LiveEngine)
    eng.orb_high = None
    eng.orb_low = None
    eng.orb_done = False
    eng.orb_status = "NONE"
    eng.orb_reconstruct_attempts = 0
    eng.orb_last_error = ""
    eng._orb_retries = retries
    eng._orb_backoff_base = backoff
    eng._orb_min_candles = min_candles
    return eng


def _after_window():
    return datetime.combine(date.today(), dtime(9, 31))


# ── tests ────────────────────────────────────────────────────────────────

def test_successful_reconstruction():
    eng = _make_engine()
    broker = _FakeBroker(lambda calls: _full_window())
    eng.reconstruct_orb_if_needed(broker, _after_window())
    assert eng.orb_status == "VALID"
    assert eng.orb_done is True
    assert eng.orb_high is not None and eng.orb_low is not None
    assert eng.orb_high >= eng.orb_low
    assert eng.orb_reconstruct_attempts == 1


def test_transient_api_failure_then_recovery():
    eng = _make_engine(retries=3, backoff=0.0)
    calls = {"n": 0}

    def loader(call):
        if call == 1:
            raise ConnectionError("simulated transient network failure")
        return _full_window()

    broker = _FakeBroker(loader)
    eng.reconstruct_orb_if_needed(broker, _after_window())
    assert eng.orb_status == "VALID"
    assert eng.orb_reconstruct_attempts == 2
    assert eng.orb_high is not None


def test_repeated_api_failures_fail_safe():
    eng = _make_engine(retries=2, backoff=0.0)
    broker = _FakeBroker(lambda calls: (_ for _ in ()).throw(ConnectionError("down")))
    eng.reconstruct_orb_if_needed(broker, _after_window())
    assert eng.orb_status == "FAILED"
    assert eng.orb_high is None and eng.orb_low is None
    assert eng.orb_done is True  # locked, never guesses
    assert eng.orb_reconstruct_attempts == 2


def test_empty_response_fails_safe():
    eng = _make_engine(retries=1, backoff=0.0)
    broker = _FakeBroker(lambda calls: [])
    eng.reconstruct_orb_if_needed(broker, _after_window())
    assert eng.orb_status == "FAILED"
    assert eng.orb_high is None


def test_malformed_candles_rejected():
    eng = _make_engine(retries=1, backoff=0.0, min_candles=15)
    bad = _full_window()
    bad[0]["high"] = "not-a-number"
    bad[1]["high"] = 1.0
    bad[1]["low"] = 5.0  # high < low
    broker = _FakeBroker(lambda calls: bad)
    eng.reconstruct_orb_if_needed(broker, _after_window())
    # 13 valid of 15 < min 15 -> incomplete -> FAILED, not guessed
    assert eng.orb_high is None
    assert eng.orb_status == "FAILED"


def test_wrong_date_ignored():
    yesterday = date.today() - timedelta(days=1)
    eng = _make_engine(retries=1, backoff=0.0, min_candles=15)
    broker = _FakeBroker(lambda calls: _full_window(day=yesterday))
    eng.reconstruct_orb_if_needed(broker, _after_window())
    assert eng.orb_high is None
    assert eng.orb_status == "FAILED"


def test_incomplete_orb_window_blocked():
    eng = _make_engine(retries=1, backoff=0.0, min_candles=15)
    # only 5 candles in window
    broker = _FakeBroker(lambda calls: _full_window(minutes=[15, 16, 17, 18, 19]))
    eng.reconstruct_orb_if_needed(broker, _after_window())
    assert eng.orb_high is None
    assert eng.orb_status == "FAILED"


def test_incomplete_ok_while_still_in_window():
    eng = _make_engine(retries=1, backoff=0.0, min_candles=15)
    # startup at 9:20 during window: only 5 candles complete but live feed adds rest
    ts = datetime.combine(date.today(), dtime(9, 20))
    broker = _FakeBroker(lambda calls: _full_window(minutes=[15, 16, 17, 18, 19]))
    eng.reconstruct_orb_if_needed(broker, ts)
    assert eng.orb_high is not None
    assert eng.orb_done is False  # not locked, live accumulation continues


def test_before_market_open_noop():
    eng = _make_engine()
    broker = _FakeBroker(lambda calls: _full_window())
    eng.reconstruct_orb_if_needed(broker, datetime.combine(date.today(), dtime(8, 0)))
    assert eng.orb_status == "NONE"
    assert eng.orb_high is None


def test_already_populated_short_circuit():
    eng = _make_engine()
    eng.orb_high = 100.0
    eng.orb_low = 90.0
    broker = _FakeBroker(lambda calls: (_ for _ in ()).throw(AssertionError("must not call")))
    eng.reconstruct_orb_if_needed(broker, _after_window())
    assert eng.orb_status == "VALID"


def test_weekend_unavailable_without_retries():
    sat = date(2026, 8, 8)  # a Saturday
    eng = _make_engine(retries=5, backoff=0.0)
    broker = _FakeBroker(lambda calls: (_ for _ in ()).throw(AssertionError("must not call")))
    eng.reconstruct_orb_if_needed(broker, datetime.combine(sat, dtime(10, 0)))
    assert eng.orb_status == "UNAVAILABLE"
    assert eng.orb_reconstruct_attempts == 0
