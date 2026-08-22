---
kind: configuration_system
name: Environment-Driven Configuration via `engine.config.Config`
category: configuration_system
scope:
    - '**'
source_files:
    - engine/config/config.py
    - master_runner.py
    - engine/execution/cost_model.py
    - backtest/backtest_engine.py
    - backtest/scalp_backtest.py
    - backtest/walkforward_oos.py
    - backtest/forensic_oos.py
    - scripts/start_trading.bat
    - scripts/start_trading_afternoon.bat
---

## What system/approach is used

The application uses a **single-process, environment-variable-driven configuration** pattern centered on one class: `engine.config.config.Config`. There are no YAML/JSON/TOML config files, no typed config models, and no runtime config reload. Every tunable parameter — capital, risk limits, ML thresholds, entry/exit gates, scalping parameters, cooldowns, lot sizes — is read at import time from `os.getenv(...)` with a hard-coded default string or number.

The process bootstrap in `master_runner.py` calls `load_dotenv()` (from the `python-dotenv` package) before importing anything else, so a local `.env` file is the standard way to override defaults for a given machine or deployment. The same `os.getenv` keys are also consumed directly by backtest scripts (`backtest/backtest_engine.py`, `backtest/scalp_backtest.py`, `backtest/walkforward_oos.py`, `backtest/forensic_oos.py`) which build their own ad-hoc dicts of env vars to pass into simulation runs.

## Key files and packages

- `engine/config/config.py` — the single source of truth for all trading/runtime knobs. It defines one `Config` class whose `__init__` reads ~60+ `os.getenv` entries and exposes them as attributes.
- `master_runner.py` — the orchestrator that loads `.env` via `load_dotenv()`, then constructs `TradingContext` and injects `ctx.config = Config()` into every subsystem (ExecutionEngine, CapitalAllocator, LiveEngine, etc.).
- `engine/execution/cost_model.py` — imports `Config` to pick up `COST_PER_LOT`, `LOT_SIZE`, and related cost/risk values.
- Backtest modules under `backtest/` — duplicate the same env-key names (`CHAMPION_THRESHOLD`, `MAX_TRADES_PER_DAY`, `LOT_SIZE`, `BT_MAX_TRADES`, `FOLDS`, `OOS_START`, etc.) so live and backtest code can be driven by the same shell/env surface.
- `scripts/start_trading.bat`, `scripts/start_trading_afternoon.bat`, `scripts/session_monitor.sh` — Windows batch / shell launchers that are the typical place where `PAPER_MODE`, `DRY_RUN`, `KITE_ACCESS_TOKEN`, and other env vars would be set before invoking `master_runner.py`.

## Architecture and conventions

1. **One class, flat namespace.** `Config` stores every setting as an instance attribute. Consumers access it via `ctx.config.<KEY>` rather than calling a getter function. There is no nested grouping (e.g. no `config.risk.daily_loss_limit`); everything lives at the top level.

2. **Defaults are embedded in the code.** Each `os.getenv` call carries a literal default (e.g. `INITIAL_CAPITAL=100000`, `RISK_PER_TRADE=0.02`, `MAX_TRADES_PER_DAY=8`, `LOT_SIZE=30`, `CHAMPION_THRESHOLD=0.42`). This means the code runs out-of-the-box without any `.env` file; overrides are opt-in.

3. **Type coercion happens at read time.** Boolean flags are compared against the string `"1"` (`== "1"`), numeric settings are wrapped in `float()` or `int()`. A mis-typed value in `.env` will raise at import time, not later during trading.

4. **Configuration is immutable after import.** `Config()` is instantiated once in `build_context()` and passed around as part of `TradingContext`. No setter exists; changing behavior requires restarting the process with different env vars.

5. **Boilerplate print on init.** The last line of `Config.__init__` prints `[CONFIG] Capital=... | DRY_RUN=... | LOT_SIZE=...`, giving a one-line confirmation of what was actually loaded.

6. **Parallel env surface in backtests.** Backtest scripts do not import `engine.config.config`; instead they re-declare the same env-key names locally and merge them into a dict passed to the backtest engine. This keeps live and research code decoupled while preserving a shared env contract.

7. **Secrets are separate from tuning knobs.** Broker credentials (`KITE_ACCESS_TOKEN`) are read directly in `master_runner.init_broker()` and raise `RuntimeError` if missing. They are not part of `Config`, keeping secrets out of the strategy config surface.

## Conventions and constraints

- **Every tunable has a documented comment above its `os.getenv` line** explaining why the default was chosen (e.g. warmup window, lunch filter, re-entry cooldown, scalp SL tiers). Treat those comments as the authoritative rationale for each default.
- **Boolean toggles use the convention `VAR == "1"`**, never truthy-string checks. To enable paper/dry-run modes, set `PAPER_MODE=1` or `DRY_RUN=1`; any other value disables them.
- **Numeric thresholds are tuned via env, not code edits.** The codebase repeatedly references WFO-optimized values (e.g. `SCALP_COOLDOWN=240`, `SCALP_MAX_CONSEC_LOSSES=5`, `MICRO_TREND_CANDLES=3`) — these are meant to be adjusted through environment, not by editing `config.py`.
- **Backtest and live share the same env key vocabulary** (`CHAMPION_THRESHOLD`, `MAX_TRADES_PER_DAY`, `LOT_SIZE`, `DEFAULT_SL_PCT`, `DEFAULT_TARGET_PCT`). Changing a threshold should be done consistently across both surfaces.
- **There is no schema validation beyond Python type casts.** Adding a new config key requires adding it to `engine/config/config.py` AND documenting the env var name; there is no central registry or auto-discovery.
- **Process restart is required to apply changes.** Because `Config` is constructed at module import time inside `build_context()`, changing `.env` or shell env variables only takes effect when the master runner process is restarted.