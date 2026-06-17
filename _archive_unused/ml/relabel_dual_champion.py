import pandas as pd

# Trade-based labels should reflect moves that can be held through a bar close,
# not just wick touches inside a single minute.
LOOKAHEAD = 12
TARGET_MOVE = 0.0008
STOP_MOVE = 0.0006
MIN_HOLD = 2


def label_direction(entry: float, future: pd.DataFrame, direction: str) -> int:
    for j in range(MIN_HOLD, len(future)):
        close = float(future.iloc[j]["close"])

        if direction == "CE":
            favorable_move = (close - entry) / entry
            adverse_move = (entry - close) / entry
        else:
            favorable_move = (entry - close) / entry
            adverse_move = (close - entry) / entry

        if favorable_move >= TARGET_MOVE:
            return 1
        if adverse_move >= STOP_MOVE:
            return 0

    return 0


print("MODE: TRADE-BASED LABELING (CLOSE CONFIRMATION)")
print("Loading dataset...")
df = pd.read_csv("ml/models/training_dataset.csv")

if df.empty:
    raise ValueError("Dataset empty. Run dataset_builder first.")

df["label_ce"] = 0
df["label_pe"] = 0

print(f"Relabeling {len(df):,} rows...")

for i in range(len(df) - LOOKAHEAD):
    entry = float(df.loc[i, "close"])
    future = df.loc[i + 1:i + LOOKAHEAD]

    df.loc[i, "label_ce"] = label_direction(entry, future, "CE")
    df.loc[i, "label_pe"] = label_direction(entry, future, "PE")

ce_rate = df["label_ce"].mean()
pe_rate = df["label_pe"].mean()

print("\nLabel rates:")
print(f"CE: {ce_rate:.2%}")
print(f"PE: {pe_rate:.2%}")

if ce_rate < 0.15 or pe_rate < 0.15:
    print("Warning: label rate too low, consider reducing TARGET_MOVE")
elif ce_rate > 0.50 or pe_rate > 0.50:
    print("Warning: label rate too high, consider increasing TARGET_MOVE")
else:
    print("OK: label balance looks reasonable")

output_path = "ml/models/training_dataset_trade.csv"
df.to_csv(output_path, index=False)

print(f"\nSaved -> {output_path}")
