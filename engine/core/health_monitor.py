# core/health_monitor.py
# UPDATED — system telemetry + monitoring

import json
import os
from datetime import datetime


HEALTH_PATH = "data/system_health.json"


def update_health(data):
    """
    Write system health snapshot to disk
    """

    os.makedirs("data", exist_ok=True)

    health = {
        "last_update": str(datetime.now()),

        # --- CORE METRICS --- #
        "pnl": data.get("pnl", 0),
        "positions": data.get("positions", 0),
        "active_position": data.get("active_position", None),

        # --- PERFORMANCE --- #
        "win_rate": data.get("win_rate", 0),
        "drawdown": data.get("drawdown", 0),

        # --- SYSTEM STATE --- #
        "regime": data.get("regime"),
        "signal": data.get("signal"),
        "mode": data.get("mode"),

        # --- EXECUTION --- #
        "latency_ms": data.get("latency", 0),
        "last_order": data.get("last_order"),

        # --- DEBUG / EXTENSIONS --- #
        **data
    }

    try:
        with open(HEALTH_PATH, "w") as f:
            json.dump(health, f, indent=2)
    except Exception as e:
        print(f"[HEALTH ERROR] {e}")


# ================= OPTIONAL: QUICK SNAPSHOT ================= #

def snapshot(ctx):
    """
    Build health snapshot directly from TradingContext
    """

    return {
        "pnl": getattr(ctx, "pnl", 0),
        "positions": len(getattr(ctx, "positions", [])),
        "active_position": getattr(ctx, "active_order", None),
        "win_rate": getattr(ctx, "win_rate", 0),
        "drawdown": getattr(ctx, "max_drawdown", 0),
        "regime": getattr(ctx.regime, "state", None) if ctx.regime else None,
        "signal": getattr(ctx, "last_trade", None),
        "mode": getattr(ctx.config, "TRADING_MODE", "UNKNOWN"),
    }