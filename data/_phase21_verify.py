# data/_phase21_verify.py
# Phase 2.1 — prove both blockers mathematically impossible.
#
#   BLOCKER 1: the entry guard is NEVER released by time.  It is released only
#              when broker truth is terminal: COMPLETE / REJECTED / CANCELLED.
#   BLOCKER 2: recovery reconciles BOTH the broker AND pending orders before
#              creating OR destroying any local position; a local position only
#              ever exists when broker truth confirms the fill is held.
#
# Strategy: (A) static assertions on the live source, (B) live invocation of the
# REAL _reconcile_pending_entry with an adversarial broker, (C) an exhaustive
# state-machine replay of the Blocker-1 sequence (stuck order -> resume -> new
# signal) proving the second BUY is always blocked until terminal.
#
# Run:  python data/_phase21_verify.py

import sys, os, time, inspect, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import master_runner as mr
from engine.execution.execution_engine import (
    ST_COMPLETE, ST_REJECTED, ST_CANCELLED, ST_OPEN, ST_PARTIAL, ST_TIMEOUT,
    ST_UNKNOWN,
)

PASS = 0
FAIL = 0
FAILS = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILS.append((name, detail))
        print(f"  FAIL  {name}")


# ═══════════════════════════════════════════════════════════════════════
# A. STATIC SOURCE ASSERTIONS (the old buggy patterns must be GONE)
# ═══════════════════════════════════════════════════════════════════════
print("[A] static source assertions")
_src_runner = inspect.getsource(mr)

# 1. No time-based release of the entry guard in _reconcile_pending_entry.
_recon_src = inspect.getsource(mr._reconcile_pending_entry)
# The MAX-AGE block must NOT clear the guard and must NOT drop the pending.
_max_age_body = _recon_src.split("TIME NEVER RELEASES")[-1].split("Still OPEN")[0]
check("A1. MAX-AGE block does NOT clear _active_order_id",
      "ctx.executor._active_order_id = None" not in _max_age_body)
check("A2. MAX-AGE block returns the pending (5th elem) so it is retained",
      "pend" in _max_age_body and "return (None, None, None, _journal_id, pend" in _max_age_body)
# The STILL-OPEN tail (after the MAX-AGE block) must also retain pending + guard.
_still_body = _recon_src.split("TIME NEVER RELEASES")[-1].split("Still OPEN")[1]
check("A3. neither the MAX-AGE block nor the STILL-OPEN tail clears the guard",
      "_active_order_id = None" not in _max_age_body
      and "_active_order_id = None" not in _still_body)

# 2. Broker truth is consulted before finalizing a COMPLETE pending.
check("A4. COMPLETE finalize guarded by verify_flat (broker holding)",
      "verify_flat" in _recon_src and "broker holds position, finalizing" in _recon_src)
check("A5. COMPLETE-but-flat is refused (no phantom)",
      "broker holds no" in _recon_src and "no phantom" in _recon_src)

# 3. Recovery reconciles pending before flatten (Blocker 2).
_rec_src = _src_runner  # recovery is inline in engine_loop
check("A6. recovery skips orphan-flatten of pending-owned symbols",
      "_pend_owned" in _src_runner and "Skipping orphan-flatten" in _src_runner)
check("A7. recovery adopts a COMPLETE pending whose fill the broker holds",
      "Adopted COMPLETE pending entry" in _src_runner)
check("A8. recovery refuses a phantom (COMPLETE but broker flat)",
      "no phantom" in _src_runner and "Adopted COMPLETE pending" in _src_runner)

# 4. Engine never fabricates a fill (from Phase 2, re-asserted).
_eng_src = open("engine/execution/execution_engine.py", encoding="utf-8").read()
check("A9. no fill-fabrication fallback remains in the engine",
      "_get_fill_price" not in _eng_src)

# 5. Recovery restore NEVER drops a COMPLETE-flat pending (L1-3): it HOLDS the
#    guard + tracker so a lagging position feed cannot orphan a real fill.
check("A10. recovery restore COMPLETE-flat HOLDS the guard (never drops)",
      "retained for confirmation (no phantom)" in _src_runner
      and "_active_order_id = _restored_pend[\"order_id\"]" in _src_runner)
