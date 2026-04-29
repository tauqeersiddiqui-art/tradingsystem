import pandas as pd
import numpy as np
import joblib
from sklearn.metrics import classification_report, roc_auc_score


DATA_PATH = "ml/models/training_dataset_trade.csv"

from ml.feature_config import FEATURE_COLUMNS

def evaluate_model(model_path, label_column, model_name, df):

    print("\n" + "=" * 60)
    print(f"Evaluating {model_name}")
    print("=" * 60)

    X = df[FEATURE_COLUMNS]
    y = df[label_column]

    model = joblib.load(model_path)
    probs = model.predict_proba(X)[:, 1]

    print("\n--- Basic Metrics ---")
    print("ROC AUC:", roc_auc_score(y, probs))

    print("\n--- Classification Report @ 0.15 ---")
    preds = (probs >= 0.15).astype(int)
    print(classification_report(y, preds))

    print("\n--- Threshold Precision Analysis ---")
    thresholds = [0.05, 0.08, 0.10, 0.15, 0.20]

    for th in thresholds:
        mask = probs >= th
        if mask.sum() == 0:
            print(f"Threshold {th}: No signals")
            continue

        precision = y[mask].mean()
        print(f"Threshold {th:.2f} → Signals: {mask.sum():6d} | Precision: {precision:.4f}")

    print("\n--- Probability Bucket Analysis ---")
    bins = np.linspace(0, probs.max(), 10)

    df_eval = pd.DataFrame({
        "prob": probs,
        "actual": y
    })

    df_eval["bucket"] = pd.cut(df_eval["prob"], bins)

    bucket_stats = df_eval.groupby("bucket").agg(
        count=("actual", "count"),
        win_rate=("actual", "mean")
    )

    print(bucket_stats)


def main():

    print("\nLoading dataset...\n")

    df = pd.read_csv(DATA_PATH)
    df = df.dropna()

    evaluate_model(
        "ml/models/champion_ce_lgbm.pkl",
        "label_ce",
        "LightGBM CE",
        df
    )

    evaluate_model(
        "ml/models/champion_pe_lgbm.pkl",
        "label_pe",
        "LightGBM PE",
        df
    )


if __name__ == "__main__":
    main()