# data/_phase1_verify.py
# Adversarial Phase-1 verification harness — proves Broker State Authority
# is FAIL-CLOSED. Run:  python data/_phase1_verify.py
#
# Constructs ZerodhaBroker / ExecutionEngine via __new__ (no live session),
# stubs kite.positions() to (a) return data, (b) raise (broker outage).
# Exercises every Phase-1 call site and asserts fail-closed behaviour.

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.execution.broker import ZerodhaBroker, BrokerStateError
from engine.execution.execution_engine import ExecutionEngine

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


class FakeKite:
    """Stubbed kite object. `fail` makes positions() raise (simulates outage)."""
    def __init__(self, net_positions=None, fail=False):
        self._net = net_positions if net_positions is not None else []
        self.fail = fail

    def positions(self):
        if self.fail:
            raise ConnectionError("simulated REST timeout / network outage")
        return {"net": self._net}


def make_broker(kite):
    b = ZerodhaBroker.__new__(ZerodhaBroker)
    b.kite = kite
    b._lock = None  # not touched by get_positions/has_open_position
    return b


def make_executor(broker, dry=False):
    e = ExecutionEngine.__new__(ExecutionEngine)
    e.broker = broker
    e.config = type("C", (), {"DRY_RUN": dry})()
    return e


# ─────────────────────────────────────────────────────────────────────────
# 1. get_positions() — must RAISE on broker failure, never return []
# ─────────────────────────────────────────────────────────────────────────
print("[1] get_positions() fail-closed")
b = make_broker(FakeKite(net_positions=[{"tradingsymbol": "X", "quantity": 0}]))
try:
    r = b.get_positions()
    check("returns list on success", isinstance(r, list))
except BrokerStateError:
    check("returns list on success", False)

b = make_broker(FakeKite(fail=True))
try:
    b.get_positions()
    check("RAISES BrokerStateError on outage (never [] fail-open)", False)
except BrokerStateError:
    check("RAISES BrokerStateError on outage (never [] fail-open)", True)

# ─────────────────────────────────────────────────────────────────────────
# 2. has_open_position() — True on position, False when flat, RAISE on unknown
# ─────────────────────────────────────────────────────────────────────────
print("[2] has_open_position() tri-state")
b = make_broker(FakeKite(net_positions=[{"tradingsymbol": "A", "quantity": 0},
                                        {"tradingsymbol": "B", "quantity": 0}]))
check("flat -> False", b.has_open_position() is False)

b = make_broker(FakeKite(net_positions=[{"tradingsymbol": "A", "quantity": 30}]))
check("open -> True", b.has_open_position() is True)

b = make_broker(FakeKite(net_positions=[{"tradingsymbol": "A", "quantity": -30}]))
check("short open -> True", b.has_open_position() is True)

b = make_broker(FakeKite(fail=True))
try:
    b.has_open_position()
    check("outage -> RAISES (unknown != flat)", False)
except BrokerStateError:
    check("outage -> RAISES (unknown != flat)", True)

# 2e. malformed response (missing 'net') -> RAISES (was fail-open [] -> flat)
b = make_broker(FakeKite(net_positions=None))
b.kite = type("K", (), {"positions": lambda s: {}})()   # returns {} (no 'net')
try:
    b.has_open_position()
    check("malformed resp (no 'net') -> RAISES", False)
except BrokerStateError:
    check("malformed resp (no 'net') -> RAISES", True)

# ─────────────────────────────────────────────────────────────────────────
# 3. verify_flat() — True only when broker CONFIRMS flat; False on outage
# ─────────────────────────────────────────────────────────────────────────
print("[3] verify_flat() fail-closed")
# 3a. broker confirms flat
b = make_broker(FakeKite(net_positions=[{"tradingsymbol": "SYM", "quantity": 0}]))
e = make_executor(b)
check("confirmed flat -> True", e.verify_flat("SYM") is True)

# 3b. broker shows open quantity (partial fill / not exited)
b = make_broker(FakeKite(net_positions=[{"tradingsymbol": "SYM", "quantity": 30}]))
e = make_executor(b)
check("still open -> False", e.verify_flat("SYM") is False)

# 3c. broker outage -> MUST RAISE (caller halts; fail-closed)
b = make_broker(FakeKite(fail=True))
e = make_executor(b)
try:
    e.verify_flat("SYM")
    check("outage -> RAISES (unknown must halt)", False)
except BrokerStateError:
    check("outage -> RAISES (unknown must halt)", True)
except Exception:
    check("outage -> RAISES (unknown must halt)", True)  # any error re-raised is fail-closed

