import os
import shutil

# ================= CONFIG ================= #

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))

# 🔒 SAFE MODE (recommended)
# True  → move to archive/
# False → permanently delete
SAFE_MODE = True

ARCHIVE_DIR = os.path.join(PROJECT_ROOT, "archive")

# ================= TARGETS ================= #

REMOVE_PATHS = [

    # ❌ OLD CORE (conflicts with LiveEngine)
    "engine/core/decision_engine.py",
    "engine/core/signal_router.py",
    "engine/core/signal_aggregator.py",
    "engine/core/event_bus.py",

    # ❌ STRATEGY LAYER (unused)
    "engine/strategies",

    # ❌ UNUSED MODELS
    "engine/models/alpha",
    "engine/models/meta",

    # ❌ DUPLICATE RISK
    "engine/risk/risk_engine.py",

    # ❌ REDUNDANT FILTERS
    "engine/filters",

    # ❌ OLD EVALUATION / DEBUG
    "ml/evaluate_models.py",
    "ml/tmp",

    # ❌ NOTEBOOKS (optional)
    "notebooks",

    # ❌ UNUSED ROOT MODELS
    "models"
]

# ================= CORE FUNCTIONS ================= #

def move_to_archive(src_path):
    rel_path = os.path.relpath(src_path, PROJECT_ROOT)
    dest_path = os.path.join(ARCHIVE_DIR, rel_path)

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    shutil.move(src_path, dest_path)
    print(f"[ARCHIVED] {rel_path}")


def delete_path(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    print(f"[DELETED] {path}")


def process_path(rel_path):
    abs_path = os.path.join(PROJECT_ROOT, rel_path)

    if not os.path.exists(abs_path):
        print(f"[SKIP] Not found: {rel_path}")
        return

    try:
        if SAFE_MODE:
            move_to_archive(abs_path)
        else:
            delete_path(abs_path)

    except Exception as e:
        print(f"[ERROR] {rel_path} → {e}")


# ================= MAIN ================= #

def main():

    print("===================================")
    print("  CLEANUP STARTED")
    print("  Mode:", "SAFE (archive)" if SAFE_MODE else "DELETE")
    print("===================================")

    if SAFE_MODE:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)

    for path in REMOVE_PATHS:
        process_path(path)

    print("===================================")
    print("  CLEANUP COMPLETE")
    print("===================================")


if __name__ == "__main__":
    main()