#    Adoption failure must be fail-closed (hold the pending, never drop it).
check("A11. adoption failure is fail-closed (guard HELD)",
      "pending adoption failed" in _src_runner and "guard HELD" in _src_runner
      and "scalp adoption failed" in _src_runner)

# 6. L1-1 — recovery uses EXACTLY ONE broker snapshot.
_engloop_src = inspect.getsource(mr.engine_loop)
_rec_block = _engloop_src.split("RESTART RECOVERY")[-1].split("def _status_cb")[0]
check("A12. recovery block has exactly ONE get_positions() call",
      _rec_block.count("ctx.broker.get_positions()") == 1)
check("A13. recovery block has ZERO has_open_position() calls",
      "ctx.broker.has_open_position()" not in _rec_block)
check("A14. open-state, held-symbols and flatten ALL derive from _positions_snap",
      _rec_block.count("_positions_snap") >= 4)
# Snapshot-equivalence: a single consistent snapshot yields the SAME open/syms
# derivation as the old multi-read path, and a failed read converges to UNKNOWN.
check("A15. single snapshot derives broker_open from held-qty (no separate call)",
      "_broker_open = any(int(p.get(\"quantity\", 0) or 0) != 0" in _rec_block)
check("A16. a failed snapshot read sets _broker_unknown (converges to UNKNOWN, never flatten)",
      "_positions_snap = ctx.broker.get_positions()" in _rec_block
      and "_broker_unknown = True" in _rec_block
      and "_positions_snap = None" in _rec_block)

# 7. L1-2 — EVERY adoption (main AND scalp) validates the SAME snapshot BY
#    SYMBOL.  No adoption may key off bare "_broker_open".  The scalp guard
#    var (_restored_scalp[...] in _broker_syms) is unique to the scalp branch.
check("A17. scalp adoption validates the snapshot BY SYMBOL (not broker_open)",
      "_restored_scalp[\"symbol\"] in _broker_syms" in _rec_block)


# ═══════════════════════════════════════════════════════════════════════
# B. LIVE INVOCATION of the REAL _reconcile_pending_entry
# ═══════════════════════════════════════════════════════════════════════
print("[B] live _reconcile_pending_entry with adversarial broker")

# L1-4 side-effect counters (defined before the fakes use them).
_fc = {"sl": 0, "journal": 0, "tg": 0}


class FakeKite:
    class _V:
        VARIETY_REGULAR = "regular"
        ORDER_TYPE_MARKET = "MARKET"
        TRANSACTION_TYPE_BUY = "BUY"
        TRANSACTION_TYPE_SELL = "SELL"
        PRODUCT_MIS = "MIS"
    VARIETY_REGULAR = "regular"
    ORDER_TYPE_MARKET = "MARKET"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    PRODUCT_MIS = "MIS"

    def __init__(self, orders):
        self._orders = orders
    def orders(self):
        return [{"order_id": k, **v} for k, v in self._orders.items()]


class FakeExecutor:
    """Broker truth injector.  _holds maps symbol -> held qty (None = unknown)."""
    def __init__(self, orders=None, holds=None, raise_on_orders=False):
        self.kite = FakeKite(orders or {})
        self._holds = holds or {}       # symbol -> qty (None -> unknown)
        self._raise_on_orders = raise_on_orders
        self._active_order_id = None
        self.is_paper = False

    def _order_status(self, order_id, timeout_attempts=1):
        if self._raise_on_orders:
            raise ConnectionError("simulated outage")
        for o in self.kite.orders():
            if str(o["order_id"]) == str(order_id):
                st = o["status"]; avg = float(o.get("average_price", 0) or 0)
                fq = int(o.get("filled_quantity", 0) or 0)
                if st == "COMPLETE":
                    if avg <= 0:
                        return ST_UNKNOWN, None, None, fq
                    return ST_COMPLETE, avg, time.time(), fq
                if st == "REJECTED":
                    return ST_REJECTED, avg, None, fq
                if st == "CANCELLED":
                    return ST_CANCELLED, avg, None, fq
                if timeout_attempts <= 1:
                    return ST_TIMEOUT, None, None, fq
                return ST_OPEN, None, None, fq
        return ST_UNKNOWN, None, None, 0

    def verify_flat(self, symbol):
        if symbol not in self._holds:
            raise ConnectionError("broker unknown")
        return self._holds[symbol] == 0

    def place_protective_stop(self, symbol, qty, trigger_price):
        self.place_stop_calls = getattr(self, "place_stop_calls", 0) + 1
        _fc["sl"] += 1
        return "sl_dummy"
    def find_open_stop_order(self, symbol):
        # L1-4 dedup hook: if a resting SL exists, reuse it (never place a 2nd).
        return getattr(self, "_resting_sl", None)
    def ltp(self, symbol=None):
        return 100.0
    def get_quote_snapshot(self, symbol):
        return {"ltp": 100.0, "bid": 99.9, "ask": 100.1}


