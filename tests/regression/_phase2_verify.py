# data/_phase2_verify.py
# Phase 2 — Execution Truth Layer: adversarial verification.
# Proves the invariant: a position is created/removed ONLY on broker-confirmed
# ST_COMPLETE; unknown/otherwise never creates or destroys a position; fills are
# NEVER fabricated.
# Run:  python data/_phase2_verify.py

import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.execution.execution_engine import (
    ExecutionEngine, _normalize_status,
    ST_COMPLETE, ST_REJECTED, ST_CANCELLED, ST_OPEN, ST_PARTIAL, ST_TIMEOUT,
    ST_UNKNOWN, _FILL_POLL_INTERVAL,
)
from engine.execution.broker import BrokerStateError

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── Fake kite object ──────────────────────────────────────────────────────
class FakeKite:
    class _V:  # kite constants
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

    def __init__(self, order_states=None, fail_orders_call=False, fail_place=False):
        # order_states: dict order_id -> {"status","average_price","filled_quantity"}
        self.order_states = order_states or {}
        self.fail_orders_call = fail_orders_call
        self.fail_place = fail_place
        self.placed = []
        self.next_oid = 1000

    def orders(self):
        if self.fail_orders_call:
            raise ConnectionError("simulated outage")
        return [{"order_id": oid, **st} for oid, st in self.order_states.items()]

    def place_order(self, **kw):
        if self.fail_place:
            raise ConnectionError("simulated placement failure")
        oid = f"oid_{self.next_oid}"
        self.next_oid += 1
        self.placed.append(oid)
        return oid


class FakeBroker:
    def __init__(self, kite, ltp_val=0.0):
        self.kite = kite
        self._ltp = ltp_val
        self.is_paper = False

    def ltp(self, symbol=None):
        return self._ltp

    def get_quote_snapshot(self, symbol):
        return {"ltp": self._ltp, "bid": self._ltp - 0.05, "ask": self._ltp + 0.05}


def make_engine(kite, dry=False, ltp=10.0):
    cfg = type("C", (), {"DRY_RUN": dry})()
    broker = FakeBroker(kite, ltp)
    e = ExecutionEngine.__new__(ExecutionEngine)
    e.broker = broker
    e.config = cfg
    e._active_order_id = None
    return e


# ── 1. _normalize_status mapping ─────────────────────────────────────────
print("[1] _normalize_status")
check("COMPLETE -> COMPLETE", _normalize_status("COMPLETE", 0) == ST_COMPLETE)
check("REJECTED -> REJECTED", _normalize_status("REJECTED", 0) == ST_REJECTED)
check("CANCELLED -> CANCELLED", _normalize_status("CANCELLED", 0) == ST_CANCELLED)
check("CANCEL_PENDING -> CANCELLED", _normalize_status("CANCEL_PENDING", 0) == ST_CANCELLED)
check("OPEN -> OPEN", _normalize_status("OPEN", 0) == ST_OPEN)
check("TRIGGER PENDING -> OPEN", _normalize_status("TRIGGER PENDING", 0) == ST_OPEN)
check("OPEN + filled qty -> PARTIAL", _normalize_status("OPEN", 3) == ST_PARTIAL)
check("garbage status -> UNKNOWN (never guessed)", _normalize_status("WAT", 0) == ST_UNKNOWN)

# ── 2. _order_status resolution (no fabrication) ─────────────────────────
print("[2] _order_status never fabricates")
# 2a COMPLETE with avg
kite = FakeKite({"oid_1": {"status": "COMPLETE", "average_price": 103.5, "filled_quantity": 30}})
e = make_engine(kite)
st, avg, ts, _fq = e._order_status("oid_1", timeout_attempts=1)
check("COMPLETE+avg -> (COMPLETE, avg)", st == ST_COMPLETE and abs(avg - 103.5) < 1e-9 and ts is not None and _fq == 30)

# 2b COMPLETE with avg=0 -> UNKNOWN (never guess a price)
kite = FakeKite({"oid_1": {"status": "COMPLETE", "average_price": 0, "filled_quantity": 30}})
e = make_engine(kite)
st, avg, ts, _fq = e._order_status("oid_1", timeout_attempts=1)
check("COMPLETE+avg0 -> UNKNOWN (no guess)", st == ST_UNKNOWN)

# 2c REJECTED
kite = FakeKite({"oid_1": {"status": "REJECTED", "average_price": 0, "filled_quantity": 0}})
e = make_engine(kite)
st, avg, ts, _fq = e._order_status("oid_1", timeout_attempts=1)
check("REJECTED -> (REJECTED, 0, None)", st == ST_REJECTED and avg == 0 and ts is None)

