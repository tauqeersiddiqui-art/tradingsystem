from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


DECISION_FIELDS = [
    "decision_id",
    "timestamp",
    "symbol",
    "direction",
    "regime",
    "confidence",
    "ml_probability",
    "recommendation",
    "allow_trade",
    "blocking_reason",
    "applied_filters",
]

OUTCOME_FIELDS = [
    "decision_id",
    "timestamp",
    "symbol",
    "direction",
    "phase55_decision",
    "pnl",
    "exit_reason",
    "outcome_class",
    "estimated_pnl_saved",
    "estimated_pnl_missed",
]


def _iso_ts(value: datetime | None = None) -> str:
    return (value or datetime.now()).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def empty_phase55_telemetry_snapshot(enabled: bool = False) -> dict[str, Any]:
    return {
        "telemetry_enabled": bool(enabled),
        "trades_evaluated": 0,
        "trades_allowed": 0,
        "trades_blocked": 0,
        "blocked_by_ce_threshold": 0,
        "blocked_by_pe_threshold": 0,
        "blocked_by_mixed_regime": 0,
        "block_percentage": 0.0,
        "correct_blocks": 0,
        "false_positive_blocks": 0,
        "correct_block_rate": 0.0,
        "false_block_rate": 0.0,
        "estimated_pnl_saved": 0.0,
        "estimated_pnl_missed": 0.0,
    }