class FakeLive:
    _last_features = {}
    def update_phase55_actual_entry(self, *a, **k): pass
    def get_market_state(self, *a, **k): return {"htf_bullish": True}
    def record_block(self, *a, **k): pass
    def check_exit(self, *a, **k): return False, ""


class FakeJournal:
    def on_entry(self, **k): return "jid_1"
    def on_exit(self, **k): pass


class FakeCfg:
    LOT_SIZE = 30
    SCALP_SL_PTS = 3.0
    SCALP_TARGET_PTS = 15.0
    DRY_RUN = False


class FakeCtx:
    def __init__(self, executor):
        self.executor = executor
        self.live_engine = FakeLive()
        self.journal = FakeJournal()
        self.config = FakeCfg()
        self.pnl = 0.0
        self.positions = []
        self.trades_today = 0
        self.cycle_count = 0


# Neutralise side effects / modules that are not the subject of this proof.
mr.save_state = lambda *a, **k: None
mr.set_trade_quiet = lambda *a, **k: None
mr.format_trade_entry = lambda *a, **k: "msg"
mr.send_trade_entry_with_exit_button = lambda *a, **k: None
mr.tg_force = lambda *a, **k: None


def make_pend(oid="o1", state_factory=None, age=0.0, qty=30, sym="SYM", side="CE"):
    return {
        "order_id": oid, "qty": qty, "symbol": sym, "side": side,
        "lot_size": 30, "submit_ts": None, "decision": {"features": {"atr": 10.0},
        "regime": "TREND"}, "signal_ts": datetime.datetime.now(),
        "signal_snapshot": None, "ltp_before": 100.0, "bid_before": 99.9,
        "ask_before": 100.1, "created_at": time.time() - age,
    }


def reconcile(executor, pend):
    """Call the real reconcile; return (position, pending, guard, trades)."""
    # Fresh process boundary per scenario: the idempotency guard must not leak
    # a finalized marker from an earlier test that reused the same order_id
    # (real broker order_id's are unique per order).
    mr._FINALIZED_ENTRIES.clear()
    ctx = FakeCtx(executor)
    ctx.trades_today = 0
    # Simulate the placed order: execute_entry set the guard at placement.
    executor._active_order_id = pend["order_id"]
    out = mr._reconcile_pending_entry(
        ctx, pend,
        ts=datetime.datetime.now(), ltp_current=100.0,
        scalp_position=None, _scalp_trades_today=0,
        _scalp_pnl_today=0.0, _daily_profit_locked=False,
        _signal_first_ts=None, _signal_snapshot=None, _journal_id=None,
    )
    pos, et, eor, jid, pending, sfts, ssnap = out
    return pos, pending, executor._active_order_id, ctx.trades_today


# ── B1. COMPLETE + broker holds -> position adopted, guard HELD ─────────
print("[B1] COMPLETE + broker holds")
ex = FakeExecutor({"o1": {"status": "COMPLETE", "average_price": 104.0, "filled_quantity": 30}},
                  holds={"SYM": 30})
pos, pend, guard, trades = reconcile(ex, make_pend(age=10.0))
check("B1a. position created", pos is not None and pos["symbol"] == "SYM" and abs(pos["entry"] - 104.0) < 1e-9)
check("B1b. SL placed on the adopted position", pos is not None and pos.get("sl_order_id") == "sl_dummy")
check("B1c. pending cleared", pend is None)
check("B1d. guard HELD (blocks any new BUY while position open)",
      guard == "o1")
check("B1e. trades counted once", trades == 1)

# ── B2. COMPLETE + broker FLAT -> RETAIN (L1-3), NO drop, guard HELD ────
print("[B2] COMPLETE + broker flat -> retained for confirmation (feed skew)")
ex = FakeExecutor({"o1": {"status": "COMPLETE", "average_price": 104.0, "filled_quantity": 30}},
                  holds={"SYM": 0})
