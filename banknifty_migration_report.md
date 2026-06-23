# BANKNIFTY Migration — Audit & Fix Report

**Date:** 2026-06-23
**Branch:** fix/cost-aware-labels
**Target instrument:** BANKNIFTY
**Constants enforced:** `TOKEN = 260105`, `STRIKE_STEP = 100`, `LOT_SIZE = 30`

Scope of fixes (per request): live execution path, broker, candle builder,
pricing, lot sizing, cost calculations, strike calculations, instrument
selection, risk management, and the live reporting (Telegram/dashboard) stack.
**Not touched** (per request): ML thresholds, strategy logic, feature
engineering, walk-forward methodology, model retraining.

---

## 1. Files changed (code)

| File | Edits | Severity band |
|------|-------|---------------|
| `engine/data/candle_builder.py` | 2 | CRITICAL (instrument token + spot symbol) |
| `engine/execution/broker.py` | 12 | CRITICAL (instrument filter, spot, ATM, strike step) |
| `backtest/option_pricer.py` | 2 | CRITICAL (pricing strike step) |
| `master_runner.py` | 6 | CRITICAL/MEDIUM (watchdog feed, ATM, qty default, comments) |
| `engine/config/config.py` | 2 | HIGH (LOT_SIZE) |
| `engine/execution/execution_engine.py` | 2 | HIGH (lot-size fallback) |
| `engine/execution/profit_manager.py` | 4 | HIGH (lot units + cost calc) |
| `engine/live_engine.py` | 1 | HIGH (P&L-guard lot default) |
| `engine/risk/risk_manager.py` | 1 | LOW (risk doc comment) |
| `telegram/messages.py` | 7 | MEDIUM (lot math + instrument labels) |

`git diff --stat` (code only): **10 files, ~73 insertions / ~77 deletions.**

> Non-code working-tree changes (`.env`, `data/runtime_state.json`,
> `data/system_health.json`, `data/diagnostics/session_version.json`,
> `data/.master_runner.pid`) are **runtime side-effects of the verification
> run** (master_runner auth refreshed the Kite access token; session state
> reset to 2026-06-23). They are NOT part of the migration and were left as-is
> — reverting `runtime_state.json` would reintroduce stale 2026-06-18 phantom
> positions (-₹2314, 2 open). No commit was made.

---

## 2. Lines changed (by category)

### CRITICAL — instrument selection / pricing / ATM / order routing

| File:Line | OLD | NEW |
|-----------|-----|-----|
| `candle_builder.py:70` | `sym = f"NSE:NIFTY 50"` | `sym = "NSE:NIFTY BANK"` |
| `candle_builder.py:309` | `return 256265` (NIFTY 50) | `return 260105` (NIFTY BANK) |
| `broker.py:32` | `inst["name"] != "NIFTY"` | `inst["name"] != "BANKNIFTY"` |
| `broker.py:136/211/303/316` | `self.ltp("NSE:NIFTY 50")` ×4 | `self.ltp("NSE:NIFTY BANK")` |
| `broker.py:141` | `atm = round(spot / 50) * 50` | `atm = round(spot / 100) * 100` |
| `broker.py:152` | `strike = atm + i * 50` | `strike = atm + i * 100` |
| `broker.py:214` | `round(spot / 50) * 50` | `round(spot / 100) * 100` |
| `broker.py:306` | `round(spot / 50) * 50` | `round(spot / 100) * 100` |
| `broker.py:307` | `... strike_shift * 50 ...` | `... strike_shift * 100 ...` |
| `broker.py:319` | `round(spot / 50) * 50` | `round(spot / 100) * 100` |
| `broker.py:322` | `s = atm + i * 50` | `s = atm + i * 100` |
| `broker.py:190/196-197` | `nifty_token = 256265` / `NIFTY token=` | `index_token = 260105` / `BANKNIFTY token=` |
| `option_pricer.py:52` | `strike_step: float = 50.0` | `strike_step: float = 100.0` |
| `master_runner.py:956` | `start_feed(["NIFTY 50"])` | `start_feed(["NIFTY BANK"])` |
| `master_runner.py:1517` | `atm_strike = round(current_price / 50) * 50` | `... / 100) * 100` |

### HIGH — lot sizing / cost / P&L / risk

| File:Line | OLD | NEW |
|-----------|-----|-----|
| `config.py:40` | `LOT_SIZE = int(os.getenv("LOT_SIZE", 65))` | `... , 30))` |
| `execution_engine.py:46` | `return 65   # NIFTY lot size` | `return 30   # BANKNIFTY lot size` |
| `profit_manager.py:38` | `_LOT_QTY = 65` | `_LOT_QTY = 30` |
| `profit_manager.py:63` | `_LOT_UNITS = 65` | `_LOT_UNITS = 30` |
| `live_engine.py:758` | `getattr(..., "LOT_SIZE", 65)` | `getattr(..., "LOT_SIZE", 30)` |

> Effect on cost model: `_cost_rs(qty)` now returns ₹66 for 1 lot (qty 30) and
> ₹132 for 2 lots (qty 60) — correct per-pair BANKNIFTY costing. Verified.

### MEDIUM — live reporting (Telegram / dashboard)

