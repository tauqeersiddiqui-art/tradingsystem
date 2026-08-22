# engine/core/state_store.py
#
# Runtime state persistence — survives engine-thread restarts AND full
# process restarts so a live position is never orphaned.
#
# Writes data/runtime_state.json atomically (tmp + os.replace) every cycle
# and on every trade transition.  Restored ONLY if the snapshot is from the
# current trading day (a stale file from a previous day is ignored, so PnL /
# trades_today never leak across sessions).

import os
import json
import logging
from datetime import datetime, date

logger = logging.getLogger("state_store")

_STATE_PATH = os.path.join("data", "runtime_state.json")

# Keys persisted for an open position.  Excludes non-serialisable runtime
# objects (_replay) and bulky/ephemeral data (features).
_PERSIST_KEYS = (
    "symbol", "side", "qty", "lot_size", "entry", "stop_loss", "target",
    "max_pnl", "min_pnl", "ml_prob", "regime", "reason", "sl_order_id",
    "entry_quality",
)


def _serialize_position(position):
    if not position:
        return None
    out = {k: position.get(k) for k in _PERSIST_KEYS if k in position}
    ets = position.get("entry_ts")
    if isinstance(ets, datetime):
        out["entry_ts"] = ets.isoformat()
    elif isinstance(ets, str):
        out["entry_ts"] = ets
    return out


def save_state(ctx, position=None, scalp_position=None):
    """Persist pnl, trades_today, closed-pnl list, and open positions."""
    try:
        snap = {
            "session_date": date.today().isoformat(),
            "saved_at":     datetime.now().isoformat(),
            "pnl":          float(getattr(ctx, "pnl", 0.0)),
            "trades_today": int(getattr(ctx, "trades_today", 0)),
            "positions":    list(getattr(ctx, "positions", [])),
            "open_position":  _serialize_position(position),
            "scalp_position": _serialize_position(scalp_position),
        }
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        # Per-process temp name + atomic os.replace => readers never observe a
        # half-written file, and concurrent writers can't corrupt each other's
        # temp (last complete snapshot wins).
        tmp = f"{_STATE_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f, indent=2)
            f.flush()
            os.fsync(f.fileno())          # durable before the rename
        os.replace(tmp, _STATE_PATH)      # atomic swap
    except Exception as e:
        logger.warning(f"[STATE] save failed: {e}")


def load_state():
    """Return the saved snapshot dict if it exists AND is from today, else {}."""
    try:
        if not os.path.exists(_STATE_PATH):
            return {}
        with open(_STATE_PATH) as f:
            snap = json.load(f)
        if snap.get("session_date") != date.today().isoformat():
            logger.info("[STATE] snapshot is from a previous day — ignored")
            return {}
        return snap
    except Exception as e:
        logger.warning(f"[STATE] load failed: {e}")
        return {}


def deserialize_position(d):
    """Rebuild a position dict from a saved snapshot (parses entry_ts back to datetime)."""
    if not d:
        return None
    pos = dict(d)
    ets = pos.get("entry_ts")
    if isinstance(ets, str):
        try:
            pos["entry_ts"] = datetime.fromisoformat(ets)
        except Exception:
            pos["entry_ts"] = datetime.now()
    return pos
