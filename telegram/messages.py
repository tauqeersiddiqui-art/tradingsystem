# telegram/messages.py

def format_trade_entry(data):

    return f"""
🟢 TRADE ENTRY CONFIRMED

📌 Instrument : {data['symbol']}
📍 Direction  : {data['side']}
📈 Regime     : {data.get('regime','TREND')}

💰 Entry Price   : {round(data['price'],2)}
📦 Lot Size      : {data['qty']}
🛑 Stop Loss     : {round(data['stop'],2)}  ({data.get('sl_pct','NA')}%)

🧠 ML Probability : {round(data['ml_prob'],3)}
━━━━━━━━━━━━━━━━━━
"""


def format_trade_exit(data):

    return f"""
🔴 TRADE EXIT CONFIRMED

📌 Instrument : {data['symbol']}
📍 Direction  : {data['side']}
📈 Regime     : {data.get('regime','TREND')}

💰 Entry Price  : {round(data['entry_price'],2)}
💰 Exit Price   : {round(data['exit_price'],2)}
📈 Price Move   : {round(data['exit_price'] - data['entry_price'],2)}

📦 Lot Size      : {data['qty']}
💵 Realized PnL  : {round(data['pnl'],2)}

🧠 ML at Entry  : {round(data['ml_prob'],3)}
🛑 Exit Reason  : {data['reason']}
━━━━━━━━━━━━━━━━━━━━
"""