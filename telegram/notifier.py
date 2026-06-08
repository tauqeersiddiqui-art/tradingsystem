# telegram/notifier.py
# Full control interface: persistent dual dashboards, trade confirmation, text commands.
#
# Two edit-in-place Telegram messages:
#   1. "AI ENGINE" — ML bias, technicals, decision reasoning, expectancy
#   2. "LIVE STATUS" — current position, trailing SL, market internals
#
# Message IDs are persisted to .telegram_state.json so they survive restarts
# and the same two messages are always edited in place (never new messages).

import os
import json
import time
import requests
from dotenv import load_dotenv

PROJECT_ROOT = os.getcwd()
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

BOT_TOKEN          = os.getenv("TELEGRAM_BOT_TOKEN",  "").strip()
BOT_CHAT_ID        = os.getenv("TELEGRAM_BOT_CHAT_ID","").strip()
CHANNEL_ID         = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
AUTHORIZED_USER_ID = os.getenv("TELEGRAM_ADMIN_ID",   "").strip()

if not BOT_TOKEN:          raise RuntimeError("TELEGRAM_BOT_TOKEN missing")
if not BOT_CHAT_ID:        raise RuntimeError("TELEGRAM_BOT_CHAT_ID missing")
if not CHANNEL_ID:         raise RuntimeError("TELEGRAM_CHANNEL_ID missing")
if not AUTHORIZED_USER_ID: raise RuntimeError("TELEGRAM_ADMIN_ID missing")

API_URL             = f"https://api.telegram.org/bot{BOT_TOKEN}"
SEND_URL            = f"{API_URL}/sendMessage"
EDIT_MESSAGE_URL    = f"{API_URL}/editMessageText"
EDIT_MARKUP_URL     = f"{API_URL}/editMessageReplyMarkup"
GET_UPDATES_URL     = f"{API_URL}/getUpdates"
ANSWER_CALLBACK_URL = f"{API_URL}/answerCallbackQuery"

_STATE_FILE = os.path.join(PROJECT_ROOT, ".telegram_state.json")

# ─────────────────────────────────────────────────────────────────────
# SHARED STATE  (read by master_runner every cycle)
# ─────────────────────────────────────────────────────────────────────
MANUAL_EXIT_REQUESTED = False
ENGINE_PAUSED         = False
ENGINE_STOP_REQUESTED = False
CE_THRESHOLD_OVERRIDE = None
PE_THRESHOLD_OVERRIDE = None

_pending_confirm_id   = None
_pending_confirm_resp = None

# ─────────────────────────────────────────────────────────────────────
# PERSISTENT MESSAGE IDs  — survives process restarts
# ─────────────────────────────────────────────────────────────────────
_engine_msg_id = None   # "AI ENGINE" dashboard
_market_msg_id = None   # "LIVE STATUS" dashboard
_trade_msg_id  = None   # current trade entry message (EXIT button)

_last_update_id = None
_last_edited    = {}    # message_id → last text (per-slot dedup)


def _load_state():
    global _engine_msg_id, _market_msg_id
    try:
        with open(_STATE_FILE) as f:
            d = json.load(f)
        _engine_msg_id = d.get("engine")
        _market_msg_id = d.get("market")
    except Exception:
        pass


def _save_state():
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump({"engine": _engine_msg_id, "market": _market_msg_id}, f)
    except Exception:
        pass


_load_state()   # restore IDs from previous session on import


# ─────────────────────────────────────────────────────────────────────
# LOW-LEVEL HELPERS
# ─────────────────────────────────────────────────────────────────────

def _send(chat_id, text, parse_mode="HTML", reply_markup=None, disable_web_page_preview=True):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(SEND_URL, json=payload, timeout=10)
        d = r.json()
        if not d.get("ok"):
            print("[TG] Send error:", d)
            return None
        return d.get("result")
    except Exception as e:
        print("[TG] Send exception:", e)
        return None


def _edit(message_id, text, parse_mode="HTML"):
    """Edit a message. Per-slot dedup: skip if text unchanged since last edit."""
    if _last_edited.get(message_id) == text:
        return
    try:
        r = requests.post(EDIT_MESSAGE_URL, json={
            "chat_id": BOT_CHAT_ID,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }, timeout=10)
        d = r.json()
        if d.get("ok"):
            _last_edited[message_id] = text
        elif "message is not modified" in str(d):
            _last_edited[message_id] = text  # mark as synced
        else:
            print("[TG] Edit error:", d)
    except Exception as e:
        print("[TG] Edit exception:", e)


