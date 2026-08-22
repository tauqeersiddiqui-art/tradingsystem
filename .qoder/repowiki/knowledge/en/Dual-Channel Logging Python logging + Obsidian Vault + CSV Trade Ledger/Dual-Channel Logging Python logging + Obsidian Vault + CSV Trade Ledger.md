---
kind: logging_system
name: 'Dual-Channel Logging: Python logging + Obsidian Vault + CSV Trade Ledger'
category: logging_system
scope:
    - '**'
source_files:
    - master_runner.py
    - utils/obsidian_logger.py
    - engine/services/trade_logger.py
    - scripts/supervisor.py
    - engine/live_engine.py
    - engine/execution/execution_engine.py
    - engine/data/candle_builder.py
    - engine/core/state_store.py
    - engine/diagnostics/trade_journal.py
    - engine/diagnostics/eod_report.py
    - engine/analytics/performance.py
    - engine/analytics/trade_replay.py
    - engine/execution/profit_manager.py
    - engine/scalping/scalp_engine.py
    - backtest/backtest_engine.py
---

## What system/approach is used

The repository uses a **dual-channel logging system** with no third-party logging framework:

1. **Python `logging` module** — standard library logger hierarchy rooted at `master_runner.py`, which calls `logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")` and attaches a single `FileHandler("logs/master_runner.log")`. Every engine sub-module creates its own named logger via `logging.getLogger("<module_name>")` (e.g. `live_engine`, `execution_engine`, `candle_builder`, `state_store`, `trade_journal`, `profit_manager`, `scalp`, `analytics.performance`, `trade_replay`, `backtest`).
2. **Obsidian "Second Brain" markdown sink** — `utils/obsidian_logger.py` appends human-readable Markdown records into a `trading_brain/` vault (`daily/`, `trades/`, `patterns/`, `rules/`) using a thread-safe `_safe_append` guarded by a `threading.Lock()`. It never raises; failures are silently swallowed so the trading loop cannot crash on I/O errors.
3. **Persistent CSV trade ledger** — `engine/services/trade_logger.py` writes one row per closed trade to `data/trades/trade_log_YYYY_WNN.csv` (weekly rolling files) under a `threading.Lock`, with a fixed 40-column schema covering entry/exit prices, PnL, MFE/MAE, R-multiple, ML probability, slippage, latency, thresholds, and regime.
4. **Supervisor console log** — `scripts/supervisor.py` uses a bespoke `log()` helper that prints to stdout and appends `logs/supervisor_status.log` in a simple `timestamp | message` format.

There is no centralized logging configuration module; each process configures its own sinks at import time.

## Key files and packages

- `master_runner.py` — global `logging.basicConfig` + root `FileHandler("logs/master_runner.log")`; defines the root `logger = logging.getLogger("master")`.
- `engine/live_engine.py` — `logger = logging.getLogger("live_engine")`; primary decision-loop logger.
- `engine/execution/execution_engine.py` — `logger = logging.getLogger("execution_engine")`.
- `engine/data/candle_builder.py` — `logger = logging.getLogger("candle_builder")`.
- `engine/core/state_store.py` — `logger = logging.getLogger("state_store")`.
- `engine/diagnostics/trade_journal.py` — `logger = logging.getLogger("trade_journal")`.
- `engine/diagnostics/eod_report.py` — `logger = logging.getLogger("eod_report")`.
- `engine/analytics/performance.py` — `logger = logging.getLogger("analytics.performance")`.
- `engine/analytics/trade_replay.py` — `logger = logging.getLogger("trade_replay")`.
- `engine/execution/profit_manager.py` — `logger = logging.getLogger("profit_manager")`.
- `engine/scalping/scalp_engine.py` — `logger = logging.getLogger("scalp")`.
- `backtest/backtest_engine.py` — `logger = logging.getLogger("backtest")`.
- `utils/obsidian_logger.py` — Obsidian vault writer (`log_trade`, `log_daily_summary`, `log_pattern`, `check_and_log_patterns`, `initialize_vault`).
- `engine/services/trade_logger.py` — CSV trade ledger (`log_trade`, `today_summary`, `get_trades_for_day`).
- `scripts/supervisor.py` — supervisor status logger writing `logs/supervisor_status.log`.

## Architecture and conventions

