import pandas as pd,os
M=r"d:\All Bots\trading_system\ml\models"
for f in sorted(os.listdir(M)):
    p=os.path.join(M,f)
    if os.path.isfile(p):
        print(f,os.path.getmtime(p),os.path.getsize(p))
td=os.path.join(M,"training_dataset.csv")
if os.path.exists(td):
    df=pd.read_csv(td,usecols=["date"])
    d=pd.to_datetime(df["date"])
    print("training_dataset rows:",len(df),"| range:",d.min(),"->",d.max())
    m=d.dt.hour*60+d.dt.minute
    print("rows 15:03-15:14:",int(((m>=903)&(m<=914)).sum()))
