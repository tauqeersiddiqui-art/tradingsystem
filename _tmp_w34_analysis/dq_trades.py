import pandas as pd,re
pd.set_option("display.width",250);pd.set_option("display.max_columns",50)
p=r"d:\All Bots\trading_system\data\trades\trade_log_2026_W34.csv"
df=pd.read_csv(p,dtype=str)
print("rows:",len(df),"cols:",len(df.columns))
pat=re.compile(r"^\d{2}:\d{2}:\d{2}$")
for c in ["entry_time","exit_time"]:
    bad=df[~df[c].fillna("").str.match(pat)]
    print(c,"bad-fmt rows:",len(bad),"samples:",bad[c].unique()[:8])
print("date col bad:",(~df.date.str.match(re.compile(r"^\d{4}-\d{2}-\d{2}$"))).sum())
t=pd.to_datetime(df.date+" "+df.entry_time,errors="coerce")
x=pd.to_datetime(df.date+" "+df.exit_time,errors="coerce")
print("unparseable entry:",int(t.isna().sum()),"| exit:",int(x.isna().sum()))
print("entry seconds==0:",int((t.dt.second==0).sum()),"/",len(t))
print("entry seconds dist:",sorted(t.dt.second.value_counts().head(6).items()))
print("entry on 1m grid (sec==0):",round(float((t.dt.second==0).mean())*100,2),"%")
print("exact-dup rows:",int(df.duplicated().sum()))
g=df.groupby(["date","entry_time","symbol"]).size().sort_values(ascending=False)
print("dup groups (date,entry_time,symbol):",(g>1).sum(),"| top:")
print(g[g>1].head(8).to_string())
big=g.index[0]
sub=df[(df.date==big[0])&(df.entry_time==big[1])&(df.symbol==big[2])]
print("largest group rows:",len(sub),"| unique full rows:",sub.drop_duplicates().shape[0])
vary=[c for c in df.columns if sub[c].nunique(dropna=False)>1]
print("varying cols in dup group:",vary)
print(sub[vary].head(10).to_string())
s19=df[(df.date=="2026-08-19")&(df.entry_time.str.startswith("14:3"))]
print("08-19 entry 14:3x rows:",len(s19),"| by entry_time:",s19.entry_time.value_counts().to_dict())
sub=df[(df.date=="2026-08-19")&(df.entry_time.str.startswith("14:34"))]
print("08-19 14:34 rows:",len(sub),"unique:",sub.drop_duplicates().shape[0])
idx=df.index[(df.date=="2026-08-19")&(df.entry_time.str.startswith("14:34"))]
print("contiguous block:",idx[0],"..",idx[-1],"len:",len(idx),"contig:",idx[-1]-idx[0]+1==len(idx))
vary19=[c for c in df.columns if sub[c].nunique(dropna=False)>1]
print("14:34 varying cols:",vary19)
if vary19: print(sub[vary19].head(6).to_string())
print("rows per date:");print(df.date.value_counts().sort_index().to_string())
print("trade_id uniq per date sample:",df.groupby("date").trade_id.nunique().tail(5).to_dict())
