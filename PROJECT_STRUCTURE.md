# AI_Trading_System_Pro FULL STRUCTURE

├── trading_system/
│   ├── .cursorrules
│   ├── .env
│   ├── .gitignore
│   ├── .mcp.json
│   ├── .opencode.json
│   ├── .telegram_state.json
│   ├── .windsurfrules
│   ├── AGENTS.md
│   ├── CLAUDE.md
│   ├── GEMINI.md
│   ├── PROJECT_STRUCTURE.md
│   ├── access_token.txt
│   ├── analysis_ce_bias.py
│   ├── analysis_expiry.py
│   ├── analysis_full.py
│   ├── backtest_output.txt
│   ├── generate_tree.py
│   ├── htf_test.py
│   ├── login.py
│   ├── master_runner.py
│   ├── profit_study.py
│   ├── run_backtest_diagnostic.py
│   ├── .claude/
│   │   ├── settings.json
│   │   ├── settings.local.json
│   │   ├── skills/
│   │   │   ├── debug-issue.md
│   │   │   ├── explore-codebase.md
│   │   │   ├── refactor-safely.md
│   │   │   ├── review-changes.md
│   ├── .code-review-graph/
│   │   ├── .gitignore
│   │   ├── graph.db
│   ├── .kiro/
│   │   ├── steering/
│   │   │   ├── code-review-graph.md
│   ├── .vscode/
│   │   ├── settings.json
│   ├── archive/
│   │   ├── models/
│   │   ├── notebooks/
│   ├── backtest/
│   │   ├── backtest_engine.py
│   │   ├── results/
│   │   │   ├── day_log.csv
│   │   │   ├── trade_log.csv
│   ├── data/
│   │   ├── system_health.json
│   │   ├── analytics/
│   │   │   ├── slippage_log.csv
│   │   │   ├── replays/
│   │   │   │   ├── 20260610_105900_CE_test_trade.json
│   │   │   │   ├── 20260611_100800_CE_1.json
│   │   │   │   ├── 20260611_101140_CE_2.json
│   │   │   │   ├── 20260611_102100_CE_1.json
│   │   │   │   ├── 20260611_105400_PE_6.json
│   │   │   │   ├── 20260611_105521_PE_3.json
│   │   │   │   ├── 20260611_105525_PE_5.json
│   │   │   │   ├── 20260611_105927_PE_6.json
│   │   │   │   ├── 20260611_110107_PE_1.json
│   │   │   │   ├── 20260611_110605_CE_3.json
│   │   │   │   ├── 20260611_110605_CE_5.json
│   │   │   │   ├── 20260612_111400_CE_1.json
│   │   │   │   ├── 20260612_111526_CE_2.json
│   │   │   │   ├── 20260612_112900_PE_1.json
│   │   ├── diagnostics/
│   │   │   ├── session_version.json
│   │   │   ├── journals/
│   │   │   │   ├── journal_2026_06_10.csv
│   │   │   │   ├── journal_2026_06_11.csv
│   │   │   │   ├── journal_2026_06_12.csv
│   │   │   ├── shadow/
│   │   │   │   ├── shadow_2026_06_10.csv
│   │   │   │   ├── shadow_2026_06_11.csv
│   │   │   │   ├── shadow_2026_06_12.csv
│   │   ├── historical/
│   │   │   ├── nifty_1m_full.csv
│   │   ├── trades/
│   │   │   ├── trade_log_2026_W24.csv
│   ├── engine/
│   │   ├── live_engine.py
│   │   ├── analytics/
│   │   │   ├── __init__.py
│   │   │   ├── performance.py
│   │   │   ├── slippage.py
│   │   │   ├── trade_logger.py
│   │   │   ├── trade_replay.py
│   │   ├── config/
│   │   │   ├── config.py
│   │   ├── core/
│   │   │   ├── context.py
│   │   │   ├── health_monitor.py
│   │   │   ├── regime_engine.py
│   │   ├── data/
│   │   │   ├── candle_builder.py
│   │   │   ├── data_manager.py
│   │   ├── diagnostics/
│   │   │   ├── __init__.py
│   │   │   ├── eod_report.py
│   │   │   ├── trade_journal.py
│   │   ├── execution/
│   │   │   ├── broker.py
│   │   │   ├── execution_engine.py
│   │   │   ├── filters.py
│   │   │   ├── order_manager.py
│   │   │   ├── profit_manager.py
│   │   ├── portfolio/
│   │   │   ├── allocator.py
│   │   ├── risk/
│   │   │   ├── risk_manager.py
│   │   ├── scalping/
│   │   │   ├── __init__.py
│   │   │   ├── scalp_engine.py
│   │   ├── services/
│   │   │   ├── dashboard.py
│   │   │   ├── notifier.py
│   │   │   ├── trade_logger.py
│   ├── logs/
│   ├── ml/
│   │   ├── __init__.py
│   │   ├── dataset_builder.py
│   │   ├── dataset_builder_v2.py
│   │   ├── day_classifier.py
│   │   ├── feature_config.py
│   │   ├── feedback_trainer.py
│   │   ├── indicators.py
│   │   ├── ml_intraday_learner.py
│   │   ├── predictor_champion.py
│   │   ├── relabel_dual_champion.py
│   │   ├── trainer_v2_champion.py
│   │   ├── walk_forward_validator.py
│   │   ├── models/
│   │   │   ├── champion_ce_lgbm.pkl
│   │   │   ├── champion_ce_lgbm_features.txt
│   │   │   ├── champion_ce_lgbm_threshold.txt
│   │   │   ├── champion_pe_lgbm.pkl
│   │   │   ├── champion_pe_lgbm_features.txt
│   │   │   ├── champion_pe_lgbm_threshold.txt
│   │   │   ├── day_classifier_labels.csv
│   │   │   ├── day_classifier_lgbm.pkl
│   │   │   ├── model.pkl
│   │   │   ├── training_dataset.csv
│   │   │   ├── training_dataset_labeled.csv
│   │   │   ├── training_dataset_trade.csv
│   │   │   ├── training_dataset_v2.csv
│   ├── scripts/
│   │   ├── cleanup_project.py
│   ├── telegram/
│   │   ├── __init__.py
│   │   ├── messages.py
│   │   ├── notifier.py
