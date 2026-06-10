# engine/analytics/trade_replay.py
#
# F1 — Trade Replay Engine.
# Captures a real-time event timeline for one live trade.
# Attached to position["_replay"] by master_runner.py.
# Zero trading logic — purely observational writes.

import os
import json
import logging
from datetime import datetime

logger = logging.getLogger("trade_replay")

_REPLAY_DIR = os.path.join("data", "analytics", "replays")

# Ladder levels mirror profit_manager.py (for labelling only)
_LADDER_LABELS = [
    (40.0, "TRAIL_80%"),
    (25.0, "TRAIL_65%"),
    (15.0, "TRAIL_40%"),
    (8.0,  "LOCK_RS400"),
    (4.0,  "LOCK_RS200"),
    (2.0,  "BREAK_EVEN"),
]


def _ladder_label(peak_pts: float) -> str:
    for threshold, label in _LADDER_LABELS:
        if peak_pts >= threshold:
            return label
    return "INITIAL_STOP"


class TradeReplay:
    """
    Captures a per-trade event timeline during live execution.
    Usage in master_runner (observational only):
        position["_replay"] = TradeReplay(position, ts, nifty_spot, market_state)
        position["_replay"].on_tick(ts, pos_ltp, position)   # every cycle
        position["_replay"].on_exit(ts, exit_price, reason, pnl, mae_pts)
        position["_replay"].save(trade_id_str)
        msg = position["_replay"].format_timeline()
    """

    def __init__(
        self,
        position: dict,
        entry_ts: datetime,
        nifty_spot: float,
        market_state: dict,
    ):
        os.makedirs(_REPLAY_DIR, exist_ok=True)
        self._entry_ts   = entry_ts
        self._entry      = position.get("entry", 0.0)
        self._qty        = max(position.get("qty", 1), 1)
        self._symbol     = position.get("symbol", "")
        self._side       = position.get("side", "")
        self._last_stop  = position.get("stop_loss", 0.0)
        self._last_mfe   = 0.0

        ms = market_state or {}
        self.events: list = [{
            "t":      entry_ts.strftime("%H:%M:%S"),
            "type":   "ENTRY",
            "price":  round(self._entry, 2),
            "spot":   round(nifty_spot, 2),
            "ml_ce":  round(ms.get("ce_adj", 0.0), 3),
            "ml_pe":  round(ms.get("pe_adj", 0.0), 3),
            "adx":    round(ms.get("adx", 0.0), 1),
            "rsi":    round(ms.get("rsi_1m", 50.0), 1),
            "vwap":   "above" if ms.get("price_vs_vwap", 0) >= 0 else "below",
            "st_dir": int(ms.get("supertrend_dir", 0)),
            "stop":   round(self._last_stop, 2),
            "regime": position.get("regime", ""),
        }]

    # ── live hooks ────────────────────────────────────────────────────

    def on_tick(self, ts: datetime, pos_ltp: float, position: dict):
        """Call every engine cycle while the position is open."""
        mfe_pts      = position.get("max_pnl", 0.0) / self._qty
        current_stop = position.get("stop_loss", self._last_stop)

        # Record every significant new MFE peak (≥0.5 pt steps)
        if mfe_pts >= self._last_mfe + 0.5:
            self._last_mfe = mfe_pts
            self.events.append({
                "t":       ts.strftime("%H:%M:%S"),
                "type":    "MFE_PEAK",
                "mfe_pts": round(mfe_pts, 2),
                "price":   round(pos_ltp, 2),
            })

        # Record stop-level changes (ladder activations)
        if abs(current_stop - self._last_stop) > 0.01:
            diff = round(current_stop - self._entry, 2)
            self.events.append({
                "t":          ts.strftime("%H:%M:%S"),
                "type":       "STOP_MOVE",
                "label":      _ladder_label(mfe_pts),
                "new_stop":   round(current_stop, 2),
                "pts_locked": diff,
                "mfe_pts":    round(mfe_pts, 2),
            })
            self._last_stop = current_stop

    def on_exit(
        self,
        ts: datetime,
        exit_price: float,
        exit_reason: str,
        pnl: float,
        mae_pts: float,
    ):
        """Call once at trade exit to close the timeline."""
        self.events.append({
            "t":        ts.strftime("%H:%M:%S"),
            "type":     "EXIT",
            "price":    round(exit_price, 2),
            "reason":   exit_reason,
            "pnl":      round(pnl, 2),
            "mfe_pts":  round(self._last_mfe, 2),
            "mae_pts":  round(mae_pts, 2),
        })

    # ── output ────────────────────────────────────────────────────────

    def format_timeline(self) -> str:
        header = (
            f"⏱ <b>TRADE REPLAY — {self._side} {self._symbol[-10:]}</b>\n"
        )
        lines = []
        for ev in self.events:
            t     = ev["t"]
            etype = ev["type"]

            if etype == "ENTRY":
                st  = "↑" if ev["st_dir"] > 0 else ("↓" if ev["st_dir"] < 0 else "–")
                lines.append(
                    f"<code>{t}</code> 📥 ENTRY {ev['price']:.1f}  "
                    f"CE {ev['ml_ce']:.2f} PE {ev['ml_pe']:.2f}  "
                    f"ADX {ev['adx']:.0f} RSI {ev['rsi']:.0f}  "
                    f"ST {st} VWAP {ev['vwap']}"
                )

            elif etype == "MFE_PEAK":
                lines.append(
                    f"<code>{t}</code> 📈 MFE {ev['mfe_pts']:+.1f} pts  "
                    f"@ {ev['price']:.1f}"
                )

            elif etype == "STOP_MOVE":
                sign   = "+" if ev["pts_locked"] >= 0 else ""
                lines.append(
                    f"<code>{t}</code> 🔒 {ev['label']}  "
                    f"SL → {ev['new_stop']:.1f}  "
                    f"({sign}{ev['pts_locked']:.1f} pts locked)"
                )

            elif etype == "EXIT":
                e    = "✅" if ev["pnl"] >= 0 else "❌"
                sign = "+" if ev["pnl"] >= 0 else ""
                lines.append(
                    f"<code>{t}</code> {e} EXIT {ev['price']:.1f}  "
                    f"{ev['reason']}  "
                    f"{sign}₹{abs(ev['pnl']):,.0f}  "
                    f"MFE {ev['mfe_pts']:+.1f}  MAE {ev['mae_pts']:+.1f}"
                )

        return header + "\n".join(lines)

    # ── persistence ───────────────────────────────────────────────────

    def save(self, trade_id: str = ""):
        """Write JSON replay file for offline review."""
        try:
            stamp = self._entry_ts.strftime("%Y%m%d_%H%M%S")
            fname = f"{stamp}_{self._side}_{trade_id}.json"
            with open(os.path.join(_REPLAY_DIR, fname), "w") as f:
                json.dump({
                    "symbol": self._symbol,
                    "side":   self._side,
                    "entry":  self._entry,
                    "qty":    self._qty,
                    "events": self.events,
                }, f, indent=2)
        except Exception as exc:
            logger.warning(f"[REPLAY] Save failed: {exc}")

    @staticmethod
    def load_latest(n: int = 5) -> list:
        """Return the N most recent saved replays as dicts."""
        if not os.path.isdir(_REPLAY_DIR):
            return []
        files = sorted(
            (f for f in os.listdir(_REPLAY_DIR) if f.endswith(".json")),
            reverse=True,
        )[:n]
        out = []
        for fname in files:
            try:
                with open(os.path.join(_REPLAY_DIR, fname)) as f:
                    out.append(json.load(f))
            except Exception:
                pass
        return out