# 2d CANCELLED
kite = FakeKite({"oid_1": {"status": "CANCELLED", "average_price": 0, "filled_quantity": 0}})
e = make_engine(kite)
st, avg, ts, _fq = e._order_status("oid_1", timeout_attempts=1)
check("CANCELLED -> (CANCELLED, 0, None)", st == ST_CANCELLED)

# 2e still OPEN after poll -> TIMEOUT (fill None, NOT a fallback price)
kite = FakeKite({"oid_1": {"status": "OPEN", "average_price": 0, "filled_quantity": 0}})
e = make_engine(kite)
st, avg, ts, _fq = e._order_status("oid_1", timeout_attempts=2)
check("OPEN after poll -> TIMEOUT, fill None", st == ST_TIMEOUT and avg is None)

# 2f broker outage -> UNKNOWN
kite = FakeKite(fail_orders_call=True)
e = make_engine(kite)
st, avg, ts, _fq = e._order_status("oid_1", timeout_attempts=1)
check("outage -> UNKNOWN, fill None", st == ST_UNKNOWN and avg is None)

# 2g order placed but not in list -> UNKNOWN
kite = FakeKite({})  # empty list, no oid_9
e = make_engine(kite)
st, avg, ts, _fq = e._order_status("oid_9", timeout_attempts=1)
check("not-found -> UNKNOWN (never guess)", st == ST_UNKNOWN)

# ── 2b. PARTIAL FILL — filled_qty surfaced, never oversold ──────────────
print("[2b] partial-fill handling")
kite = FakeKite({"oid_1": {"status": "OPEN", "average_price": 99.0, "filled_quantity": 15}})
e = make_engine(kite)
st, avg, ts, fq = e._order_status("oid_1", timeout_attempts=1)
check("PARTIAL(15) -> TIMEOUT(not terminal), fill None, fq=15 surfaced", st == ST_TIMEOUT and avg is None and fq == 15)

kite = FakeKite({"oid_1": {"status": "CANCELLED", "average_price": 99.5, "filled_quantity": 10}})
e = make_engine(kite)
st, avg, ts, fq = e._order_status("oid_1", timeout_attempts=1)
check("CANCELLED-after-partial(10) -> (CANCELLED, 99.5, None, 10)", st == ST_CANCELLED and abs(avg - 99.5) < 1e-9 and fq == 10)

# exit position-shrink mirror: never oversell a fresh retry after partial close
def exit_partial_shrink(qty, fq):
    return max(0, qty - fq)
check("partial exit shrink 30-10=20", exit_partial_shrink(30, 10) == 20)
check("partial exit full close 10-10=0", exit_partial_shrink(10, 10) == 0)
check("partial exit never negative", exit_partial_shrink(5, 10) == 0)

# ── 3. execute_entry — never returns a guessed fill ──────────────────────
print("[3] execute_entry contract")
# 3a COMPLETE entry -> real price
kite = FakeKite({"oid_1000": {"status": "COMPLETE", "average_price": 104.0, "filled_quantity": 30}})
e = make_engine(kite, ltp=10.0)
r = e.execute_entry("SYM", "CE", 30)
check("entry COMPLETE -> state COMPLETE, price=104.0", r and r["state"] == ST_COMPLETE and abs(r["price"] - 104.0) < 1e-9)

# 3b REJECTED entry -> state REJECTED, NO price (no phantom)
kite = FakeKite({"oid_1000": {"status": "REJECTED", "average_price": 0, "filled_quantity": 0}})
e = make_engine(kite)
r = e.execute_entry("SYM", "CE", 30)
check("entry REJECTED -> state REJECTED, price None", r and r["state"] == ST_REJECTED and r.get("price") is None)

# 3c OPEN (timeout) entry -> TIMEOUT, NO price (this was the PHANTOM before)
kite = FakeKite({"oid_1000": {"status": "OPEN", "average_price": 0, "filled_quantity": 0}})
e = make_engine(kite)
r = e.execute_entry("SYM", "CE", 30)
check("entry OPEN(timeout) -> TIMEOUT, price None (was phantom fill)", r and r["state"] == ST_TIMEOUT and r.get("price") is None)
check("entry OPEN(timeout) -> order_id retained for reconcile", r and r.get("order_id") == "oid_1000")

# 3d outage during poll -> UNKNOWN, NO price
kite = FakeKite(fail_orders_call=True)
e = make_engine(kite)
r = e.execute_entry("SYM", "CE", 30)
check("entry outage -> UNKNOWN, price None", r and r["state"] == ST_UNKNOWN and r.get("price") is None)

