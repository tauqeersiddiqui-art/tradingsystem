---
kind: build_system
name: Python Script-Based Build & Session Orchestration (No Formal Build System)
category: build_system
scope:
    - '**'
source_files:
    - requirements.txt
    - .github/workflows/trading_morning.yml
    - .github/workflows/trading_afternoon.yml
    - .github/workflows/trading_test.yml
    - .github/workflows/research-tests.yml
    - master_runner.py
    - scripts/supervisor.py
    - scripts/start_trading.bat
    - scripts/session_monitor.sh
    - tests/test_entry_confirmation.py
---

## What system/approach is used

This repository has **no formal build system** — there are no Makefiles, Dockerfiles, `setup.py`/`pyproject.toml`, or packaging scripts. The project is a pure-Python trading application that is executed directly via the Python interpreter. Dependencies are pinned in a flat `requirements.txt` and installed with `pip install -r requirements.txt`. Runtime orchestration is handled by standalone Python scripts (`master_runner.py`, `scripts/supervisor.py`) and Windows batch files (`scripts/start_trading.bat`, `start_trading_afternoon.bat`, `setup_autostart.bat`).

## Key files and packages

- **Dependency manifest**: `requirements.txt` — declares `kiteconnect`, `pyotp`, `python-dotenv`, `pandas`, `numpy`, `lightgbm`, `scikit-learn`, `joblib`, `requests`, `playwright`.
- **CI pipelines** (GitHub Actions): `.github/workflows/trading_morning.yml`, `trading_afternoon.yml`, `trading_test.yml`, `research-tests.yml` — schedule-based morning/afternoon sessions plus test workflows.
- **Local orchestrator**: `master_runner.py` — single-process entry point that boots Zerodha broker session, assembles `TradingContext`, runs the live engine loop, and embeds an internal watchdog thread (`EngineWatchdog`) for crash recovery.
- **External supervisor**: `scripts/supervisor.py` — separate process that polls `data/.master_runner.pid`, auto-restarts `master_runner.py` on death (up to `SUPERVISOR_MAX_RESTARTS`), writes heartbeat logs, and stops after market close (15:40 IST).
- **Windows autostart**: `scripts/start_trading.bat` — called by Windows Task Scheduler; kills stale PID, launches `supervisor.py` detached.
- **Linux monitor**: `scripts/session_monitor.sh` — cron-style watcher that checks process liveness every 15 min until 15:30 IST and restarts if dead.
- **Tests**: `tests/test_entry_confirmation.py` — unit tests for `should_confirm_entry` and `ScalpEngine.check_entry/check_exit`; invoked by CI.

## Architecture and conventions

### Two-tier supervision model
The runtime uses a **process-level supervisor + thread-level watchdog** pattern:
1. `supervisor.py` runs as a long-lived parent process, polling every N seconds (default 15) for the engine's PID file (`data/.master_runner.pid`). On failure it spawns `master_runner.py` via `subprocess.Popen` with `DETACHED_PROCESS` on Windows and `PYTHONIOENCODING=utf-8`.
2. Inside `master_runner.py`, `EngineWatchdog` is a daemon thread that monitors `_engine_thread` every 30 s and restarts it if it dies, subject to three safety guards: emergency shutdown flag, daily loss limit reached, and max restarts (`WATCHDOG_MAX_RESTARTS`, default 5).

### CI-driven session execution
Morning and afternoon sessions run on GitHub Actions `ubuntu-latest` runners on a UTC cron schedule (`'40 3 * * 1-5'` = 9:10 AM IST). Each job:
- Checks out code, sets up Python 3.11 with pip cache.
- Installs `requirements.txt` and Playwright Chromium.
- Writes a `.env` from GitHub Secrets (Kite API keys, Telegram tokens).
- Runs `login_headless.py` for headless Zerodha login.
- Executes `timeout --signal=SIGINT <SECS> python master_runner.py` capped at 215 minutes.
- Saves `data/runtime_state.json` via `actions/cache/save@v4` keyed by date so the afternoon job can resume position management seamlessly.

### State hand-off between sessions
`runtime_state.json` is the canonical hand-off artifact between morning and afternoon CI jobs (and between local supervisor restarts). It stores PnL, positions, trades_today, open_position, scalp_position. On startup, `load_state()` restores counters and `deserialize_position()` rehydrates any open position so broker reconciliation can adopt it safely.

### Environment-driven modes
Runtime behavior is toggled via environment variables loaded from `.env` via `python-dotenv`:
- `PAPER_MODE=1` / `DRY_RUN=1` / `TEST_MODE=1` — switch between paper trading, dry-run, and test modes.
- `ALLOW_BROKER_POSITION_ON_START=1` — skip open-position gate for lunch-break resume.
- `ML_ON=1`, `CHAMPION_THRESHOLD=0.40`, `INITIAL_CAPITAL`, `MAX_RISK_PER_TRADE_PCT`, `MAX_LOTS_CAP` — strategy knobs.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_CHAT_ID`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_ADMIN_ID` — alerting.

### Logging and diagnostics layout
All output goes under `logs/` (`master_runner.log`, `monitor_session.log`, `session_output.log`, `supervisor_status.log`, `master_stdout.log`). Runtime data lives under `data/` (`runtime_state.json`, `system_health.json`, historical CSVs, journals, shadow backtests). No centralized logging framework is used — each script writes directly to its own log file.

## Conventions and constraints

- **No packaging**: The project is never built into wheels, containers, or installable packages. Deployment is "run the script" — either locally via Windows Task Scheduler or remotely via GitHub Actions scheduled workflows.
- **Dependencies are loose upper bounds**: `requirements.txt` uses `>=` pins (e.g. `pandas>=2.0.0`, `lightgbm>=4.0.0`); there is no lockfile (`requirements.lock`, `Pipfile.lock`, `poetry.lock`).
- **Environment secrets flow through `.env`**: Both local runs and CI write a `.env` file populated from `os.getenv`/secrets before launching the engine. Secrets are never baked into source.
- **Session boundaries are time-gated**: Both the external supervisor (market close 15:40 IST) and the CI `timeout` command enforce hard session end times; the engine itself also gates activity to market hours (9:15–15:30 IST).
- **Crash recovery is mandatory**: Every restart path (watchdog, supervisor, CI resume) must reconcile against the broker before resuming trading. An orphaned position without saved state triggers PAUSE + flatten.
- **Tests target entry/exit logic only**: The sole test file (`tests/test_entry_confirmation.py`) validates `should_confirm_entry` and `ScalpEngine` filters with synthetic tick histories; there is no integration test suite for broker connectivity or full-engine runs.