| File:Line | OLD | NEW |
|-----------|-----|-----|
| `messages.py:164/206/281` | `lots = qty // 65` | `lots = qty // 30` |
| `messages.py:341` | `Quantity : 65 (1 lot)` | `Quantity : 30 (1 lot)` |
| `messages.py:346` | `Trigger : NIFTY ... momentum` | `Trigger : BANKNIFTY ... momentum` |
| `messages.py:358/392` | `qty = pos.get("qty", 65)` | `qty = pos.get("qty", 30)` |
| `messages.py:578` | `📡 NIFTY <b>{ltp_str}</b>` | `📡 BANKNIFTY <b>{ltp_str}</b>` |
| `master_runner.py:442` | `qty_list, [65] * n` | `qty_list, [30] * n` |

### LOW — comments / docs (in modified scope files)

`candle_builder.py:303-307`, `broker.py:89/124-127/204`, `config.py:20/39`,
`execution_engine.py:37`, `profit_manager.py:23-25/62/128`,
`risk_manager.py:5`, `master_runner.py:532/764/1081-1083`.

---

## 3. Verification results (repo-wide, `*.py`)

Run after all edits. Commands: ripgrep across the repository.

- **Remaining `256265` (NIFTY token):** 1 — `scripts/refresh_zerodha_data.py:31` (out-of-scope data script).
- **Remaining `NSE:NIFTY 50` / `NIFTY 50`:** 1 — same line above. **Zero in the live execution path.**
- **Remaining lot-size `65`:** 4 — all out-of-scope (`walkforward_oos.py:65`, `forensic_oos.py:41`, `dataset_builder_v3.py:38`, `feedback_trainer.py:26`). All other `65`/`0.65` hits are ML probability thresholds or percentages, not lot sizes.
- **Remaining strike-step `/50*50`:** 1 — `backtest/backtest_engine.py:1085` (out-of-scope backtest).

**Compile/import test:** all 10 edited files `py_compile` clean; `import master_runner` → `IMPORT_OK` (broker auth succeeded, candle token=260105, LOT_SIZE=30, pricer step=100, `_cost_rs(30)=66`, `_cost_rs(60)=132`).

---

## 4. Remaining NIFTY references (NOT changed — outside requested fix scope)

These are in data-pipeline / ML / backtest code, which the request explicitly
excluded (no ML, no feature engineering, no walk-forward methodology, no
retraining). **They do not affect the live execution or live reporting stack**,
but they should be addressed before any BANKNIFTY backtest/retrain is trusted:

| File:Line | Issue | Why left |
|-----------|-------|----------|
| `scripts/refresh_zerodha_data.py:30-31` | `nifty_1m_full.csv` + token `256265` | Standalone data utility, superseded by `download_banknifty.py`; not imported by live engine. |
| `ml/dataset_builder_v3.py:38` | `LOT_UNITS = 65` in label P&L | Changing it alters training labels → requires retrain (excluded). |
| `ml/day_classifier.py:39` | reads `nifty_1m_full.csv` | ML data path (excluded). |
| `ml/feedback_trainer.py:26` | `BROKERAGE_ROUND_TRIP = 65*2` | ML feedback module (excluded). |
| `backtest/walkforward_oos.py:65` | `LOT_UNITS = 65` | Walk-forward methodology (excluded). |
| `backtest/forensic_oos.py:41` | `LOT_UNITS = 65` | Backtest (excluded). |
| `backtest/backtest_engine.py:50` | `BANKBANKNIFTY_LOT_SIZE` typo → `NameError` on default-config run | Backtest (excluded); pre-existing crash bug, flagged. |
| `backtest/backtest_engine.py:1085` | `round(spot/50)*50` | Backtest ATM (excluded). |
| `backtest/backtest_engine.py:1356` | `__main__` loads `nifty_1m_full.csv` | Backtest entry point (excluded). |
| `backtest/option_pricer.py` `atm_vol=0.13` | likely low IV for BANKNIFTY | Not a token/strike/lot/instrument item in the replace list; left for a tuning decision. |
| `engine/risk/risk_manager.py:42-49` | 4–10 premium-pt SL band | Premium-space (instrument-agnostic); no BANKNIFTY target value was provided — changing it = strategy tuning (excluded). |

---

## 5. References intentionally left unchanged (correct as-is)

- All `0.65` ML probability thresholds (`live_engine.py`, `backtest_engine.py`, `messages.py`, `notifier.py`, `dashboard.py`) — ML thresholds, explicitly out of scope.
- `messages.py` "65% locked" / `dashboard.py` "65%" — percentages, not lot sizes.
- `profit_manager.py:99,160` `0.65` — ladder/retention fractions.
- `telegram/messages.py` symbol parser regex — already accepts `BANKNIFTY` (5-digit strikes ~50000 still match `\d{5}`).
- `master_runner.py:561` `_BANKNIFTY_INDEX_TOKEN = 260105` and `live_engine.py:32` ORB token — already correct BANKNIFTY.

---

## 6. Caveat on LOT_SIZE = 30

The value **30** was used per the explicit instruction. Note: a web source
checked during the prior review indicated the *current* exchange BANKNIFTY lot
may be **35** (NSE Jan-2026 revision). This was applied as instructed (30);
confirm against the live Kite instrument master before real-money trading. The
live path also reads `inst["lot_size"]` from the broker instrument map first
(`execution_engine.py:41`), so live orders use the broker's actual lot when a
BANKNIFTY option is resolved; **30** is only the fallback/sizing default.

---

## 7. Goal status

Live execution + reporting stack now consistently uses BANKNIFTY:
`LOT_SIZE = 30`, `STRIKE_STEP = 100`, `TOKEN = 260105`. Existing trading logic,
ML thresholds, features, and walk-forward methodology were preserved. Remaining
NIFTY references are confined to out-of-scope data/ML/backtest files, listed
above.
