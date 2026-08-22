import pandas as pd
p=r"d:\All Bots\trading_system\data\trades\trade_log_2026_W34.csv"
df=pd.read_csv(p,dtype=str)
sub=df[(df.date=="2026-08-19")&(df.entry_time=="14:34:16")]
print("14:34:16 group exit_reason counts:",sub.exit_reason.value_counts().to_dict())
print("exit_time range:",sub.exit_time.min(),"->",sub.exit_time.max())
print("pnl range:",sub.pnl.astype(float).min(),"->",sub.pnl.astype(float).max(),"sum:",sub.pnl.astype(float).sum())
print("last 3 rows:");print(sub[["trade_id","exit_time","exit_price","pnl","peak_pnl","exit_reason"]].tail(3).to_string())
s2=df[(df.date=="2026-08-19")&(df.entry_time=="14:30:32")]
print("14:30:32 group:",len(s2),"reasons:",s2.exit_reason.value_counts().to_dict(),"exit range:",s2.exit_time.min(),"->",s2.exit_time.max())
print("08-19 distinct entries:",df[df.date=="2026-08-19"].groupby(["entry_time","symbol"]).ngroups)
