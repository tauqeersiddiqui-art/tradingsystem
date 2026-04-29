# execution/profit_manager.py

def manage_position(
    entry_price,
    ltp,
    lot_size,
    stop_loss,
    max_pnl,
    ml_prob
):
    """
    Returns:
        updated_stop_loss,
        updated_max_pnl,
        exit_reason (None or string)
    """

    pnl = (ltp - entry_price) * lot_size
    max_pnl = max(max_pnl, pnl)

    reason = None

    # =========================================
    # 1️⃣ Break-even once trade shows strength
    # =========================================
    if max_pnl >= 100:
        stop_loss = max(stop_loss, entry_price)

    # =========================================
    # 2️⃣ Hard Minimum ₹120 Lock (Capital Protection)
    # =========================================
    if max_pnl >= 150:
        min_points = 120 / lot_size
        stop_loss = max(stop_loss, entry_price + min_points)

    # =========================================
    # 3️⃣ Ladder Profit Lock System
    # =========================================
    if max_pnl >= 300:
        stop_loss = max(stop_loss, entry_price + 3)

    if max_pnl >= 600:
        stop_loss = max(stop_loss, entry_price + 6)

    # =========================================
    # 4️⃣ Ultra Profit Lock (92% of Max Profit)
    # =========================================
    # Balanced Professional Mode
    if max_pnl >= 1000:

        lock_percent = 0.92   # Change to 0.95 for aggressive lock
        allowed_drawdown = max_pnl * (1 - lock_percent)

        if pnl <= max_pnl - allowed_drawdown:
            reason = "MaxProfitTrail"

    # =========================================
    # 5️⃣ AI Dynamic Runner (Confidence Based)
    # =========================================
    if max_pnl >= 300:

        if ml_prob < 0.35:
            retention = 0.65
        elif ml_prob < 0.50:
            retention = 0.75
        else:
            retention = 0.85

        if pnl <= max_pnl * retention:
            reason = "Drawdown"

    # =========================================
    # 6️⃣ Final Hard Stop Loss
    # =========================================
    if ltp <= stop_loss:
        reason = "Stop Loss"

    return stop_loss, max_pnl, reason