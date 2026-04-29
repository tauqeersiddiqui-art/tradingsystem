# engine/portfolio/allocator.py

print("✅ allocator module loaded")  # DEBUG LINE

class CapitalAllocator:

    def __init__(self, config):
        self.config = config
        self.max_risk_pct = 0.01
        self.max_lots = 10

    def size_position(self, capital, ml_prob, atr, price, lot_size, current_pnl, spread_pct=0):

        if ml_prob is None or ml_prob < 0.55:
            return 0

        if spread_pct > 0.06:
            return 0

        risk_capital = capital * self.max_risk_pct

        if atr <= 0:
            atr = price * 0.01

        raw_lots = risk_capital / (atr * lot_size)

        lots = int(raw_lots)

        lots = max(1, lots)
        lots = min(self.max_lots, lots)

        return lots