# ML Loss Analysis — Session 20260706

_Generated: 2026-07-06T16:45:28.260852 — evidence only, no trading logic changed._

## Headline

- ML trades: **8** — Winners **3** / Losers **5**
- Gross wins **₹+157.5**, gross losses **₹-1138.5**, **net ₹-981.0**
- Avg win **₹+52.5** vs avg loss **₹-227.7** — payoff is inverted (small wins, big losses)

## Root-cause pattern (across ALL 8 trades)

- **Every trade went green first**: MFE > 0 on all trades = True. Entries were directionally reasonable; the loss came *after* a favourable excursion.
- **No trade hit its target.** Exits were STOP (7) or TIME_EXIT_WEAK (1) — winners were small break-even/ladder locks while losers ran to the full stop.
- **Confidence was NOT the problem.** ML probabilities ranged 0.68–0.99; the Phase5.5 CE quality threshold (0.4358) never engaged. A confidence filter would not have prevented these.
- The `shadow_ml_95_would_block` flag is **True for all 8** — only an aggressive 0.95 floor blocks them, and that also blocks the winners.
- Dominant `loss_class`: `immediate_adverse_move` and `stop_too_tight` — an **exit/stop management** signature, not an entry-selection one.

## Per-trade breakdown (losers)

### BANKNIFTY26JUL58300PE PE  |  ₹-7.5  (stop_too_tight)

- **Entry / Exit:** 2026-07-06T10:48:07.282106 → 2026-07-06T10:52:21.377577
- **Regime:** engine=`RANGE`, phase55=`range`
- **Confidence:** ml_prob=0.789, ce_raw=0.5898, pe_raw=0.789
- **SHAP:** NOT_CAPTURED (not logged by current telemetry)
- **ATR:** 24.6  |  **ADX:** 16.3  |  **RSI:** 38.0
- **EMA/HTF state:** bullish  |  **VWAP:** above  |  **Supertrend dir:** -1
- **Entry reason:** ML_PE  |  **Exit reason:** STOP (STOP hit)
- **MFE / MAE:** ₹+123.0 / ₹-268.5  (went green ₹123 before reversing)
- **Phase5.5 rec that would prevent it:** ML-0.95 confidence floor (shadow_ml_95_would_block=True); HTF/30m EMA trend gate (shadow_htf_would_block=True); Phase5.5 CE quality threshold (0.4358) did NOT trigger — signal conf 0.789 cleared it, so the current threshold would NOT have prevented this
- **Should have been skipped?** YES
- **Would delaying entry have helped?** LIKELY YES — price fell to 670.0 vs entry 674.9 (-4.9); a delayed/limit entry would have improved basis

### BANKNIFTY26JUL58200CE CE  |  ₹-498.0  (stop_too_tight)

- **Entry / Exit:** 2026-07-06T11:51:10.347472 → 2026-07-06T11:52:13.428171
- **Regime:** engine=`RANGE`, phase55=`trend`
- **Confidence:** ml_prob=0.798, ce_raw=0.7747, pe_raw=0.0229
- **SHAP:** NOT_CAPTURED (not logged by current telemetry)
- **ATR:** 16.56  |  **ADX:** 27.0  |  **RSI:** 25.7
- **EMA/HTF state:** bullish  |  **VWAP:** below  |  **Supertrend dir:** -1
- **Entry reason:** ML_CE  |  **Exit reason:** STOP (STOP hit)
- **MFE / MAE:** ₹+132.0 / ₹-498.0  (went green ₹132 before reversing)
- **Phase5.5 rec that would prevent it:** ML-0.95 confidence floor (shadow_ml_95_would_block=True); HTF/30m EMA trend gate (shadow_htf_would_block=True); Phase5.5 CE quality threshold (0.4358) did NOT trigger — signal conf 0.798 cleared it, so the current threshold would NOT have prevented this
- **Should have been skipped?** YES
- **Would delaying entry have helped?** LIKELY YES — price fell to 934.2 vs entry 940.7 (-6.5); a delayed/limit entry would have improved basis

### BANKNIFTY26JUL58500CE CE  |  ₹-309.0  (immediate_adverse_move)