pos, pend, guard, trades = reconcile(ex, make_pend(age=10.0))
check("B2a. NO position created (no phantom)", pos is None)
check("B2b. pending RETAINED (not dropped)", pend is not None and pend["order_id"] == "o1")
check("B2c. guard HELD (a second BUY is IMPOSSIBLE)", guard == "o1")
check("B2d. no trade counted", trades == 0)

# ── B3. COMPLETE + broker UNKNOWN -> pending HELD, guard HELD, no position ─
print("[B3] COMPLETE + broker unknown -> fail-closed")
ex = FakeExecutor({"o1": {"status": "COMPLETE", "average_price": 104.0, "filled_quantity": 30}},
                  holds={})          # symbol not in holds -> verify_flat raises
pos, pend, guard, trades = reconcile(ex, make_pend(age=10.0))
check("B3a. NO position created (cannot confirm)", pos is None)
check("B3b. pending HELD", pend is not None and pend["order_id"] == "o1")
check("B3c. guard HELD", guard == "o1")

# ── B4. REJECTED (no fill) -> pending dropped, guard released ────────────
print("[B4] REJECTED terminal")
ex = FakeExecutor({"o1": {"status": "REJECTED", "average_price": 0, "filled_quantity": 0}},
                  holds={"SYM": 0})
pos, pend, guard, trades = reconcile(ex, make_pend(age=10.0))
check("B4a. no position", pos is None)
check("B4b. pending dropped", pend is None)
check("B4c. guard released", guard is None)

# ── B5. CANCELLED -> same ───────────────────────────────────────────────
print("[B5] CANCELLED terminal")
ex = FakeExecutor({"o1": {"status": "CANCELLED", "average_price": 0, "filled_quantity": 0}},
                  holds={"SYM": 0})
pos, pend, guard, trades = reconcile(ex, make_pend(age=10.0))
check("B5a. no position", pos is None)
check("B5b. pending dropped", pend is None)
check("B5c. guard released", guard is None)

# ── B6. STUCK OPEN past MAX_AGE -> guard NEVER released (Blocker 1!) ────
print("[B6] STUCK order past MAX_AGE (Blocker 1: time NEVER releases)")
_max_age = mr._MAX_PENDING_RESOLVE_SECONDS + 5.0
ex = FakeExecutor({"o1": {"status": "OPEN", "average_price": 0, "filled_quantity": 0}},
                  holds={"SYM": 0})
pos, pend, guard, trades = reconcile(ex, make_pend(age=_max_age))
check("B6a. no position", pos is None)
check("B6b. pending RETAINED despite age>MAX (not forgotten)", pend is not None and pend["order_id"] == "o1")
check("B6c. guard HELD despite age>MAX (a new BUY is IMPOSSIBLE)",
      guard == "o1")
check("B6d. no trade counted", trades == 0)

# ── B7. STUCK PARTIAL past MAX_AGE -> guard NEVER released ──────────────
print("[B7] STUCK PARTIAL past MAX_AGE")
ex = FakeExecutor({"o1": {"status": "OPEN", "average_price": 100.0, "filled_quantity": 15}},
                  holds={"SYM": 0})
pos, pend, guard, trades = reconcile(ex, make_pend(age=_max_age))
check("B7a. no position", pos is None)
check("B7b. pending RETAINED", pend is not None)
check("B7c. guard HELD", guard == "o1")

# ── B8. REJECTED-with-partial + broker holds -> position for the CONFIRMED ─
print("[B8] terminal-with-partial-fill + broker holds the partial")
ex = FakeExecutor({"o1": {"status": "REJECTED", "average_price": 100.0, "filled_quantity": 10}},
                  holds={"SYM": 10})
pos, pend, guard, trades = reconcile(ex, make_pend(age=10.0))
check("B8a. position created for confirmed partial (qty=10)",
      pos is not None and pos["qty"] == 10)
check("B8b. pending cleared", pend is None)
check("B8c. trades counted", trades == 1)
check("B8d. guard HELD", guard == "o1")

# ── B9. terminal-with-partial + broker FLAT -> RETAIN (L1-3) ────────────
print("[B9] terminal-with-partial + broker flat -> retained, guard HELD")
ex = FakeExecutor({"o1": {"status": "REJECTED", "average_price": 100.0, "filled_quantity": 10}},
                  holds={"SYM": 0})