# 3e placement failure -> REJECTED (no order_id, safe to retry fresh)
kite = FakeKite(fail_place=True)
e = make_engine(kite)
r = e.execute_entry("SYM", "CE", 30)
check("entry placement failure -> REJECTED, no order_id", r and r["state"] == ST_REJECTED and not r.get("order_id"))

# ── 4. execute_exit — never clears on unconfirmed ────────────────────────
print("[4] execute_exit contract")
# 4a COMPLETE exit -> real price
kite = FakeKite({"oid_1000": {"status": "COMPLETE", "average_price": 98.0, "filled_quantity": 30}})
e = make_engine(kite)
r = e.execute_exit("SYM", 30, "CE")
check("exit COMPLETE -> state COMPLETE, price=98.0", r and r["state"] == ST_COMPLETE and abs(r["price"] - 98.0) < 1e-9)

# 4b REJECTED exit -> REJECTED, price None (no ghost-close)
kite = FakeKite({"oid_1000": {"status": "REJECTED", "average_price": 0, "filled_quantity": 0}})
e = make_engine(kite)
r = e.execute_exit("SYM", 30, "CE")
check("exit REJECTED -> REJECTED, price None", r and r["state"] == ST_REJECTED and r.get("price") is None)

# 4c OPEN(timeout) exit -> TIMEOUT, price None (was ghost-close before)
kite = FakeKite({"oid_1000": {"status": "OPEN", "average_price": 0, "filled_quantity": 0}})
e = make_engine(kite)
r = e.execute_exit("SYM", 30, "CE")
check("exit OPEN(timeout) -> TIMEOUT, price None (was ghost-close)", r and r["state"] == ST_TIMEOUT and r.get("price") is None)
check("exit OPEN(timeout) -> order_id retained", r and r.get("order_id") == "oid_1000")

# 4d placement failure -> REJECTED (nothing in flight, safe retry)
kite = FakeKite(fail_place=True)
e = make_engine(kite)
r = e.execute_exit("SYM", 30, "CE")
check("exit placement failure -> REJECTED", r and r["state"] == ST_REJECTED and not r.get("order_id"))

# ── 5. Position creation/removal gating (master_runner logic mirror) ─────
print("[5] position create/remove gating")
# mirror: position is created only when state == COMPLETE
def entry_gate(order):
    if order and order.get("state") == ST_COMPLETE:
        return ("CREATE", order.get("price"))
    if order and order.get("order_id"):
        return ("PENDING", None)
    return ("NONE", None)

check("COMPLETE entry -> CREATE position", entry_gate({"state": ST_COMPLETE, "price": 104.0}) == ("CREATE", 104.0))
check("TIMEOUT entry -> PENDING (no position)", entry_gate({"state": ST_TIMEOUT, "order_id": "x"}) == ("PENDING", None))
check("REJECTED entry -> PENDING->abandon (no position)", entry_gate({"state": ST_REJECTED, "order_id": "x"})[0] == "PENDING")
check("UNKNOWN entry -> PENDING (no position)", entry_gate({"state": ST_UNKNOWN, "order_id": "x"}) == ("PENDING", None))
check("placement failure (no oid) -> NONE", entry_gate({"state": ST_REJECTED}) == ("NONE", None))

# mirror: position is cleared only when state == COMPLETE
def exit_gate(order):
    if order and order.get("state") == ST_COMPLETE:
        return ("CLEAR", order.get("price"))
    if order and order.get("order_id"):
        return ("PENDING", None)
    return ("KEEP", None)

check("COMPLETE exit -> CLEAR position", exit_gate({"state": ST_COMPLETE, "price": 98.0}) == ("CLEAR", 98.0))
check("TIMEOUT exit -> PENDING (position kept)", exit_gate({"state": ST_TIMEOUT, "order_id": "y"}) == ("PENDING", None))
check("REJECTED exit -> PENDING (position kept)", exit_gate({"state": ST_REJECTED, "order_id": "y"})[0] == "PENDING")
check("UNKNOWN exit -> PENDING (position kept)", exit_gate({"state": ST_UNKNOWN, "order_id": "y"}) == ("PENDING", None))
check("placement failure (no oid) -> KEEP position", exit_gate({"state": ST_REJECTED}) == ("KEEP", None))

# ── 6. duplicate-order guard still holds ─────────────────────────────────
print("[6] duplicate-order guard")
kite = FakeKite({"oid_1000": {"status": "COMPLETE", "average_price": 104.0, "filled_quantity": 30}})
e = make_engine(kite)
e._active_order_id = "oid_1000"
r = e.execute_entry("SYM", "CE", 30)
check("active order -> second entry blocked (None)", r is None)
e._active_order_id = None
r = e.execute_entry("SYM", "CE", 30)
check("guard cleared -> entry allowed", r is not None)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
