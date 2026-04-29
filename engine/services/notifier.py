import os
import requests
import time

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_BOT_CHAT_ID")

_last = 0


def notify(msg, force=False):
    global _last

    if not TOKEN or not CHAT_ID:
        print(msg)
        return

    now = time.time()

    if not force and now - _last < 5:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg}
        )
        _last = now
    except:
        pass