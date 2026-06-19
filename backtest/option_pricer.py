# backtest/option_pricer.py
# ---------------------------------------------------------------------------
# BLACK-SCHOLES ATM OPTION PRICER  (real delta + gamma + theta)
#
# WHY THIS EXISTS:
#   The original OptionPriceSimulator in backtest_engine.py priced an option as
#       premium = time_value + 0.5 * favorable_spot_move
#   That is NOT an option:
#     * delta was FIXED at 0.5  -> no gamma (delta never changed with moves),
#     * time_value barely changed over the hold -> almost NO theta decay.
#   Result: backtest P&L IGNORED the two costs that actually kill long-option
#   intraday strategies (theta bleed + adverse gamma). Backtests looked better
#   than reality; the AUC-vs-expectancy gap was partly an artifact of this.
#
#   This module prices a near-ATM European option each bar via Black-Scholes:
#     * delta moves with moneyness (gamma present),
#     * theta genuinely decays as mins_to_close shrinks,
#     * CE gains on up-moves, PE on down-moves (side-correct).
#
# DROP-IN: the public method signature is IDENTICAL to the old simulator:
#       premium(entry_spot, cur_spot, side, mins_to_close) -> float (pts)
#   So backtest_engine.py / walkforward_oos.py / dataset_builder_v3.py only need
#   to import OptionPriceSimulator from here instead of the old inline class.
#
# To switch the engine over, in backtest/backtest_engine.py replace the inline
# `class OptionPriceSimulator: ...` with:
#       from backtest.option_pricer import OptionPriceSimulator
# and keep `_opt_sim = OptionPriceSimulator()` as-is. walkforward_oos.py and
# dataset_builder_v3.py import it via backtest_engine, so they follow along.
# ---------------------------------------------------------------------------

import os
import math


class OptionPriceSimulator:
    """
    Black-Scholes ATM option pricer for NIFTY-spot backtesting.

    The strike is pinned to the ENTRY spot (ATM at entry, rounded to the nearest
    50-pt strike). As cur_spot moves, the option correctly goes ITM/OTM with
    real delta+gamma, and theta bleeds as mins_to_close shrinks. Premium is
    returned in option-premium points (same space as the stop ladder and the
    old proxy), so nothing downstream changes units.

    Vol/rate are simple ATM assumptions, deliberately conservative — this is an
    honest backtest pricer, not a live pricing oracle.
    """

    def __init__(self, atm_vol: float = 0.13, rate: float = 0.065,
                 minutes_per_day: int = 375, trading_days: int = 252,
                 strike_step: float = 50.0):
        self.atm_vol     = float(os.getenv("BT_ATM_VOL", atm_vol))   # annualized IV
        self.rate        = rate
        self.mpd         = minutes_per_day
        self.tdays       = trading_days
        self.strike_step = strike_step

    # ── math helpers ─────────────────────────────────────────
    @staticmethod
    def _norm_cdf(x: float) -> float:
        """Standard normal CDF via erf (no scipy dependency)."""
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def _years(self, mins_to_close: float) -> float:
        return max(mins_to_close / (self.mpd * self.tdays), 1e-6)

    def _bs_price(self, spot: float, strike: float, T: float, side: str) -> float:
        """Black-Scholes price of a European CE/PE. T in years."""
        sigma = max(self.atm_vol, 1e-4)
        T = max(T, 1e-6)
        if spot <= 0 or strike <= 0:
            return 1.0
        d1 = (math.log(spot / strike) + (self.rate + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        disc = math.exp(-self.rate * T)
        if side == "CE":
            px = spot * self._norm_cdf(d1) - strike * disc * self._norm_cdf(d2)
        else:  # PE
            px = strike * disc * self._norm_cdf(-d2) - spot * self._norm_cdf(-d1)
        return float(px)

    # ── public API (drop-in compatible) ───────────────────────────
    def price(self, spot: float, atm_strike: float, side: str,
              mins_to_close: float) -> float:
        """Black-Scholes premium for an option struck at atm_strike."""
        T = self._years(mins_to_close)
        return round(max(self._bs_price(spot, atm_strike, T, side), 1.0), 2)

    def premium(self, entry_spot: float, cur_spot: float, side: str,
                mins_to_close: float) -> float:
        """
        Premium of the option that was ATM at ENTRY, valued at cur_spot.

        Same signature/return space as the old proxy — drop-in. Strike is pinned
        to entry_spot (nearest strike_step), so moves produce real delta+gamma
        and theta decays with mins_to_close. Side-correct: CE up, PE down.
        """
        strike = round(entry_spot / self.strike_step) * self.strike_step
        T = self._years(mins_to_close)
        px = self._bs_price(cur_spot, strike, T, side)
        return round(max(px, 1.0), 2)

    def pnl(self, entry_spot: float, exit_spot: float, atm_strike: float,
            side: str, qty: int, entry_mins_to_close: float,
            exit_mins_to_close: float) -> float:
        """Premium-space P&L (side-correct), matching the old simulator's API."""
        ep = self.premium(entry_spot, entry_spot, side, entry_mins_to_close)
        xp = self.premium(entry_spot, exit_spot,  side, exit_mins_to_close)
        return round((xp - ep) * qty, 2)
