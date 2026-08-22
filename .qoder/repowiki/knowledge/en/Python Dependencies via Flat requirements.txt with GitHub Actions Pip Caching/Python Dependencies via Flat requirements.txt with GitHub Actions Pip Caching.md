---
kind: dependency_management
name: Python Dependencies via Flat requirements.txt with GitHub Actions Pip Caching
category: dependency_management
scope:
    - '**'
source_files:
    - requirements.txt
    - .github/workflows/trading_morning.yml
    - .github/workflows/trading_afternoon.yml
    - .github/workflows/trading_test.yml
    - .github/workflows/research-tests.yml
    - .gitignore
---

## System / Approach

This repository uses the simplest possible Python dependency management strategy: a single flat `requirements.txt` at the repository root, installed via `pip` in both local development and CI. There is no virtual environment committed to the repo, no lockfile (no `requirements.lock`, `Pipfile.lock`, `poetry.lock`, or `pyproject.toml`), and no vendoring of third-party packages.

## Key Files

- **`requirements.txt`** — The sole source of truth for runtime dependencies. Declares 10 top-level packages with minimum-version pins (`>=`):
  - Broker integration: `kiteconnect>=4.1.0`
  - Auth/OTP: `pyotp>=2.8.0`
  - Config loading: `python-dotenv>=1.0.0`
  - Data/ML stack: `pandas>=2.0.0`, `numpy>=1.24.0`, `lightgbm>=4.0.0`, `scikit-learn>=1.3.0`, `joblib>=1.3.0`
  - HTTP client: `requests>=2.31.0`
  - Browser automation (for broker login): `playwright>=1.44.0`
- **`.github/workflows/trading_morning.yml`**, **`trading_afternoon.yml`**, **`trading_test.yml`**, **`research-tests.yml`** — All CI workflows install dependencies identically:
  ```
  pip install -r requirements.txt
  ```
  They enable `cache: 'pip'` on the `actions/setup-python` step to reuse the pip cache across runs.
- **`.gitignore`** — Excludes `env/`, `venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `catboost_info/`, and other generated artifacts; confirms that virtual environments are intentionally kept local and not versioned.

## Architecture & Conventions

- **Flat manifest**: All dependencies live in one file at the repo root. No per-subpackage `requirements.txt`, no `setup.py`, no `pyproject.toml`, no Poetry/Pipenv usage.
- **Minimum-version pins only**: Every entry uses `>=X.Y.Z` rather than exact pins (`==`) or upper bounds (`<`). This means CI will accept any newer compatible release, but also means builds are not fully reproducible without an external lockfile.
- **No private registry configuration**: There is no `pip.conf`, `~/.config/pip/pip.conf`, `PYPI_URL`, or `--index-url` anywhere in the repo. All packages are expected to be pulled from PyPI.
- **No vendoring**: Third-party code is never checked into the tree; only application code and model artifacts under `ml/models/*.pkl` are committed.
- **CI as the canonical install path**: The GitHub Actions workflows are the authoritative reference for how dependencies are installed in production-like environments. Each workflow runs `pip install -r requirements.txt` after setting up Python, and the test workflow additionally dumps the installed versions via `pip list | grep -E "..."` to verify key packages.
- **Optional extras are not declared**: `playwright` is listed as a hard dependency even though it is only used for headless browser login; there is no optional group (e.g., `[browser]`). Similarly, `catboost` is not listed here — the training pipeline prints an explicit fallback message instructing users to `pip install catboost` when it is missing, indicating it is treated as an optional/developer-only dependency outside the core manifest.

## Conventions & Constraints

- **One manifest, one source of truth**: New dependencies must be added to `requirements.txt`; there is no alternative location.
- **Version policy**: Use `>=` minimum pins. Exact pinning or upper-bound constraints are not observed anywhere in the codebase.
- **Virtual environments are local-only**: `env/` and `venv/` are gitignored; developers are expected to create their own venvs locally.
- **Reproducibility is not enforced by the repo**: Without a lockfile, two installs can resolve different transitive versions. Reproducibility relies on CI's cached pip layers and human discipline to bump versions deliberately.
- **Secrets and credentials are excluded**: `.env` and `access_token.txt` are gitignored, so secrets are loaded at runtime via `python-dotenv` rather than being baked into dependencies or configs.