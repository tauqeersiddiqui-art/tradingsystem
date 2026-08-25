# engine/portfolio/allocator.py


class CapitalAllocator:

    def __init__(self, config):
        self.config = config
        self.max_risk_pct = 0.01
        self.max_lots = 10
        # Floor must match the intraday learner's clamp range [0.45, 0.56] —
        # a 0.55 floor used to ALLOC_ZERO signals the learner had accepted.
        self.min_ml_prob = float(getattr(config, "ALLOC_MIN_ML_PROB", 0.45))

    def size_position(self, capital, ml_prob, atr, price, lot_size, current_pnl, spread_pct=0):

        if ml_prob is None or ml_prob < self.min_ml_prob:
            return 0

        if spread_pct > 0.06:
            return 0

        risk_capital = capital * self.max_risk_pct

        if atr <= 0:
            atr = price * 0.01

        raw_lots = risk_capital / (atr * lot_size)

        lots = int(raw_lots)

        # Risk cap is authoritative: if the allowed risk buys less than one
        # lot, there is no trade (was max(1, lots) — made the cap advisory).
        if lots < 1:
            return 0
        lots = min(self.max_lots, lots)

        return lots