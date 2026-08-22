import pandas as pd,numpy as np
R=r"d:\All Bots\trading_system\data\historical"
df=pd.read_csv(R+"\\nifty_1m_full.csv")
df["date"]=pd.to_datetime(df["date"],format="mixed")
df=df.sort_values("date").reset_index(drop=True)
dd=np.sort(df.date.dt.normalize().unique())
delta=np.diff(dd).astype("timedelta64[D]").astype(int)
big=np.where(delta>4)[0]
print("trading-day gaps>4cal days:",len(big))
for k in big[-10:]:
    print("  ",str(dd[k])[:10],"->",str(dd[k+1])[:10],"|",int(delta[k]),"cal days")
per=df.groupby(df.date.dt.date).size()
print("min-bar day:",per.idxmin(),int(per.min()),"| days<200:",int((per<200).sum()))
print("<375:",int((per<375).sum()),"| =375:",int((per==375).sum()),"| =376:",int((per==376).sum()),"| >376:",int((per>376).sum()))
lb=df.groupby(df.date.dt.date).date.last().dt.strftime("%H:%M")
print("last-bar 15:29:",int((lb=="15:29").sum()),"| 15:30:",int((lb=="15:30").sum()),"| 15:31:",int((lb=="15:31").sum()))
d2=df[df["date"]>=pd.Timestamp("2021-01-01")].reset_index(drop=True)
mins=d2["date"].dt.hour.values*60+d2["date"].dt.minute.values
sess=((mins>=570)&(mins<660))|((mins>=840)&(mins<915))
days=d2["date"].dt.date.values
n=len(d2);L=12
si=np.where(sess)[0]
fwd=np.minimum(si+L,n-1)
trunc=int((si+L>=n).sum())
cross=int(((days[fwd]!=days[si])&(si+L<n)).sum())
print("rows since 2021:",n,"| session bars:",len(si),"| truncated(no 12 fwd):",trunc,"| cross-day lookahead:",cross)
ex=[str(d2.date.iloc[i]) for i in si[(days[fwd]!=days[si])&(si+L<n)][:6]]
print("cross-day ex:",ex)
ex2=[str(d2.date.iloc[i]) for i in si[si+L>=n][:6]]
print("truncated ex:",ex2)
dt=d2["date"].values
dsec=np.diff(dt).astype("timedelta64[s]").astype(float)
samed=days[1:]==days[:-1]
print("post-2021 intra-day gaps>61s:",int(((dsec>61)&samed).sum()))
bad=[(str(d2.date.iloc[k]),str(d2.date.iloc[k+1])) for k in np.where((dsec>61)&samed)[0][:10]]
print("gap bars:",bad)
