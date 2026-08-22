---
kind: external_dependency
name: LightGBM ML model serving
slug: lightgbm
category: external_dependency
category_hints:
    - framework_behavior
scope:
    - '**'
---

### Identity

### Role in this repo
- The `IntradayMLLearner` loads a trained LightGBM model and produces probability scores that gate both the main strategy entries and the scalp layer (`SCALP_ML_MIN_PROB`).

### Stable usage notes
- Model files are persisted externally and loaded at runtime; the code treats the model as a black-box predictor returning probabilities — do not assume feature names or training pipeline details without inspecting the ml module.
- Verify exact API/params against the official LightGBM docs.