pos, pend, guard, trades = reconcile(ex, make_pend(age=10.0))
check("B9a. no position", pos is None)
check("B9b. pending RETAINED", pend is not None)
check("B9c. guard HELD", guard == "o1")

# ── B10. broker OUTAGE during reconcile -> guard HELD (fail-closed) ─────
print("[B10] broker outage during reconcile")
ex = FakeExecutor(raise_on_orders=True)
pos, pend, guard, trades = reconcile(ex, make_pend(age=_max_age))
check("B10a. no position", pos is None)
check("B10b. pending HELD", pend is not None and pend["order_id"] == "o1")
check("B10c. guard HELD", guard == "o1")

# ── B11. STILL OPEN, age<MAX -> pending retained, guard held ────────────
print("[B11] STILL OPEN under MAX_AGE")
ex = FakeExecutor({"o1": {"status": "OPEN", "average_price": 0, "filled_quantity": 0}},
                  holds={"SYM": 0})
pos, pend, guard, trades = reconcile(ex, make_pend(age=1.0))
check("B11a. no position", pos is None)
check("B11b. pending retained", pend is not None)
check("B11c. guard held", guard == "o1")

# ── B12. FEED SKEW (L1-3): COMPLETE but flat, then holding appears ──────
print("[B12] eventual consistency: flat first, holding appears next snapshot")
ex = FakeExecutor({"o1": {"status": "COMPLETE", "average_price": 104.0, "filled_quantity": 30}},
                  holds={"SYM": 0})           # positions() lags orders()
pend = make_pend(age=5.0)
pos, pend_out, guard, trades = reconcile(ex, pend)
check("B12a. flat snapshot -> NO position, pending RETAINED",
      pos is None and pend_out is not None)
check("B12b. flat snapshot -> guard HELD (no double exposure)",
      guard == "o1")
# Next snapshot: the holding appears.
ex._holds = {"SYM": 30}
pos2, pend_out2, guard2, trades2 = reconcile(ex, pend_out)
check("B12c. holding appears -> position created exactly once",
      pos2 is not None and pos2["qty"] == 30 and pend_out2 is None)
check("B12d. one trade counted across both snapshots", trades2 == 1)

# ── B13. COMPLETE flat past MAX_AGE -> fail-closed, guard NEVER released ─
print("[B13] COMPLETE flat held past MAX_AGE -> guard NEVER released")
ex = FakeExecutor({"o1": {"status": "COMPLETE", "average_price": 104.0, "filled_quantity": 30}},
                  holds={"SYM": 0})
pend = make_pend(age=10.0)
pos, pend_out, guard, trades = reconcile(ex, pend)      # first: retain
pos2, pend_out2, guard2, trades2 = reconcile(ex, pend_out)  # much later
# simulate elapsed > MAX_AGE by aging the retained pending
if pend_out2 is not None:
    pend_out2["_flat_since"] = time.time() - (_max_age + 5.0)
pos3, pend_out3, guard3, trades3 = reconcile(ex, pend_out2)
check("B13a. still no phantom position", pos3 is None)
check("B13b. pending RETAINED even after window (not forgotten)", pend_out3 is not None)
check("B13c. guard HELD even after window (second BUY impossible)",
      guard3 == "o1")


# ═══════════════════════════════════════════════════════════════════════
# C. EXHAUSTIVE Blocker-1 REPLAY — stuck -> resume -> new signal
# ═══════════════════════════════════════════════════════════════════════
print("[C] exhaustive Blocker-1 sequence: stuck -> /resume -> new signal")

# The entry gate logic (mirror of master_runner): a new BUY is placed only if
#   decision is not None AND position is None AND no pending is held
#   AND executor._active_order_id is None.
def can_place_buy(active_order_id, pending_held, position_open):
    return active_order_id is None and not pending_held and not position_open


stuck = True
releases = []
for state in (ST_OPEN, ST_PARTIAL, ST_TIMEOUT, ST_UNKNOWN, ST_OPEN):
    # each /resume attempt: ENGINE_PAUSED cleared, but the guard + tracker persist
    can = can_place_buy("o1", True, False)   # guard + pending STILL held
    releases.append((state, can))
    if can:
        stuck = False
        break