- **Entry / Exit:** 2026-07-06T12:20:23.105938 → 2026-07-06T12:24:49.946176
- **Regime:** engine=`RANGE`, phase55=`volatile_trend`
- **Confidence:** ml_prob=0.6849, ce_raw=0.6781, pe_raw=0.0115
- **SHAP:** NOT_CAPTURED (not logged by current telemetry)
- **ATR:** 23.12  |  **ADX:** 46.4  |  **RSI:** 77.5
- **EMA/HTF state:** bullish  |  **VWAP:** above  |  **Supertrend dir:** 1
- **Entry reason:** ML_CE  |  **Exit reason:** STOP (STOP hit)
- **MFE / MAE:** ₹+19.5 / ₹-309.0  (went green ₹20 before reversing)
- **Phase5.5 rec that would prevent it:** ML-0.95 confidence floor (shadow_ml_95_would_block=True); Phase5.5 CE quality threshold (0.4358) did NOT trigger — signal conf 0.685 cleared it, so the current threshold would NOT have prevented this
- **Should have been skipped?** YES
- **Would delaying entry have helped?** LIKELY YES — price fell to 850.2 vs entry 855.1 (-4.9); a delayed/limit entry would have improved basis

### BANKNIFTY26JUL58400CE CE  |  ₹-277.5  (immediate_adverse_move)

- **Entry / Exit:** 2026-07-06T12:36:27.802476 → 2026-07-06T12:39:05.275716
- **Regime:** engine=`RANGE`, phase55=`volatile_trend`
- **Confidence:** ml_prob=0.7667, ce_raw=0.7745, pe_raw=0.0006
- **SHAP:** NOT_CAPTURED (not logged by current telemetry)
- **ATR:** 19.92  |  **ADX:** 39.1  |  **RSI:** 46.7
- **EMA/HTF state:** bullish  |  **VWAP:** above  |  **Supertrend dir:** 1
- **Entry reason:** ML_CE  |  **Exit reason:** STOP (STOP hit)
- **MFE / MAE:** ₹+99.0 / ₹-277.5  (went green ₹99 before reversing)
- **Phase5.5 rec that would prevent it:** ML-0.95 confidence floor (shadow_ml_95_would_block=True); Phase5.5 CE quality threshold (0.4358) did NOT trigger — signal conf 0.767 cleared it, so the current threshold would NOT have prevented this
- **Should have been skipped?** YES
- **Would delaying entry have helped?** LIKELY YES — price fell to 910.0 vs entry 914.1 (-4.1); a delayed/limit entry would have improved basis

### BANKNIFTY26JUL58400CE CE  |  ₹-46.5  (immediate_adverse_move)

- **Entry / Exit:** 2026-07-06T13:09:30.762915 → 2026-07-06T13:14:31.290857
- **Regime:** engine=`RANGE`, phase55=`range`
- **Confidence:** ml_prob=0.9094, ce_raw=0.9674, pe_raw=0.2301
- **SHAP:** NOT_CAPTURED (not logged by current telemetry)
- **ATR:** 20.05  |  **ADX:** 16.6  |  **RSI:** 65.5
- **EMA/HTF state:** bullish  |  **VWAP:** above  |  **Supertrend dir:** 1
- **Entry reason:** ML_CE  |  **Exit reason:** TIME_EXIT_WEAK (TIME_EXIT_WEAK)
- **MFE / MAE:** ₹+25.5 / ₹-262.5  (went green ₹26 before reversing)
- **Phase5.5 rec that would prevent it:** ML-0.95 confidence floor (shadow_ml_95_would_block=True); Phase5.5 CE quality threshold (0.4358) did NOT trigger — signal conf 0.909 cleared it, so the current threshold would NOT have prevented this
- **Should have been skipped?** YES
- **Would delaying entry have helped?** LIKELY YES — price fell to 914.4 vs entry 919.1 (-4.8); a delayed/limit entry would have improved basis

## Winners (contrast)

| Symbol | Dir | PnL | MFE | MAE | conf | exit |
|---|---|---|---|---|---|---|
| BANKNIFTY26JUL58200CE | CE | ₹+72.0 | ₹+152 | ₹-196 | 0.7948 | STOP |
| BANKNIFTY26JUL58500CE | CE | ₹+16.5 | ₹+130 | ₹-254 | 0.9318 | STOP |
| BANKNIFTY26JUL58500CE | CE | ₹+69.0 | ₹+134 | ₹-30 | 0.9492 | STOP |

## Telemetry gaps (fields the required report asks for but the system does not log)

- **SHAP explanation** — no SHAP values are computed or persisted anywhere in the engine.
- **RSI / ADX / supertrend** — only present in replay JSONs, not in the trade journal.
- **Volatility** — no standalone metric; ATR-at-entry is the only volatility proxy logged.
- Recommend (post-freeze): add SHAP + RSI/ADX columns to the trade journal so this analysis is fully data-driven next session.

## Bottom line

The ML engine's entries were directionally sound (every trade printed positive MFE). The −₹981 came from **asymmetric exits**: winners locked tiny gains while losers round-tripped to full stops. **No Phase5.5 confidence/regime filter as configured would have converted this day to positive** — the lever is stop/exit management, which is explicitly out of scope for this freeze. Phase5.5 continues to record shadow evidence for a future, properly-calibrated gate.
