# telegram/notifier.py
# Telegram Notifier – Production Safe + Trade-Only Channel Mode

import os
import requests
from dotenv import load_dotenv

# -------------------------------------------------
# Load ENV
# -------------------------------------------------

PROJECT_ROOT = os.getcwd()
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(ENV_PATH, override=True)

# -------------------------------------------------
# ENV VARS
# -------------------------------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOT_CHAT_ID = os.getenv("TELEGRAM_BOT_CHAT_ID", "").strip()
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
AUTHORIZED_USER_ID = os.getenv("TELEGRAM_ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")

if not BOT_CHAT_ID:
    raise RuntimeError("TELEGRAM_BOT_CHAT_ID missing")

if not CHANNEL_ID:
    raise RuntimeError("TELEGRAM_CHANNEL_ID missing")

if not AUTHORIZED_USER_ID:
    raise RuntimeError("TELEGRAM_ADMIN_ID missing in .env")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
SEND_URL = f"{API_URL}/sendMessage"
GET_UPDATES_URL = f"{API_URL}/getUpdates"
ANSWER_CALLBACK_URL = f"{API_URL}/answerCallbackQuery"
EDIT_MARKUP_URL = f"{API_URL}/editMessageReplyMarkup"
EDIT_MESSAGE_URL = f"{API_URL}/editMessageText"

# -------------------------------------------------
# Manual Exit State
# -------------------------------------------------

MANUAL_EXIT_REQUESTED = False
_last_update_id = None
_last_trade_message_id = None
_last_edited_text = None
# -------------------------------------------------
# Core Sender
# -------------------------------------------------

def _send(chat_id, msg, parse_mode="HTML", reply_markup=None):

    payload = {
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": parse_mode
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        response = requests.post(SEND_URL, json=payload, timeout=10)
        data = response.json()

        if not data.get("ok"):
            print("Telegram API Error:", data)
            return None

        return data.get("result")

    except Exception as e:
        print("Telegram exception:", e)
        return None


# -------------------------------------------------
# BOT ONLY (All System Messages)
# -------------------------------------------------

def send_bot(message, parse_mode="HTML"):
    return _send(BOT_CHAT_ID, message, parse_mode=parse_mode)


# -------------------------------------------------
# CHANNEL ONLY (Trade Messages Only)
# -------------------------------------------------

def send_trade_channel(message):
    return _send(CHANNEL_ID, message, parse_mode="HTML")


# -------------------------------------------------
# Edit Existing Bot Message
# -------------------------------------------------

def edit_bot(message_id, new_text, parse_mode="HTML"):

    global _last_edited_text

    # Prevent duplicate edits
    if new_text == _last_edited_text:
        return

    payload = {
        "chat_id": BOT_CHAT_ID,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": parse_mode
    }

    try:
        response = requests.post(EDIT_MESSAGE_URL, json=payload, timeout=10)
        data = response.json()

        if data.get("ok"):
            _last_edited_text = new_text
        else:
            # Ignore "message is not modified" error silently
            if "message is not modified" not in str(data):
                print("Edit message error:", data)

    except Exception as e:
        print("Edit exception:", e)

# -------------------------------------------------
# Send Trade Entry With Manual Exit Button
# -------------------------------------------------

def send_trade_entry_with_exit_button(message):

    global _last_trade_message_id

    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "🔴 MANUAL EXIT",
                    "callback_data": "manual_exit"
                }
            ]
        ]
    }

    # Send to BOT (with button)
    result = _send(
        BOT_CHAT_ID,
        message,
        parse_mode="HTML",
        reply_markup=keyboard
    )

    # Send clean copy to CHANNEL (no button)
    send_trade_channel(message)

    if result:
        _last_trade_message_id = result["message_id"]


# -------------------------------------------------
# Remove Exit Button
# -------------------------------------------------

def remove_exit_button():

    global _last_trade_message_id

    if not _last_trade_message_id:
        return

    try:
        requests.post(
            EDIT_MARKUP_URL,
            json={
                "chat_id": BOT_CHAT_ID,
                "message_id": _last_trade_message_id,
                "reply_markup": None
            },
            timeout=10
        )
    except Exception as e:
        print("Failed removing button:", e)

    _last_trade_message_id = None


# -------------------------------------------------
# Poll Manual Exit Button
# -------------------------------------------------

def poll_manual_exit():

    global _last_update_id
    global MANUAL_EXIT_REQUESTED

    try:

        params = {"timeout": 2}

        if _last_update_id:
            params["offset"] = _last_update_id + 1

        response = requests.get(GET_UPDATES_URL, params=params, timeout=5)
        data = response.json()

        if not data.get("ok"):
            return

        for update in data.get("result", []):

            _last_update_id = update["update_id"]

            if "callback_query" not in update:
                continue

            callback = update["callback_query"]
            user_id = str(callback["from"]["id"])
            callback_id = callback["id"]
            action = callback.get("data")

            # Remove Telegram loading spinner
            requests.post(
                ANSWER_CALLBACK_URL,
                json={"callback_query_id": callback_id},
                timeout=5
            )

            if user_id != AUTHORIZED_USER_ID:
                send_bot("⚠ Unauthorized exit attempt blocked.")
                continue

            if action == "manual_exit":
                MANUAL_EXIT_REQUESTED = True
                send_bot("🔴 Manual exit requested.")

    except Exception as e:
        print("Manual exit polling error:", e)
        print("Manual exit polling error:", e)