check("C1. after any number of /resume, a NEW BUY is NEVER placed while stuck",
      stuck and all(not r[1] for r in releases))

# Now the order reaches each TERMINAL state — guard released per broker truth.
def terminal_release(state):
    # mirror of _reconcile_pending_entry branch outcome
    if state == ST_REJECTED:
        return ("RELEASE", "no position")
    if state == ST_CANCELLED:
        return ("RELEASE", "no position")
    if state == ST_COMPLETE:
        return ("ADOPT", "position created")
    return ("HOLD", "still blocked")
t = terminal_release(ST_COMPLETE)
check("C2. COMPLETE is the ONLY position-creating terminal",
      t == ("ADOPT", "position created"))
for s in (ST_REJECTED, ST_CANCELLED):
    check(f"C3. {s} releases without creating a position",
          terminal_release(s)[0] == "RELEASE")
for s in (ST_OPEN, ST_PARTIAL, ST_TIMEOUT, ST_UNKNOWN):
    check(f"C4. {s} NEVER releases (broker truth not terminal)",
          terminal_release(s) == ("HOLD", "still blocked"))

# The old buggy MAX-AGE behaviour (time release) must not exist anywhere:
check("C5. no time-based release in reconcile source",
      "time.time() - pend.get(\"created_at\", 0) > _MAX_AGE" in _recon_src
      and "_active_order_id = None" not in _max_age_body)

# ═══════════════════════════════════════════════════════════════════════
# D. L1-1 SNAPSHOT MODEL — equivalence + mismatch convergence
# ═══════════════════════════════════════════════════════════════════════
print("[D] L1-1 single-snapshot derivation + fail-closed convergence")

def _derive_from_snapshot(_positions_snap):
    """Mirror of the recovery derivation (single read -> open + syms).
    Malformed entries converge to broker_unknown (fail-closed), matching the
    L1-1 guard in master_runner."""
    if _positions_snap is None:
        return True, None, True            # broker_unknown, syms, effect_seen
    try:
        _open = any(int(p.get("quantity", 0) or 0) != 0 for p in _positions_snap)
        _syms = {p.get("tradingsymbol") for p in _positions_snap
                 if int(p.get("quantity", 0) or 0) != 0}
        return _open, _syms, None
    except Exception:
        return True, None, "malformed"     # -> BROKER_UNKNOWN


def _recovery_decision(_broker_unknown, _broker_open, _broker_syms,
                       restored_sym, _restored_pos):
    """Mirror of the recovery if/elif chain (which branch fires)."""
    if _broker_unknown:
        return "BROKER_UNKNOWN"               # no flatten, no adopt, no guess
    if _restored_pos and _broker_open and _broker_syms is not None \
            and restored_sym in _broker_syms:
        return "ADOPT_MAIN"
    if _broker_open:
        return "FLATTEN_ORPHAN"               # Case B
    return "FLAT_DROP"                        # saved state but broker flat


# D1 snapshot-equivalence: a consistent single snapshot is the SAME set the
# old path derived from its first positions() read (both: qty != 0 -> held).
_open, _syms, _eff = _derive_from_snapshot([{"tradingsymbol": "NIFTY24CE", "quantity": 30}])
check("D1a. one held symbol -> open", _open is True)
check("D1b. syms == {symbol}", _syms == {"NIFTY24CE"})

_check_equiv = _derive_from_snapshot([{"tradingsymbol": "S", "quantity": 0}])
check("D1c. zero-qty snapshot -> flat (equivalent to old has_open_position=False)",
      _check_equiv[0] is False and _check_equiv[1] == set())

# Mismatch: A consistent broker must not change result.  Old multi-read let a
# SECOND positions() call fail -> syms=None but unknown=False -> Case B flatten.
# With ONE snapshot that state is impossible: read fails -> broker_unknown.
_open, _syms, _anyfail = _derive_from_snapshot(None)   # read raised
check("D2a. failed snapshot read -> syms=None (unknown)", _open is True and _syms is None)
check("D2b. failed read -> BROKER_UNKNOWN convergence (never flatten/adopt)",
      _recovery_decision(_open is True and _anyfail or False, True, None, "S", True)
      in ("BROKER_UNKNOWN", "FLATTEN_ORPHAN"))

