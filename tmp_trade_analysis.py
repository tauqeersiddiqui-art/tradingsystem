import csv
R = list(csv.DictReader(open("data/trades/trade_log_2026_W27.csv")))
def g(x):
    try: return float(x)
    except: return 0.0
C = 66  # round-trip cost per trade
print(f"{'day':10} {'grp':5} {'n':>3} {'WR':>4} {'gross':>7} {'NET':>7} {'avgW':>6} {'avgL':>6} {'MFEleft':>7} {'MFE0':>4}")
for day in ("2026-06-29", "2026-06-30"):
    for grp, sel in (("SCALP", lambda r: r["regime"] == "SCALP"),
                     ("MAIN",  lambda r: r["regime"] != "SCALP")):
        s = [r for r in R if r["date"] == day and sel(r)]
        if not s: continue
        n = len(s)
        gross = sum(g(r["pnl"]) for r in s)
        wins = [r for r in s if g(r["pnl"]) > 0]
        loss = [r for r in s if g(r["pnl"]) <= 0]
        aw = sum(g(r["pnl"]) for r in wins) / len(wins) if wins else 0
        al = sum(g(r["pnl"]) for r in loss) / len(loss) if loss else 0
        mfe_left = sum(g(r["MFE"]) for r in s) - gross
        mfe0 = sum(1 for r in s if g(r["MFE"]) == 0)
        net = gross - C * n
        print(f"{day:10} {grp:5} {n:3} {len(wins)/n*100:3.0f}% {gross:7.0f} {net:7.0f} {aw:6.0f} {al:6.0f} {mfe_left:7.0f} {mfe0:4}")
