# SESSION HANDOFF — 2026-08-18 (night)

Written so tomorrow's session can pick up instantly. Read this FIRST.

## ⚡ The ONE prep item for tomorrow 9:15 AM
Zerodha access tokens EXPIRE DAILY. Tonight's token dies overnight.
**Procedure when user says "run":**
1. Kill any running `python master_runner.py` (stale token/session): `taskkill //IM python.exe //F` (or by PID) + `rm -f data/.master_runner.pid`
2. Test token: if `KITE_ACCESS_TOKEN` in `.env` is dead → run `python login.py` (Edge + selenium + pyotp, all installed) to refresh it into `.env`
3. Start: `PYTHONIOENCODING=utf-8 nohup python -u master_runner.py > /tmp/runner_live.log 2>&1 &`
4. Verify: feed connected (NIFTY BANK spot ~57k), `[WS SUBSCRIBED]` shows 23 tokens, `DRY_RUN=True`, `LOT_SIZE=30`, zero errors
5. Monitor all day: every trade (side/strike/fill/exit), block reasons if no trade, fix + restart on any error
6. Safety: bot runs DRY_RUN=1 (simulated orders). Only flip DRY_RUN=0 for real money on explicit user instruction.

## 🔧 State of the system (all done & verified tonight)
- **Instrument:** Bank NIFTY everywhere — token 260105, lot 30, 100-pt strikes
  - `engine/execution/broker.py`: option_index filters `name=="BANKNIFTY"` (342 options) — "NIFTY BANK"/"NIFTY" both leave it EMPTY
  - `engine/execution/broker.py`: REST spot is `ltp("NSE:NIFTY BANK")` — "NSE:BANKNIFTY" returns {} → None (was killing ALL trades)
  - `engine/live_engine.py`, `master_runner.py`, `candle_builder.py`, `scripts/refresh_zerodha_data.py`: token 260105
  - Live feed subscribes `"NIFTY BANK"`
- **ML engine revived:** hard-zero removed (`predictor_champion.py`), feature bugs fixed (`momentum_velocity` 3000x scale, `volatility` window in `ml/feature_config.py`), **June-19 champion models restored** from git 4380977 (Aug-16 retrain was degenerate — PE max 0.55 < 0.65 floor). Old models backed up `ml/models/backup_20260817_retrain/`
- **Entry confirmation:** `should_confirm_entry()` in master_runner (structure/pullback/momentum/HTF/trap), scalp gates in `engine/scalping/scalp_engine.py`, SAFE_SCALP mode (ML silent >20min → stricter), early breakeven lock (SCALP_LOCK_PTS=1.5)
- **Cleanup done:** 2GB training CSVs deleted (regenerable via `python ml/dataset_builder.py`), nifty-CSV poison rows stripped (backup in `archive/nifty_csv_2026_wrong_instrument_rows.csv`), versioned files renamed (trainer.py, dataset_builder.py)
- **Symbol format:** `BANKNIFTY26AUG57500CE` → `BANKNIFTY Aug 2026 57500 CE` (telegram + status)

## 🧪 Verified
- 34 tests pass, 1 skipped
- Real broker data: spot 57497.8, ATM CE 57500CE @560, PE 57500PE @298.9, chain 57300-57700
- Live run: 0 errors, watchdog healthy, 23 tokens subscribed

## 💡 Suggested next features (user-approved direction)
1. OI (open interest) confirmation gate — data already subscribed, unused
2. Premium/theta gate (skip too-cheap AND too-rich premium)
3. Trend-mode strike shift (ITM 0.6 delta during trends)
4. Wire auto pattern detection → `trading_brain/patterns/` + `rules/`

## 🧠 Second brain (`trading_brain/`)
Obsidian vault: `daily/` (EOD summaries), `trades/` (per-day), `patterns/` + `rules/` (EMPTY — never auto-populated). Role = memory layer turning history into rules.