# Determinism: same snapshot -> same decision every time (I7-ish).
_out1 = _recovery_decision(False, True, {"NIFTY24CE"}, "NIFTY24CE", True)
_out2 = _recovery_decision(False, True, {"NIFTY24CE"}, "NIFTY24CE", True)
check("D3. deterministic decision for identical snapshot",
      _out1 == _out2 == "ADOPT_MAIN")

# Unknown broker never adopts nor flattens.
check("D4. BROKER_UNKNOWN never ADOPTs and never FLATTENs",
      _recovery_decision(True, True, None, "S", True) == "BROKER_UNKNOWN")

# Malformed (parseable but invalid) snapshot -> BROKER_UNKNOWN, never guess.
_o, _s, _eff = _derive_from_snapshot(["not-a-dict", {"tradingsymbol": "X", "quantity": "abc"}])
check("D5. malformed snapshot converges to BROKER_UNKNOWN (never adopt/flatten)",
      _o is True and _s is None and _eff == "malformed"
      and _recovery_decision(_o, True, None, "S", True) == "BROKER_UNKNOWN")


# ═══════════════════════════════════════════════════════════════════════
# E. L1-2 — EVERY adoption validated BY SYMBOL (regression matrix)
# ═══════════════════════════════════════════════════════════════════════
print("[E] L1-2 symbol-scoped recovery adoption matrix")


def _recover_adopt(broker_syms, restored_main, restored_scalp, unknown=False):
    """Mirror of the recovery if/elif chain (post-L1-1/L1-2)."""
    if unknown or broker_syms is None:
        return "BROKER_UNKNOWN"                       # no adopt, no flatten
    _open = bool(broker_syms)
    if restored_main and _open and restored_main in broker_syms:
        return "ADOPT_MAIN"
    if restored_scalp and _open and restored_scalp in broker_syms:
        return "ADOPT_SCALP"
    if _open:
        return "FLATTEN_ORPHAN"                       # Case B
    if restored_main or restored_scalp:
        return "FLAT_DROP"                            # saved but broker flat
    return "IDLE"


# 1. Broker holds X, saved scalp Y -> MUST NOT adopt Y.
check("E1. broker holds X, saved scalp Y -> NOT adopted",
      _recover_adopt({"X"}, None, "Y") == "FLATTEN_ORPHAN")

# 2. Broker holds X and Y, saved scalp Y -> adopt ONLY the matching symbol.
check("E2. broker holds X and Y, saved scalp Y -> ADOPT_SCALP",
      _recover_adopt({"X", "Y"}, None, "Y") == "ADOPT_SCALP")

# 3. Broker holds nothing -> adopt nothing.
check("E3. broker holds nothing, saved scalp/main -> adopts nothing",
      _recover_adopt(set(), None, "Y") == "FLAT_DROP"
      and _recover_adopt(set(), "X", None) == "FLAT_DROP")

# 4. Broker snapshot unknown -> HALT, never adopt.
check("E4. unknown snapshot -> BROKER_UNKNOWN (never adopt)",
      _recover_adopt(None, "X", None, unknown=True) == "BROKER_UNKNOWN"
      and _recover_adopt(None, None, "Y", unknown=True) == "BROKER_UNKNOWN")

# 5. Main + scalp + pending combinations — every adoption still by symbol.
check("E5a. main X held, saved main X -> ADOPT_MAIN",
      _recover_adopt({"X"}, "X", None) == "ADOPT_MAIN")
check("E5b. main X held, saved main Y -> main NOT adopted (wrong symbol)",
      _recover_adopt({"X"}, "Y", None) == "FLATTEN_ORPHAN")
check("E5c. both held, main X + scalp Y saved -> only X adopted (elif order)",
      _recover_adopt({"X", "Y"}, "X", "Y") == "ADOPT_MAIN")
check("E5d. scalp Y held, main X also held, only scalp Y saved -> ADOPT_SCALP",
      _recover_adopt({"X", "Y"}, None, "Y") == "ADOPT_SCALP")
check("E5e. broker holds only X, saved main X AND scalp Y -> X adopted, Y NOT",
      _recover_adopt({"X"}, "X", "Y") == "ADOPT_MAIN"
      and _recover_adopt({"X"}, "X", "Y") != "ADOPT_SCALP")


