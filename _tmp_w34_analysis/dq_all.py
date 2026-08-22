import pandas as pd,numpy as np,re
R=r"d:\All Bots\trading_system\data\historical"
def load(n):
    p=R+"\\"+n
    df=pd.read_csv(p)
    df["date"]=pd.to_datetime(df["date"],errors="coerce")
    return p,df
fs=re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},")
fT=re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2},")
for n in ["nifty_1m_full.csv","banknifty_1m_full.csv"]:
    p,df=load(n)
    raw=open(p,encoding="utf-8").read().splitlines()
    nsp=sum(1 for l in raw[1:] if fs.match(l));nT=sum(1 for l in raw[1:] if fT.match(l))
    print("="*66);print(n,"| hdr:",raw[0])
    print("lines:",len(raw),"| space-fmt:",nsp,"| T-fmt:",nT,"| other:",len(raw)-1-nsp-nT)
    oth=[l for l in raw[1:] if not(fs.match(l) or fT.match(l))]
    print("other-ex:",oth[:3]);print("tail:",raw[-2:])
    print("NaT:",int(df.date.isna().sum()))
    df=df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    print("rows:",len(df),"| range:",df.date.min(),"->",df.date.max())
    print("dup-ts:",int(df.duplicated("date").sum()))
    d=df.date.diff().dt.total_seconds()
    print("out-of-order:",int((d<0).sum()),"| gaps>61s:",int((d>61).sum()))
    gi=d[d>61].index[:8]
    print("gap-ex:",[(str(df.date.iloc[i-1]),str(df.date.iloc[i])) for i in gi])
    dd=df.date.dt.date
    per=df.groupby(dd).size()
    print("days:",len(per),"| bars/day: mode",int(per.mode()[0]),"min",int(per.min()),"max",int(per.max()))
    fb=df.groupby(dd).date.agg(lambda s:s.iloc[0].strftime("%H:%M")).value_counts()
    lb=df.groupby(dd).date.agg(lambda s:s.iloc[-1].strftime("%H:%M")).value_counts()
    print("first-bar-time dist:",dict(fb.head(4)))
    print("last-bar-time dist:",dict(lb.head(6)))
    sameday=(dd==dd.shift())
    ig=d[(d>61)&sameday]
    print("INTRA-DAY gaps>61s:",int(len(ig)))
    print("intraday-gap-ex:",[(str(df.date.iloc[i-1]),str(df.date.iloc[i])) for i in ig.index[:10]])
    o=df[["open","high","low","close"]]
    print("bad-high:",int((df.high<o[["open","close"]].max(axis=1)).sum()),
          "| bad-low:",int((df.low>o[["open","close"]].min(axis=1)).sum()),
          "| h<l:",int((df.high<df.low).sum()),
          "| <=0:",int((o<=0).any(axis=1).sum()))
    flat=(df.high==df.low)&(df.open==df.low)&(df.close==df.high)
    g=(~flat).cumsum();runs=flat.groupby(g).sum()
    print("flat-bars:",int(flat.sum()),"| max-run:",int(runs.max()),"| runs>=5:",int((runs>=5).sum()),"| runs>=60:",int((runs>=60).sum()))
    print("vol-uniq:",sorted(df.volume.unique())[:5],"| zero%:",round(float((df.volume==0).mean())*100,3))
print("="*66);print("AUG 2026 DETAIL (nifty file)")
_,dfN=load("nifty_1m_full.csv")
dfN=dfN.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
aug=dfN[(dfN.date>=pd.Timestamp("2026-08-01"))&(dfN.date<=pd.Timestamp("2026-08-21 23:59"))]
tot=0
for d in range(1,22):
    day=pd.Timestamp(2026,8,d)
    if day.weekday()>=5:
        continue
    exp=pd.date_range(day+pd.Timedelta(hours=9,minutes=15),day+pd.Timedelta(hours=15,minutes=30),freq="min")
    got=set(aug.date[aug.date.dt.date==day.date()])
    miss=[str(x)[11:16] for x in exp if x not in got]
    extra=[str(x)[11:16] for x in sorted(got) if x not in set(exp)]
    tot+=len(miss)
    print(day.strftime("%m-%d %a"),"bars:",len(got),"missing:",len(miss),miss[:8],"extra:",extra)
print("TOTAL missing bars Aug1-21:",tot)
print("="*66);print("FILE OVERLAP CHECK")
a=open(R+"\\nifty_1m_full.csv",encoding="utf-8").read().splitlines()
b=open(R+"\\banknifty_1m_full.csv",encoding="utf-8").read().splitlines()
print("bn rows:",len(b),"| bn == nifty[:len(b)] identical:",a[:len(b)]==b)
print("nifty rows beyond bn:",len(a)-len(b))
print("nifty rows w/ 15:31 bar (whole file):",sum(1 for l in a[1:] if l[11:16]=="15:31"))
print("nifty rows outside 09:15-15:31:",sum(1 for l in a[1:] if not("09:15"<=l[11:16]<="15:31")))
