# Experimental ML Pipeline V2

This folder is isolated from the production trading system.

Production artifacts that must remain untouched:

- `ml/models/champion_ce_lgbm.pkl`
- `ml/models/champion_pe_lgbm.pkl`
- `ml/models/champion_ce_lgbm_threshold.txt`
- `ml/models/champion_pe_lgbm_threshold.txt`
- `engine/live_engine.py`
- `ml/predictor_champion.py`

All V2 outputs must be written under:

- `experimental/ml_pipeline_v2/artifacts/`

## Purpose

Pipeline V2 separates probability spaces that were previously mixed:

1. Directional probability: "Will the market move in this direction?"
2. Trade quality: "Is this trade likely to be profitable after costs?"
3. Execution and sizing: "How much risk should be allocated?"
4. Exit quality: "How should the trade be managed after entry?"

The June 19 cost-aware model was statistically valid for rare profitable-after-cost events, but incompatible with the live engine's expected directional probability. V2 explicitly models those as separate stages.

## Documents

- `docs/phase1_root_cause_validation.md`
- `docs/architecture.md`
- `docs/validation_and_research_plan.md`

## Scripts

- `scripts/run_phase1_audit.py`
- `scripts/train_pipeline_v2.py`
- `scripts/validate_pipeline_v2.py`

These scripts do not replace production models. They write experimental reports and candidate artifacts only.

## Basic Usage

From the repository root:

```powershell
python experimental/ml_pipeline_v2/scripts/run_phase1_audit.py
python experimental/ml_pipeline_v2/scripts/train_pipeline_v2.py --dry-run
python experimental/ml_pipeline_v2/scripts/validate_pipeline_v2.py --dry-run
```

Remove `--dry-run` only when you want to write V2 candidate artifacts under `experimental/ml_pipeline_v2/artifacts/`.

