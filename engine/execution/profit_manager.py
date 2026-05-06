# engine/execution/profit_manager.py

def manage_position(entry_price, ltp, lot_size, stop_loss, max_pnl, ml_prob):

    pnl     = (ltp - entry_price) * lot_size
    max_pnl = max(max_pnl, pnl)
    reason  = None

    # 1️⃣ Break-even at ₹150 (raised from ₹100 — give more room early)
    if max_pnl >= 150:
        stop_loss = max(stop_loss, entry_price)

    # 2️⃣ Lock ₹200 minimum once ₹300 reached (raised from ₹120 lock at ₹150)
    if max_pnl >= 300:
        min_points = 200 / lot_size
        stop_loss  = max(stop_loss, entry_price + min_points)

    # 3️⃣ Ladder locks — tighter steps
    if max_pnl >= 500:
        stop_loss = max(stop_loss, entry_price + 5)

    if max_pnl >= 900:
        stop_loss = max(stop_loss, entry_price + 9)

    # 4️⃣ Ultra Profit Lock — only above ₹1500 (was ₹1000, too early)
    if max_pnl >= 1500:
        lock_percent     = 0.90
        allowed_drawdown = max_pnl * (1 - lock_percent)
        if pnl <= max_pnl - allowed_drawdown:
            reason = "MaxProfitTrail"

    # 5️⃣ Drawdown exit — confidence-based runner
    #    Threshold raised to max_pnl >= 400 (was 300) — let it breathe longer
    if max_pnl >= 400:
        if ml_prob < 0.35:
            retention = 0.70    # was 0.65
        elif ml_prob < 0.50:
            retention = 0.80    # was 0.75
        else:
            retention = 0.88    # was 0.85

        if pnl <= max_pnl * retention:
            reason = "Drawdown"

    # 6️⃣ Hard stop
    if ltp <= stop_loss:
        reason = "Stop Loss"

    return stop_loss, max_pnl, reason