def _answer_cb(callback_id, text=""):
    try:
        requests.post(ANSWER_CALLBACK_URL,
                      json={"callback_query_id": callback_id, "text": text},
                      timeout=5)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# PUBLIC SENDERS
# ─────────────────────────────────────────────────────────────────────

def send_bot(message, parse_mode="HTML"):
    return _send(BOT_CHAT_ID, message, parse_mode=parse_mode)


def send_trade_channel(message):
    return _send(CHANNEL_ID, message, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────
# DUAL DASHBOARD  — two persistent edit-in-place messages
# ─────────────────────────────────────────────────────────────────────

def send_or_edit_engine_dashboard(text: str):
    """AI Engine Status — ML bias, technicals, decision, expectancy."""
    global _engine_msg_id
    if _engine_msg_id is None:
        result = _send(BOT_CHAT_ID, text)
        if result:
            _engine_msg_id = result["message_id"]
            _save_state()
    else:
        _edit(_engine_msg_id, text)


def send_or_edit_market_dashboard(text: str, reply_markup=None):
    """Live Market Status — position card, ORB, market internals."""
    global _market_msg_id
    if _market_msg_id is None:
        result = _send(BOT_CHAT_ID, text, reply_markup=reply_markup)
        if result:
            _market_msg_id = result["message_id"]
            _save_state()
    else:
        if reply_markup:
            # Need to edit both text and markup
            try:
                requests.post(EDIT_MESSAGE_URL, json={
                    "chat_id": BOT_CHAT_ID,
                    "message_id": _market_msg_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup,
                    "disable_web_page_preview": True,
                }, timeout=10)
                _last_edited[_market_msg_id] = text
            except Exception as e:
                print("[TG] Market dashboard edit error:", e)
        else:
            _edit(_market_msg_id, text)


# Keep old single-dashboard name as alias for backward compat
def send_or_edit_dashboard(text: str, parse_mode: str = "HTML"):
    send_or_edit_engine_dashboard(text)


def reset_dashboard():
    """Only call this when you explicitly want new dashboard messages (e.g. new day)."""
    global _engine_msg_id, _market_msg_id
    _engine_msg_id = None
    _market_msg_id = None
    _last_edited.clear()
    _save_state()


# ─────────────────────────────────────────────────────────────────────
# TRADE ENTRY  — EXIT button attached to trade message
# ─────────────────────────────────────────────────────────────────────

def send_trade_entry_with_exit_button(message):
    global _trade_msg_id
    kb = {"inline_keyboard": [[{"text": "🔴 EXIT NOW", "callback_data": "manual_exit"}]]}
    result = _send(BOT_CHAT_ID, message, reply_markup=kb)
    send_trade_channel(message)
    if result:
        _trade_msg_id = result["message_id"]


def remove_exit_button():
    global _trade_msg_id
    if not _trade_msg_id:
        return
    try:
        requests.post(EDIT_MARKUP_URL, json={
            "chat_id": BOT_CHAT_ID,
            "message_id": _trade_msg_id,
            "reply_markup": {"inline_keyboard": []},
        }, timeout=10)
    except Exception:
        pass
    _trade_msg_id = None


# ─────────────────────────────────────────────────────────────────────
# TRADE CONFIRMATION  — YES / SKIP with 30s auto-execute timeout
# ─────────────────────────────────────────────────────────────────────

def ask_trade_permission(side: str, price: float, ml_prob: float,
                         stop: float, target: float) -> bool:
    global _pending_confirm_id, _pending_confirm_resp

    uid = f"confirm_{int(time.time())}"
    _pending_confirm_id   = uid
    _pending_confirm_resp = None

    msg = (
        f"<b>SIGNAL: {side}</b>\n"
        f"Entry ~{price:.1f}  |  ML {ml_prob:.0%}\n"
        f"SL {stop:.1f}  Target {target:.1f}\n"
        f"<i>Auto-execute in 30s</i>"
    )
    kb = {"inline_keyboard": [[
        {"text": "YES — Execute", "callback_data": f"trade_yes_{uid}"},
        {"text": "SKIP",          "callback_data": f"trade_skip_{uid}"},
    ]]}
    _send(BOT_CHAT_ID, msg, reply_markup=kb)

    deadline = time.time() + 30
    while time.time() < deadline:
        poll_commands()
        if _pending_confirm_resp is not None:
            break
        time.sleep(0.5)

    resp = _pending_confirm_resp
    _pending_confirm_id   = None
    _pending_confirm_resp = None
    return resp != "skip"


# ─────────────────────────────────────────────────────────────────────
# UNIFIED COMMAND POLLER
# ─────────────────────────────────────────────────────────────────────

_HELP_TEXT = (
    "<b>Commands</b>\n"
    "/status   — engine snapshot\n"
    "/pause    — stop new entries\n"
    "/resume   — re-enable entries\n"
    "/stop     — halt after current trade\n"
    "/ce 0.65  — override CE threshold\n"
    "/pe 0.68  — override PE threshold\n"
    "/reset    — clear threshold overrides\n"
    "/newdash  — create fresh dashboard messages\n"
    "/help     — this message"
)


def poll_commands(status_cb=None):
    global _last_update_id, MANUAL_EXIT_REQUESTED, ENGINE_PAUSED
    global ENGINE_STOP_REQUESTED, CE_THRESHOLD_OVERRIDE, PE_THRESHOLD_OVERRIDE
    global _pending_confirm_resp

    try:
        params = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
        if _last_update_id:
            params["offset"] = _last_update_id + 1

        r = requests.get(GET_UPDATES_URL, params=params, timeout=5)
        data = r.json()
        if not data.get("ok"):
            return

        for update in data.get("result", []):
            _last_update_id = update["update_id"]

            if "callback_query" in update:
                cb     = update["callback_query"]
                uid    = str(cb["from"]["id"])
                cb_id  = cb["id"]
                action = cb.get("data", "")
                _answer_cb(cb_id)

                if uid != AUTHORIZED_USER_ID:
                    continue

                if action == "manual_exit":
                    MANUAL_EXIT_REQUESTED = True
                    send_bot("Manual exit requested.")

                elif action.startswith("trade_yes_"):
                    if _pending_confirm_id and action.endswith(_pending_confirm_id.split("_", 1)[1]):
                        _pending_confirm_resp = "yes"
                        send_bot("Executing trade.")

                elif action.startswith("trade_skip_"):
                    if _pending_confirm_id and action.endswith(_pending_confirm_id.split("_", 1)[1]):
                        _pending_confirm_resp = "skip"
                        send_bot("Signal skipped.")

            elif "message" in update:
                msg  = update["message"]
                uid  = str(msg.get("from", {}).get("id", ""))
                text = msg.get("text", "").strip()

                if uid != AUTHORIZED_USER_ID or not text.startswith("/"):
                    continue

                parts = text.lower().split()
                cmd   = parts[0]

                if cmd == "/pause":
                    ENGINE_PAUSED = True
                    send_bot("Engine PAUSED — no new entries. /resume to restart.")

                elif cmd == "/resume":
                    ENGINE_PAUSED = False
                    send_bot("Engine RESUMED.")

                elif cmd == "/stop":
                    ENGINE_STOP_REQUESTED = True
                    send_bot("Engine STOP after current trade.")

                elif cmd == "/ce" and len(parts) == 2:
                    try:
                        CE_THRESHOLD_OVERRIDE = float(parts[1])
                        send_bot(f"CE threshold override: {CE_THRESHOLD_OVERRIDE:.2f}")
                    except ValueError:
                        send_bot("Usage: /ce 0.65")

                elif cmd == "/pe" and len(parts) == 2:
                    try:
                        PE_THRESHOLD_OVERRIDE = float(parts[1])
                        send_bot(f"PE threshold override: {PE_THRESHOLD_OVERRIDE:.2f}")
                    except ValueError:
                        send_bot("Usage: /pe 0.68")

                elif cmd == "/reset":
                    CE_THRESHOLD_OVERRIDE = None
                    PE_THRESHOLD_OVERRIDE = None
                    send_bot("Threshold overrides cleared.")

                elif cmd == "/newdash":
                    reset_dashboard()
                    send_bot("Dashboard reset — fresh messages will be created next cycle.")

                elif cmd == "/status":
                    info = status_cb() if status_cb else "Status unavailable."
                    send_bot(info)

                elif cmd == "/help":
                    send_bot(_HELP_TEXT)

                else:
                    send_bot(f"Unknown: {cmd}\n" + _HELP_TEXT)

    except Exception as e:
        print("[TG] poll_commands error:", e)


def poll_manual_exit(status_cb=None):
    poll_commands(status_cb=status_cb)
