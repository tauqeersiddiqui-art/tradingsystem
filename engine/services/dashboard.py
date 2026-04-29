# engine/services/dashboard.py

from datetime import datetime
import numpy as np
import os


class DashboardEngine:

    def __init__(self):
        self.ml_history = []

    def update_ml(self, prob):
        if prob is not None:
            self.ml_history.append(prob)
            if len(self.ml_history) > 500:
                self.ml_history.pop(0)

    def percentile(self, prob):
        if not self.ml_history:
            return 0
        return int((np.sum(np.array(self.ml_history) <= prob) / len(self.ml_history)) * 100)


def render(ctx, market_data, decision):

    if not hasattr(ctx, "dashboard_engine"):
        ctx.dashboard_engine = DashboardEngine()

    candles = market_data["candles"]
    ltp = candles[-1]

    features = decision.get("features", {}) if decision else {}

    ema20 = features.get("ema20", ltp)
    ema50 = features.get("ema50", ltp)

    # ================= REGIME ================= #
    regime = "TREND" if abs(ema20 - ema50) > 0.001 * ltp else "RANGE"

    # ================= STRUCTURE ================= #
    ema_trend = "UP" if ema20 > ema50 else "DOWN"
    pullback = "✅" if abs(ema20 - ltp) < 0.003 * ltp else "❌"
    continuation = "✅" if (ema20 > ema50 and ltp > ema20) else "❌"
    late_entry = "✅" if ctx.cycle_count > 60 else "❌"

    # ================= ML ================= #

    ml_prob = decision.get("ml_prob", 0) if decision else 0

    # update ML history
    ctx.dashboard_engine.update_ml(ml_prob)

    # calculate percentile properly
    percentile = ctx.dashboard_engine.percentile(ml_prob)

    # ENV threshold (single source of truth)
    try:
        env_thr = float(os.getenv("CHAMPION_THRESHOLD", 0.6))
    except:
        env_thr = 0.6

    required_min = round(env_thr, 2)
    gap = round(ml_prob - required_min, 3)

    # ================= BIAS ================= #
    bias = decision.get("side", "PE") if decision else "PE"

    # ================= SCORING ================= #
    score = round((ml_prob * 50) + (percentile / 2), 2)
    score_required = 3.0
    score_gap = round(score - score_required, 2)

    # ================= RISK ================= #
    trades = len(ctx.positions)
    pnl = round(ctx.pnl, 2)

    daily_limit_ok = "✅" if pnl > -2000 else "❌"

    last_trades = ctx.positions[-3:]
    loss_lock = "❌" if all(p < 0 for p in last_trades) and len(last_trades) == 3 else "✅"

    # ================= DECISION ================= #
    action = "TRADE" if decision else "WAIT"
    primary_block = "None" if decision else "No Signal"

    # ================= STATS ================= #
    wins = sum(1 for p in ctx.positions if p > 0)
    losses = sum(1 for p in ctx.positions if p < 0)

    ce_stats = f"{wins}W/{losses}L"
    pe_stats = ce_stats  # simplified

    adaptive_thr = required_min

    # ================= OUTPUT ================= #

    return f"""
🤖 AGENTIC TRADER — AI ENGINE STATUS
━━━━━━━━━━━━━━━━━━━━━━━━━━

🕒 Time : {datetime.now().strftime('%H:%M:%S')}
📊 Regime : {regime}
🎯 Direction Bias : {bias}

━━━━━━━━ STRUCTURE ━━━━━━━━
📈 EMA Trend      : {ema_trend}
🔁 Pullback       : {pullback}
🚀 Continuation   : {continuation}
⏳ Late Entry     : {late_entry}

━━━━━━━━ ML ENGINE ━━━━━━━━
🧠 ML Probability : {round(ml_prob,3)}
🎯 Required Min   : {required_min}
📉 Gap            : {gap}
📊 Today Percentile : {percentile}%

━━━━━━━━ SCORING ━━━━━━━━
⭐ Final Score     : {score}
🎯 Score Required  : {score_required}
📉 Score Gap       : {score_gap}

━━━━━━━━ RISK CONTROL ━━━━━━━━
💰 Trades Today    : {trades} / 10
📉 Today PnL       : {pnl}
🛡 Daily Limit OK  : {daily_limit_ok}
🔐 Consecutive Loss Lock : {loss_lock}

━━━━━━━━ DECISION ━━━━━━━━
⛔ Action          : {action}
🚫 Primary Block   : {primary_block}
📌 Secondary Block : NONE

━━━━━━━━━━━━━━━━━━━━━━━━━━

Day Type : UNKNOWN
CE {ce_stats} | PE {pe_stats}
Adaptive Thr : {adaptive_thr}
Multipliers CE x1.0 PE x1.0
"""