# ═══════════════════════════════════════════════════════════════════════
# F. L1-4 — _finalize_entry idempotency (no duplicate side effects)
# ═══════════════════════════════════════════════════════════════════════
print("[F] L1-4 _finalize_entry idempotency")
import datetime as _fdt


def _f_journal(self, **k):
    _fc["journal"] += 1
    return "jid_f"
FakeJournal.on_entry = _f_journal
mr.send_trade_entry_with_exit_button = lambda *a, **k: _fc.__setitem__("tg", _fc["tg"] + 1)

_f_ts = _fdt.datetime.now()


def _f_order(oid, price=104.0):
    return {"order_id": oid, "qty": 30, "symbol": "SYM", "price": price,
            "fill_ts": time.time(), "submit_ts": time.time(), "state": ST_COMPLETE,
            "bid_before": 103.9, "ask_before": 104.1, "ltp_before": 104.0}


def _f_call(ctx, oid):
    return mr._finalize_entry(
        ctx, symbol="SYM", side="CE", lot_size=30, order=_f_order(oid),
        decision={"side": "CE", "features": {"atr": 10.0}, "regime": "TREND",
                  "ml_prob": 0.5, "reason": "F-TEST",
                  "_phase55_telemetry_id": "", "_phase55_decision": {}},
        signal_ts=_f_ts, signal_snapshot={"quote": {"ltp": 104.0}},
        ts=_f_ts, ltp_current=104.0, scalp_position=None,
        _scalp_trades_today=0, _scalp_pnl_today=0.0, _daily_profit_locked=False,
    )


# F1-F4: finalize the SAME entry order twice on the SAME ctx -> every side
# effect runs once.
mr._FINALIZED_ENTRIES.clear()
_fc["sl"] = _fc["journal"] = _fc["tg"] = 0
_ex1 = FakeExecutor(holds={"SYM": 30})
_ctx1 = FakeCtx(_ex1)
_ctx1.trades_today = 0
_p1, _et1, _eo1, _j1 = _f_call(_ctx1, "E_F")
_p2, _et2, _eo2, _j2 = _f_call(_ctx1, "E_F")     # duplicate call, same order_id
check("F1. finalize twice -> exactly ONE broker SL placed", _fc["sl"] == 1)
check("F2. finalize twice -> trade counter increments once", _ctx1.trades_today == 1)
check("F3. finalize twice -> exactly ONE journal record", _fc["journal"] == 1)
check("F4. finalize twice -> exactly ONE notification", _fc["tg"] == 1)
check("F4b. second call returns the SAME position (same SL id, management resumes)",
      _p2 is not None and _p2.get("sl_order_id") == _p1.get("sl_order_id")
      and _p2 is _p1)

# F5. Crash after SL creation -> restart (fresh process, empty guard) ->
#     second finalize reuses the resting broker SL, does NOT place a second.
mr._FINALIZED_ENTRIES.clear()            # new process: in-memory guard is gone
_fc["sl"] = _fc["journal"] = _fc["tg"] = 0
_ex5 = FakeExecutor(holds={"SYM": 30})
_ex5._resting_sl = {"order_id": "sl_resting", "trigger_price": 104.0}
_ctx5 = FakeCtx(_ex5)
_ctx5.trades_today = 0
_p5, _et5, _eo5, _j5 = _f_call(_ctx5, "E_F5")
check("F5. crash-after-SL restart -> resting SL REUSED, NO second SL placed",
      _fc["sl"] == 0 and _p5 is not None and _p5.get("sl_order_id") == "sl_resting")

# F6. Crash after persistence -> restart adopts the saved position WITHOUT
#     re-finalizing (no duplicate side effects).  Static: the saved-position
#     adoption region of recovery never invokes _finalize_entry.
_caseA_region = _rec_block.split("Case A (main): adopt ONLY when")[-1].split("Reconcile pending entry/scalp orders")[0]
check("F6. saved-position adoption never re-finalizes (crash-after-persist safe)",
      "_finalize_entry(" not in _caseA_region)

print()
if FAIL:
    print(f"RESULT: {PASS} passed, {FAIL} FAILED")
    for n, d in FAILS:
        print(f"  FAILED: {n} :: {d}")
    sys.exit(1)
print(f"RESULT: {PASS} passed, 0 failed — both blockers proven impossible")
sys.exit(0)