### Logger hierarchy and naming
- Each production module declares a module-level `logger = logging.getLogger("<short_name>")`, producing namespaced output like `[live_engine]`, `[execution_engine]`, `[candle_builder]`, etc. The root handler in `master_runner.py` routes all of them to `logs/master_runner.log`.
- Log level is set once at process start to `INFO`; individual handlers can override (the master runner's FileHandler is also set to `INFO`).
- Format is uniform across the root handler: `HH:MM:SS [name] LEVEL: message`.

### Structured fields
- **CSV trades** are the only truly structured sink: a fixed 40-field schema (`trade_id, date, entry_time, exit_time, symbol, side, regime, entry_price, exit_price, quantity, pnl, R_multiple, ml_prob, entry_score, MFE, MAE, holding_seconds, stop_loss, target, stop_distance_pts, peak_pnl, entry_reason, exit_reason, signal_ts, order_submit_ts, fill_ts, signal_price, fill_price, first_bid, first_ask, first_ltp, spread, slippage_pts, signal_to_order_latency_ms, order_to_fill_latency_ms, ce_threshold, pe_threshold`) written atomically under a lock.
- **Obsidian markdown** records use a consistent template per record type (trade block, daily summary block, pattern block) with bold field labels and `---` separators.
- **Console/stdout logs** are unstructured free-form strings; there is no JSON or key-value structured logging for operational logs.

### Sinks and routing
| Sink | Writer | Location | Rotation / Lifecycle |
|---|---|---|---|
| Engine runtime logs | `logging.FileHandler` attached to root logger | `logs/master_runner.log` | Append-only, no rotation in code |
| Supervisor status | Custom `log()` | `logs/supervisor_status.log` | Append-only |
| Trade records | `engine/services/trade_logger.py` | `data/trades/trade_log_YYYY_WNN.csv` | Weekly rolling file per ISO week |
| Obsidian trades | `utils/obsidian_logger.log_trade` | `trading_brain/trades/YYYY-MM-DD.md` | One file per day |
| Obsidian daily summaries | `utils/obsidian_logger.log_daily_summary` | `trading_brain/daily/YYYY-MM-DD.md` | Appended per EOD |
| Obsidian patterns | `utils/obsidian_logger.log_pattern` | `trading_brain/patterns/common_failures.md` | Single file appended |

### Thread safety
- All persistent writers guard writes with `threading.Lock()` (Obsidian `_write_lock`, trade CSV `_lock`).
- Obsidian `_safe_append` catches all exceptions and returns `False` — the comment explicitly states "logging layer must never crash trading loop".

### Operational conventions
- Trades are logged **only on exit** (`log_trade` called after every exit), not on entry.
- Daily summaries are generated at EOD (15:30 trigger) via `log_daily_summary`.
- Pattern detection runs at EOD via `check_and_log_patterns(trades_today)` against the day's CSV trades to auto-flag recurring failure modes (high MFE low capture, immediate reversal, repeated losing side, stop too tight).
- The supervisor reads `logs/master_runner.log` tail to include last engine log line in heartbeat messages.

## Conventions and constraints

- **No external logging library**: the project relies exclusively on stdlib `logging` plus plain-file appenders; no `structlog`, `loguru`, `python-json-logger`, etc.
- **Single root configuration**: `master_runner.py` is the only place `logging.basicConfig` is called; other modules only create named loggers — they do not reconfigure handlers.
- **INFO minimum level**: all root handlers are set to `INFO`; DEBUG-level messages are not emitted through the root handler.
- **Append-only files**: all sinks open files in `"a"` mode; there is no built-in log rotation, truncation, or size-based rollover in code.
- **Fail-fast logging is forbidden**: `utils/obsidian_logger._safe_append` wraps every write in try/except and swallows exceptions, ensuring the trading loop continues even if disk writes fail.
- **Trade data is authoritative in CSV**: `engine/services/trade_logger.py` computes net PnL via `net_pnl` from `engine.execution.cost_model` (R6) and stores it alongside derived metrics (R-multiple, MFE, MAE); this CSV is the source of truth for EOD analytics and pattern detection.
- **Vault structure is enforced**: `initialize_vault()` creates `trading_brain/{daily,trades,patterns,rules}/` directories and seed README/index files on first run.
- **Supervisor isolation**: `scripts/supervisor.py` deliberately avoids importing `telegram.notifier` (which starts a long-poll thread) to prevent Telegram 409 conflicts; it sends alerts via direct `requests.post` instead.