R=r"d:\All Bots\trading_system\data\historical"
raw=open(R+"\\nifty_1m_full.csv",encoding="utf-8").read().splitlines()
a=[l for l in raw[1:] if l.startswith("2026-08-21 10:51:00")]
b=[l for l in raw[1:] if l.startswith("2026-08-21T10:51:00")]
print("space 10:51 rows:",a)
print("T 10:51 rows:",b)
import collections
keys=[l[:19].replace("T"," ") for l in raw[1:]]
c=collections.Counter(keys)
d={k:v for k,v in c.items() if v>1}
print("dup timestamps (mixed-fmt aware):",len(d))
for k,v in list(d.items())[:6]:print("  ",k,v)