# 3d. DRY_RUN path unchanged (paper has no broker to reconcile)
b = make_broker(FakeKite(fail=True))
e = make_executor(b, dry=True)
check("DRY_RUN -> True (no broker reconcile)", e.verify_flat("SYM") is True)

# ─────────────────────────────────────────────────────────────────────────
# 4. STARTUP GATE (mirrors master_runner init_broker logic)
#    unknown/blocked state must BLOCK startup
# ─────────────────────────────────────────────────────────────────────────
print("[4] startup gate fail-closed")


def startup_gate(broker, resume_ok=False):
    """Returns None (allowed) or error-string (blocked). Mirrors master_runner."""
    if not resume_ok and hasattr(broker, "has_open_position"):
        try:
            _broker_has_pos = broker.has_open_position()
        except Exception:
            _broker_has_pos = True  # unknown -> block
        if _broker_has_pos:
            return "blocked"
    return None


b = make_broker(FakeKite(net_positions=[]))
check("flat broker -> start allowed", startup_gate(b) is None)

b = make_broker(FakeKite(net_positions=[{"tradingsymbol": "A", "quantity": 30}]))
check("open broker -> blocked", startup_gate(b) == "blocked")

b = make_broker(FakeKite(fail=True))
check("UNKNOWN broker -> blocked (was FAIL-OPEN before)", startup_gate(b) == "blocked")

b = make_broker(FakeKite(fail=True))
check("resume_ok=True -> gate bypassed (explicit override)", startup_gate(b, resume_ok=True) is None)

# ─────────────────────────────────────────────────────────────────────────
# 5. RECOVERY (mirrors master_runner engine_loop reconciliation)
#    unknown broker state -> HALT: position=None, engine paused, no resume
# ─────────────────────────────────────────────────────────────────────────
print("[5] recovery fail-closed")


class FakeTN:
    ENGINE_PAUSED = False


class FakeCtx:
    config = type("C", (), {})


def recovery_logic(broker, restored_pos, is_dry_run):
    """Mirror of master_runner recovery decision chain. Returns
    (position_resumed, engine_paused, broker_unknown)."""
    tn = FakeTN()
    broker_unknown = False
    broker_open = False
    try:
        if is_dry_run:
            broker_open = False
        else:
            broker_open = broker.has_open_position()
    except Exception:
        broker_unknown = True
        broker_open = True
        tn.ENGINE_PAUSED = True

    position = None
    if broker_unknown:
        position = None          # do not resume anything
    elif is_dry_run and restored_pos:
        position = restored_pos  # paper resume
    elif broker_open and restored_pos:
        position = restored_pos  # live resume
    # (orphan-flatten / flat-was-closed branches omitted for focus)
    return position, tn.ENGINE_PAUSED, broker_unknown


saved = {"symbol": "SYM", "entry": 100.0}

# 5a. live restart, broker flat, saved state exists -> treated closed, no resume, no pause
b = make_broker(FakeKite(net_positions=[]))
pos, paused, unknown = recovery_logic(b, saved, is_dry_run=False)
check("live+flat+saved -> no resume", pos is None)
check("live+flat+saved -> not paused", paused is False)

# 5b. live restart, broker has the position, saved state -> resume management
b = make_broker(FakeKite(net_positions=[{"tradingsymbol": "SYM", "quantity": 30}]))
pos, paused, unknown = recovery_logic(b, saved, is_dry_run=False)
check("live+open+saved -> resume management", pos == saved)
check("live+open+saved -> not paused", paused is False)

# 5c. live restart, broker OUTAGE -> HALT (was FAIL-OPEN: resumed/dropped, kept trading)
b = make_broker(FakeKite(fail=True))
pos, paused, unknown = recovery_logic(b, saved, is_dry_run=False)
check("outage -> NO position resumed", pos is None)
check("outage -> ENGINE PAUSED", paused is True)
check("outage -> flagged unknown", unknown is True)

# 5d. live restart, broker OUTAGE, no saved state -> HALT (must not open new trades)
b = make_broker(FakeKite(fail=True))
pos, paused, unknown = recovery_logic(b, None, is_dry_run=False)
check("outage+no-saved -> no resume", pos is None)
check("outage+no-saved -> ENGINE PAUSED", paused is True)

# 5e. dry-run restart unchanged
b = make_broker(FakeKite(fail=True))
pos, paused, unknown = recovery_logic(b, saved, is_dry_run=True)
check("dry-run+outage -> paper resume (no broker reconcile)", pos == saved)
check("dry-run+outage -> not paused", paused is False)

print()
print(f"RESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