class Phase55Telemetry:
    """
    Observational telemetry only. This class never decides whether to trade.
    """

    def __init__(self, *, base_dir: str | Path = "data/phase55", session_date: date | None = None):
        self.base_dir = Path(base_dir)
        self.session_date = session_date or date.today()
        self.session_key = self.session_date.strftime("%Y%m%d")
        self.decisions_path = self.base_dir / f"phase55_decisions_{self.session_key}.csv"
        self.outcomes_path = self.base_dir / f"phase55_outcomes_{self.session_key}.csv"
        self.report_dir = self.base_dir / "reports"

        self.trades_evaluated = 0
        self.trades_allowed = 0
        self.trades_blocked = 0
        self.blocked_by_ce_threshold = 0
        self.blocked_by_pe_threshold = 0
        self.blocked_by_mixed_regime = 0

        self.correct_blocks = 0
        self.false_positive_blocks = 0
        self.estimated_pnl_saved = 0.0
        self.estimated_pnl_missed = 0.0

        self.blocking_reasons: Counter[str] = Counter()
        self.blocked_regimes: Counter[str] = Counter()
        self._decisions: dict[str, dict[str, Any]] = {}
        self._open_shadows: dict[str, dict[str, Any]] = {}
        self._next_id = 1

    @classmethod
    def from_config(cls, config: object | None) -> "Phase55Telemetry":
        base_dir = getattr(config, "PHASE55_TELEMETRY_DIR", "data/phase55")
        return cls(base_dir=base_dir)

    def record_decision(
        self,
        *,
        timestamp: datetime | None,
        symbol: str,
        direction: str,
        regime: str,
        confidence: float,
        ml_probability: float,
        recommendation: str,
        allow_trade: bool,
        blocking_reason: str,
        applied_filters: Iterable[str],
    ) -> str:
        decision_id = f"P55-{self.session_key}-{self._next_id:05d}"
        self._next_id += 1

        filters = [str(item) for item in (applied_filters or [])]
        record = {
            "decision_id": decision_id,
            "timestamp": _iso_ts(timestamp),
            "symbol": str(symbol or "BANKNIFTY"),
            "direction": str(direction or "").upper(),
            "regime": str(regime or "UNKNOWN"),
            "confidence": round(_safe_float(confidence), 6),
            "ml_probability": round(_safe_float(ml_probability), 6),
            "recommendation": str(recommendation or ""),
            "allow_trade": bool(allow_trade),
            "blocking_reason": str(blocking_reason or ""),
            "applied_filters": "|".join(filters),
        }
        self._decisions[decision_id] = dict(record)
        self._append_csv(self.decisions_path, DECISION_FIELDS, record)

        self.trades_evaluated += 1
        if allow_trade:
            self.trades_allowed += 1
        else:
            self.trades_blocked += 1
            reason = record["blocking_reason"] or "UNKNOWN"
            self.blocking_reasons[reason] += 1
            self.blocked_regimes[record["regime"]] += 1
            self._count_block_bucket(reason, filters)

        return decision_id

    def update_actual_entry(
        self,
        decision_id: str | None,
        *,
        symbol: str,
        entry_price: float,
        quantity: int,
        timestamp: datetime | None,
    ) -> None:
        if not decision_id or decision_id not in self._decisions:
            return
        record = self._decisions[decision_id]
        record["symbol"] = str(symbol or record.get("symbol") or "UNKNOWN")
        record["actual_entry_price"] = round(_safe_float(entry_price), 4)
        record["actual_quantity"] = int(quantity or 0)
        record["actual_entry_ts"] = _iso_ts(timestamp)

    def attach_shadow_entry(
        self,
        decision_id: str | None,
        *,
        symbol: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        target: float,
        timestamp: datetime | None,
        max_hold_seconds: int,
    ) -> bool:
        if not decision_id or decision_id not in self._decisions:
            return False
        if decision_id in self._open_shadows:
            return False

        record = self._decisions[decision_id]
        if bool(record.get("allow_trade")):
            return False

        entry = _safe_float(entry_price)
        if entry <= 0:
            return False

        record["symbol"] = str(symbol or record.get("symbol") or "UNKNOWN")
        self._open_shadows[decision_id] = {
            "decision_id": decision_id,
            "symbol": record["symbol"],
            "direction": record.get("direction", ""),
            "entry_price": entry,
            "quantity": max(int(quantity or 0), 1),
            "stop_loss": _safe_float(stop_loss),
            "target": _safe_float(target),
            "entry_ts": timestamp or datetime.now(),
            "last_price": entry,
            "max_hold_seconds": max(int(max_hold_seconds or 0), 1),
        }
        return True

    def open_shadow_symbols(self) -> list[str]:
        return sorted({str(item["symbol"]) for item in self._open_shadows.values() if item.get("symbol")})

    def observe_shadow_price(self, *, symbol: str, price: float, timestamp: datetime | None) -> None:
        ltp = _safe_float(price)
        if ltp <= 0:
            return
        ts = timestamp or datetime.now()
        to_close: list[tuple[str, float, str]] = []
        for decision_id, shadow in list(self._open_shadows.items()):
            if shadow.get("symbol") != symbol:
                continue
            shadow["last_price"] = ltp
            entry_ts = shadow.get("entry_ts") or ts
            held = (ts - entry_ts).total_seconds()
            if shadow.get("target", 0.0) > 0 and ltp >= shadow["target"]:
                to_close.append((decision_id, ltp, "SHADOW_TARGET"))
            elif shadow.get("stop_loss", 0.0) > 0 and ltp <= shadow["stop_loss"]:
                to_close.append((decision_id, ltp, "SHADOW_STOP"))
            elif held >= shadow.get("max_hold_seconds", 1):
                to_close.append((decision_id, ltp, "SHADOW_TIME_EXIT"))

        for decision_id, exit_price, reason in to_close:
            self.close_shadow(decision_id, exit_price=exit_price, exit_reason=reason, timestamp=ts)

    def close_all_open_shadows(self, *, timestamp: datetime | None, exit_reason: str = "SHADOW_EOD_MARK") -> None:
        ts = timestamp or datetime.now()
        for decision_id, shadow in list(self._open_shadows.items()):
            exit_price = _safe_float(shadow.get("last_price"), _safe_float(shadow.get("entry_price")))
            self.close_shadow(decision_id, exit_price=exit_price, exit_reason=exit_reason, timestamp=ts)

    def close_shadow(
        self,
        decision_id: str,
        *,
        exit_price: float,
        exit_reason: str,
        timestamp: datetime | None,
    ) -> None:
        shadow = self._open_shadows.pop(decision_id, None)
        record = self._decisions.get(decision_id)
        if not shadow or not record:
            return

        pnl = (_safe_float(exit_price) - _safe_float(shadow.get("entry_price"))) * max(int(shadow.get("quantity", 1)), 1)
        if pnl > 0:
            outcome_class = "FALSE_POSITIVE_BLOCK"
            self.false_positive_blocks += 1
            self.estimated_pnl_missed += pnl
            saved = 0.0
            missed = pnl
        else:
            outcome_class = "CORRECT_BLOCK"
            self.correct_blocks += 1
            self.estimated_pnl_saved += abs(pnl)
            saved = abs(pnl)
            missed = 0.0

        self._append_csv(
            self.outcomes_path,
            OUTCOME_FIELDS,
            {
                "decision_id": decision_id,
                "timestamp": _iso_ts(timestamp),
                "symbol": record.get("symbol", shadow.get("symbol", "UNKNOWN")),
                "direction": record.get("direction", ""),
                "phase55_decision": "BLOCK",
                "pnl": round(pnl, 2),
                "exit_reason": exit_reason,
                "outcome_class": outcome_class,
                "estimated_pnl_saved": round(saved, 2),
                "estimated_pnl_missed": round(missed, 2),
            },
        )

    def record_actual_outcome(
        self,
        decision_id: str | None,
        *,
        symbol: str,
        direction: str,
        pnl: float,
        exit_reason: str,
        timestamp: datetime | None,
    ) -> None:
        if not decision_id or decision_id not in self._decisions:
            return
        record = self._decisions[decision_id]
        self._append_csv(
            self.outcomes_path,
            OUTCOME_FIELDS,
            {
                "decision_id": decision_id,
                "timestamp": _iso_ts(timestamp),
                "symbol": symbol or record.get("symbol", "UNKNOWN"),
                "direction": direction or record.get("direction", ""),
                "phase55_decision": "ALLOW" if record.get("allow_trade") else "BLOCK",
                "pnl": round(_safe_float(pnl), 2),
                "exit_reason": str(exit_reason or ""),
                "outcome_class": "ACTUAL_ALLOWED_TRADE",
                "estimated_pnl_saved": 0.0,
                "estimated_pnl_missed": 0.0,
            },
        )

    def snapshot(self) -> dict[str, Any]:
        out = empty_phase55_telemetry_snapshot(enabled=True)
        out.update(
            {
                "trades_evaluated": self.trades_evaluated,
                "trades_allowed": self.trades_allowed,
                "trades_blocked": self.trades_blocked,
                "blocked_by_ce_threshold": self.blocked_by_ce_threshold,
                "blocked_by_pe_threshold": self.blocked_by_pe_threshold,
                "blocked_by_mixed_regime": self.blocked_by_mixed_regime,
                "block_percentage": self._pct(self.trades_blocked, self.trades_evaluated),
                "correct_blocks": self.correct_blocks,
                "false_positive_blocks": self.false_positive_blocks,
                "correct_block_rate": self._pct(self.correct_blocks, self._blocked_outcomes()),
                "false_block_rate": self._pct(self.false_positive_blocks, self._blocked_outcomes()),
                "estimated_pnl_saved": round(self.estimated_pnl_saved, 2),
                "estimated_pnl_missed": round(self.estimated_pnl_missed, 2),
            }
        )
        return out

    def generate_eod_report(self, *, timestamp: datetime | None = None) -> dict[str, Any]:
        self.close_all_open_shadows(timestamp=timestamp, exit_reason="SHADOW_EOD_MARK")
        stats = self.snapshot()
        recommendation = self._recommendation(stats)
        top_reasons = self.blocking_reasons.most_common(5)
        top_regimes = self.blocked_regimes.most_common(5)
        payload = {
            "timestamp": _iso_ts(timestamp),
            "session": self.session_key,
            "stats": stats,
            "top_blocking_reasons": top_reasons,
            "regimes_most_frequently_blocked": top_regimes,
            "recommendation": recommendation,
            "decisions_path": str(self.decisions_path),
            "outcomes_path": str(self.outcomes_path),
        }

        self.report_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.report_dir / f"phase55_summary_{self.session_key}.json"
        md_path = self.report_dir / f"phase55_summary_{self.session_key}.md"
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        md_path.write_text(self._format_report(payload), encoding="utf-8")
        payload["json_path"] = str(json_path)
        payload["report_path"] = str(md_path)
        return payload

    def _count_block_bucket(self, reason: str, filters: list[str]) -> None:
        text = " ".join([reason, *filters]).upper()
        if "PHASE55_CE_QUALITY_THRESHOLD" in text:
            self.blocked_by_ce_threshold += 1
        if "PHASE55_PE_DIRECTIONAL_THRESHOLD" in text:
            self.blocked_by_pe_threshold += 1
        if "PHASE55_CE_MIXED_REGIME" in text:
            self.blocked_by_mixed_regime += 1

    def _blocked_outcomes(self) -> int:
        return self.correct_blocks + self.false_positive_blocks

    @staticmethod
    def _pct(num: int | float, den: int | float) -> float:
        if not den:
            return 0.0
        return round(float(num) / float(den) * 100.0, 2)

    @staticmethod
    def _append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            if not exists:
                writer.writeheader()
            writer.writerow({key: row.get(key, "") for key in fields})

    @staticmethod
    def _recommendation(stats: dict[str, Any]) -> str:
        outcomes = int(stats.get("correct_blocks", 0)) + int(stats.get("false_positive_blocks", 0))
        if outcomes < 5:
            return "Keep Current Thresholds"
        if stats.get("estimated_pnl_missed", 0.0) > stats.get("estimated_pnl_saved", 0.0):
            return "Relax Thresholds"
        if stats.get("correct_block_rate", 0.0) >= 70.0 and stats.get("false_block_rate", 0.0) <= 20.0:
            return "Tighten Thresholds"
        return "Keep Current Thresholds"

    @staticmethod
    def _format_report(payload: dict[str, Any]) -> str:
        stats = payload["stats"]

        def _lines(items: list[tuple[str, int]]) -> str:
            if not items:
                return "- none"
            return "\n".join(f"- {name}: {count}" for name, count in items)

        return (
            "# Phase55 Summary\n\n"
            f"Generated: {payload['timestamp']}\n\n"
            f"- Trades Evaluated: {stats['trades_evaluated']}\n"
            f"- Trades Allowed: {stats['trades_allowed']}\n"
            f"- Trades Blocked: {stats['trades_blocked']}\n"
            f"- Block %: {stats['block_percentage']:.2f}\n"
            f"- Correct Blocks: {stats['correct_blocks']}\n"
            f"- Incorrect Blocks: {stats['false_positive_blocks']}\n"
            f"- Estimated PnL Saved: {stats['estimated_pnl_saved']:.2f}\n"
            f"- Estimated PnL Missed: {stats['estimated_pnl_missed']:.2f}\n\n"
            "## Top Blocking Reasons\n\n"
            f"{_lines(payload['top_blocking_reasons'])}\n\n"
            "## Regimes Most Frequently Blocked\n\n"
            f"{_lines(payload['regimes_most_frequently_blocked'])}\n\n"
            f"Recommendation: {payload['recommendation']}\n"
        )
