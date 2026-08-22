import pandas as pd,numpy as np
R=r"d:\All Bots\trading_system\data\historical"
raw=open(R+"\\nifty_1m_full.csv",encoding="utf-8").read().splitlines()
T=[l[:19] for l in raw[1:] if l[10]=="T"]
print("T-fmt rows:",len(T),"| first:",T[0],"| last:",T[-1])
df=pd.read_csv(R+"\\nifty_1m_full.csv")
try:
    pd.to_datetime(df["date"])
    print("PLAIN to_datetime: OK (no error)")
except Exception as e:
    print("PLAIN to_datetime RAISES:",type(e).__name__,str(e)[:140])
df["date"]=pd.to_datetime(df["date"],format="mixed")
df=df.sort_values("date").reset_index(drop=True)
flat=((df.high==df.low)&(df.open==df.low)&(df.close==df.high)).values
idx=np.where(flat)[0]
runs=[]
if len(idx):
    brk=np.where(np.diff(idx)>1)[0]+1
    segs=[s for s in np.split(idx,brk) if len(s)>=5]
    for s in segs:
        runs.append((str(df.date.iloc[s[0]]),str(df.date.iloc[s[-1]]),int(len(s))))
print("flat runs>=5:",len(runs))
for r in runs:print("  ",r)
dd=df.date.dt.normalize().unique()
dd=np.sort(dd)
gaps=[]
for i in range(1,len(dd)):
    delta=(dd[i]-dd[i-1]).days
    if delta>4:
        gaps.append((str(dd[i-1])[:10],str(dd[i])[:10],delta))
print("trading-day gaps>4cal days:",len(gaps))
for g in gaps[-12:]:print("  ",g)
per=df.groupby(df.date.dt.date).size()
print("min-bar day:",per.idxmin(),int(per.min()),"| days<200 bars:",(per<200).sum())
print("days w/ <375 bars:",(per<375).sum(),"| =375:",(per==375).sum(),"| =376:",(per==376).sum(),"| >376:",(per>376).sum())
lb=df.groupby(df.date.dt.date).date.last().dt.strftime("%H:%M")
print("last-bar 15:29:",(lb=="15:29").sum(),"| 15:30:",(lb=="15:30").sum(),"| 15:31:",(lb=="15:31").sum())
d2=df[df["date"]>=pd.Timestamp("2021-01-01")].reset_index(drop=True)
mins=d2["date"].dt.hour.values*60+d2["date"].dt.minute.values
sess=((mins>=570)&(mins<660))|((mins>=840)&(mins<915))
days=d2["date"].dt.date.values
n=len(d2);L=12
si=np.where(sess)[0]
trunc=0;cross=0;exT=[];exC=[]
for i in si:
    j=i+L
    if j>=n:
        trunc+=1
        if len(exT)<5:exT.append(str(d2.date.iloc[i]))
        continue
    if days[j]!=days[i]:
        cross+=1
        if len(exC)<5:exC.append(str(d2.date.iloc[i]))
print("session bars since 2021:",len(si),"| truncated(no 12 fwd):",trunc,"| cross-day lookahead:",cross)
print("trunc ex:",exT);print("cross ex:",exC)
dl=np.diff(d2["date"].values).astype("timedelta64[s]").astype(float)
samed=np.array([days[k]==days[k-1] for k in range(1,n)])
intgap=(dl[1:] if False else dl)>61
print("post-2021 intra-day gaps>61s:",int((intgap&samed)).sum())
dd=np.sort(df.date.dt.normalize().unique())
delta=np.diff(dd).astype("timedelta64[D]").astype(int)
big=np.where(delta>4)[0]
print("trading-day gaps>4cal days:",len(big))
for k in big[-10:]:
    print("  ",str(dd[k])[:10],"->",str(dd[k+1])[:10],"|",int(delta[k]),"cal days")
per=df.groupby(df.date.dt.date).size()
print("min-bar day:",per.idxmin(),int(per.min()),"| days<200 bars:",int((per<200).sum()))
print("<375:",int((per<375).sum()),"| =375:",int((per==375).sum()),"| =376:",int((per==376).sum()),"| >376:",int((per>376).sum()))
lb=df.groupby(df.date.dt.date).date.last().dt.strftime("%H:%M")
print("last-bar 15:29:",int((lb=="15:29").sum()),"| 15:30:",int((lb=="15:30").sum()),"| 15:31:",int((lb=="15:31").sum()))
