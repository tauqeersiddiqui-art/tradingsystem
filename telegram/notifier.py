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
import logging
import time
import threading
import queue
import re
import requests
from dotenv import load_dotenv

_log = logging.getLogger("tg.notifier")

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
TRADE_QUIET_MODE      = False

_pending_confirm_id   = None
_pending_confirm_resp = None


def set_trade_quiet(enabled: bool):
    """Suppress non-live Telegram sends while a trade is open."""
    global TRADE_QUIET_MODE
    TRADE_QUIET_MODE = bool(enabled)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

# ─────────────────────────────────────────────────────────────────────
# BACKGROUND TELEGRAM THREAD
# Telegram I/O runs in a daemon thread with a queue.
# Engine loop calls are non-blocking — never stall trading.
# ─────────────────────────────────────────────────────────────────────
_tg_queue          = queue.Queue(maxsize=50)
_live_update_lock  = threading.Lock()
_pending_trade_live = None
_trade_live_queued  = False
_pending_scalp_live = None
_scalp_live_queued  = False
_poll_interval     = 3.0
_poll_interval_max = 60.0
_tg_http_timeout   = _env_float("TG_HTTP_TIMEOUT_SECONDS", 2.0)
_tg_poll_timeout   = _env_float("TG_POLL_TIMEOUT_SECONDS", 5.0)
try:
    _tg_max_tasks_per_tick = max(1, int(os.getenv("TG_MAX_TASKS_PER_TICK", "5")))
except (TypeError, ValueError):
    _tg_max_tasks_per_tick = 5
_last_poll_ts      = 0.0
_poll_fail_count   = 0
_last_queue_warn_ts = 0.0

_http = requests.Session()
_http.trust_env = False
_BOT_URL_RE = re.compile(r"/bot[^/\s]+")


def _redact(value) -> str:
    text = str(value)
    if BOT_TOKEN:
        text = text.replace(BOT_TOKEN, "<redacted>")
    return _BOT_URL_RE.sub("/bot<redacted>", text)


def _tg_worker():
    global _last_poll_ts, _poll_fail_count
    current_interval = _poll_interval
    while True:
        try:
            processed = 0
            while processed < _tg_max_tasks_per_tick:
                task = None
                try:
                    task = _tg_queue.get_nowait()
                    fn, args, kwargs = task
                    fn(*args, **kwargs)
                except queue.Empty:
                    break
                except Exception as e:
                    _log.warning("[TG-thread] send error: %s", _redact(e))
                finally:
                    if task is not None:
                        _tg_queue.task_done()
                        processed += 1

            now = time.time()
            if now - _last_poll_ts >= current_interval:
                _last_poll_ts = now
                before = _poll_fail_count
                _poll_commands_internal()
                if _poll_fail_count > before:
                    current_interval = min(current_interval * 2, _poll_interval_max)
                else:
                    current_interval = _poll_interval

        except Exception as e:
            _log.warning("[TG-thread] worker error: %s", _redact(e))

        time.sleep(0.5)


_tg_thread = None


def _ensure_thread():
    global _tg_thread
    if _tg_thread is None or not _tg_thread.is_alive():
        _tg_thread = threading.Thread(target=_tg_worker, daemon=True)
        _tg_thread.start()


def _tg_enqueue(fn, *args, **kwargs):
    global _last_queue_warn_ts
    _ensure_thread()
    try:
        _tg_queue.put_nowait((fn, args, kwargs))
        return True
    except queue.Full:
        now = time.time()
        if now - _last_queue_warn_ts > 30:
            _last_queue_warn_ts = now
            _log.warning("[TG] queue full; dropping %s", getattr(fn, "__name__", "task"))
        return False


# ─────────────────────────────────────────────────────────────────────
# PERSISTENT MESSAGE IDs  — survives process restarts
# ─────────────────────────────────────────────────────────────────────
_engine_msg_id = None
_market_msg_id = None
_trade_msg_id  = None
_scalp_msg_id  = None

_last_update_id = None
_last_edited    = {}
_EDIT_GONE      = "GONE"


def _load_state():
    global _engine_msg_id, _market_msg_id, _trade_msg_id, _scalp_msg_id
    try:
        with open(_STATE_FILE) as f:
            d = json.load(f)
        _engine_msg_id = d.get("engine")
        _market_msg_id = d.get("market")
        _trade_msg_id  = d.get("trade")
        _scalp_msg_id  = d.get("scalp")
    except Exception:
        pass


def _save_state():
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump({
                "engine": _engine_msg_id,
                "market": _market_msg_id,
                "trade":  _trade_msg_id,
                "scalp":  _scalp_msg_id,
            }, f)
    except Exception:
        pass


_load_state()


# ─────────────────────────────────────────────────────────────────────
# LOW-LEVEL HELPERS  — direct requests
# ─────────────────────────────────────────────────────────────────────

def _send(chat_id, text, parse_mode="HTML", reply_markup=None, disable_web_page_preview=True):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup if isinstance(reply_markup, str) else json.dumps(reply_markup)
    try:
        r = _http.post(SEND_URL, json=payload, timeout=_tg_http_timeout)
        d = r.json()
        if not d.get("ok"):
            _log.warning("[TG] Send error: %s", _redact(d))
            return None
        return d.get("result")
    except Exception as e:
        _log.warning("[TG] Send exception: %s", _redact(e))
        return None


def _edit(message_id, text, parse_mode="HTML"):
    """Returns True, False (transient), or _EDIT_GONE."""
    if _last_edited.get(message_id) == text:
        return True
    try:
        r = _http.post(EDIT_MESSAGE_URL, json={
            "chat_id": BOT_CHAT_ID,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }, timeout=_tg_http_timeout)
        d = r.json()
        if d.get("ok"):
            _last_edited[message_id] = text
            return True

        desc = str(d.get("description", "")).lower()
        if "message is not modified" in desc:
            _last_edited[message_id] = text
            return True

        if any(phrase in desc for phrase in (
            "message to edit not found",
            "message can't be edited",
            "message_id_invalid",
            "chat not found",
        )):
            _log.warning("[TG] Edit target gone: %s", _redact(d))
            return _EDIT_GONE

        _log.warning("[TG] Edit error: %s", _redact(d))
        return False
    except Exception as e:
        _log.warning("[TG] Edit exception: %s", _redact(e))
        return False


def _edit_with_markup(message_id, text, reply_markup=None):
    """Same return contract as _edit(): True / False / _EDIT_GONE."""
    if _last_edited.get(message_id) == text and not reply_markup:
        return True
    try:
        payload = {
            "chat_id": BOT_CHAT_ID,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup if isinstance(reply_markup, str) else json.dumps(reply_markup)
        r = _http.post(EDIT_MESSAGE_URL, json=payload, timeout=_tg_http_timeout)
        d = r.json()
        if d.get("ok"):
            _last_edited[message_id] = text
            return True

        desc = str(d.get("description", "")).lower()
        if "message is not modified" in desc:
            _last_edited[message_id] = text
            return True

        if any(phrase in desc for phrase in (
            "message to edit not found",
            "message can't be edited",
            "message_id_invalid",
            "chat not found",
        )):
            _log.warning("[TG] Edit target gone: %s", _redact(d))
            return _EDIT_GONE

        _log.warning("[TG] Market edit error: %s", _redact(d))
        return False
    except Exception as e:
        _log.warning("[TG] Market edit exception: %s", _redact(e))
        return False


def _answer_cb(callback_id, text=""):
    try:
        _http.post(ANSWER_CALLBACK_URL,
                   json={"callback_query_id": callback_id, "text": text},
                   timeout=_tg_http_timeout)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
# PUBLIC SENDERS
# ─────────────────────────────────────────────────────────────────────

def send_bot(message, parse_mode="HTML"):
    if TRADE_QUIET_MODE:
        _log.debug("[TG QUIET] suppressed bot message")
        return
    _tg_enqueue(_send, BOT_CHAT_ID, message, parse_mode=parse_mode)


def send_bot_force(message, parse_mode="HTML"):
    _tg_enqueue(_send, BOT_CHAT_ID, message, parse_mode=parse_mode)


def send_trade_channel(message):
    if TRADE_QUIET_MODE:
        _log.debug("[TG QUIET] suppressed channel message")
        return
    _tg_enqueue(_send, CHANNEL_ID, message, parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────
# DUAL DASHBOARD  — two persistent edit-in-place messages
# ─────────────────────────────────────────────────────────────────────

def _do_send_or_edit_engine(text: str):
    global _engine_msg_id
    if _engine_msg_id is None:
        result = _send(BOT_CHAT_ID, text)
        if result:
            _engine_msg_id = result["message_id"]
            _save_state()
    else:
        ok = _edit(_engine_msg_id, text)
        if ok == _EDIT_GONE:
            old_id = _engine_msg_id
            _engine_msg_id = None
            _last_edited.pop(old_id, None)
            _save_state()
            result = _send(BOT_CHAT_ID, text)
            if result:
                _engine_msg_id = result["message_id"]
                _save_state()


def _do_send_or_edit_market(text: str, reply_markup=None):
    global _market_msg_id
    if _market_msg_id is None:
        result = _send(BOT_CHAT_ID, text, reply_markup=reply_markup)
        if result:
            _market_msg_id = result["message_id"]
            _save_state()
    else:
        ok = _edit_with_markup(_market_msg_id, text, reply_markup)
        if ok == _EDIT_GONE:
            old_id = _market_msg_id
            _market_msg_id = None
            _last_edited.pop(old_id, None)
            _save_state()
            result = _send(BOT_CHAT_ID, text, reply_markup=reply_markup)
            if result:
                _market_msg_id = result["message_id"]
                _save_state()


def send_or_edit_engine_dashboard(text: str):
    _tg_enqueue(_do_send_or_edit_engine, text)


def send_or_edit_market_dashboard(text: str, reply_markup=None):
    _tg_enqueue(_do_send_or_edit_market, text, reply_markup)


def send_or_edit_dashboard(text: str, parse_mode: str = "HTML"):
    send_or_edit_engine_dashboard(text)


def reset_dashboard():
    global _engine_msg_id, _market_msg_id
    _engine_msg_id = None
    _market_msg_id = None
    _last_edited.clear()
    _save_state()


def repost_engine_dashboard(text: str):
    _tg_enqueue(_do_repost_engine, text)


def _do_repost_engine(text: str):
    global _engine_msg_id
    if _engine_msg_id:
        try:
            _http.post(f"{API_URL}/deleteMessage", json={
                "chat_id": BOT_CHAT_ID,
                "message_id": _engine_msg_id,
            }, timeout=_tg_http_timeout)
        except Exception:
            pass
        _last_edited.pop(_engine_msg_id, None)
        _engine_msg_id = None
    result = _send(BOT_CHAT_ID, text)
    if result:
        _engine_msg_id = result["message_id"]
        _save_state()


# ─────────────────────────────────────────────────────────────────────
# TRADE ENTRY  — EXIT button attached; live-edited while trade is open
# ─────────────────────────────────────────────────────────────────────

def send_trade_entry_with_exit_button(message):
    global _trade_msg_id
    kb = {"inline_keyboard": [[{"text": "🔴 EXIT NOW", "callback_data": "manual_exit"}]]}
    result = _send(BOT_CHAT_ID, message, reply_markup=kb)
    send_trade_channel(message)
    if result:
        _trade_msg_id = result["message_id"]
        _last_edited.pop(_trade_msg_id, None)


def delete_trade_message():
    global _trade_msg_id
    if not _trade_msg_id:
        return
    mid = _trade_msg_id
    _trade_msg_id = None
    _last_edited.pop(mid, None)
    _save_state()
    try:
        _http.post(f"{API_URL}/deleteMessage", json={
            "chat_id": BOT_CHAT_ID, "message_id": mid,
        }, timeout=_tg_http_timeout)
    except Exception:
        pass


def freeze_trade_message(exit_text: str):
    global _trade_msg_id
    if not _trade_msg_id:
        return
    mid = _trade_msg_id
    _trade_msg_id = None
    _last_edited.pop(mid, None)
    _save_state()
    _tg_enqueue(_do_freeze_trade, mid, exit_text)


def _do_freeze_trade(mid: int, exit_text: str):
    _edit_with_markup(mid, exit_text, reply_markup=None)


def _trade_exit_markup():
    return {"inline_keyboard": [[{"text": "EXIT NOW", "callback_data": "manual_exit"}]]}


def _do_send_trade_entry_with_exit_button(message):
    global _trade_msg_id
    result = _send(BOT_CHAT_ID, message, reply_markup=_trade_exit_markup())
    _send(CHANNEL_ID, message, parse_mode="HTML")
    if result:
        _trade_msg_id = result["message_id"]
        _last_edited.pop(_trade_msg_id, None)
        _save_state()


def send_trade_entry_with_exit_button(message):
    _tg_enqueue(_do_send_trade_entry_with_exit_button, message)


def update_trade_live(message: str):
    global _pending_trade_live, _trade_live_queued
    with _live_update_lock:
        _pending_trade_live = message
        if _trade_live_queued:
            return
        _trade_live_queued = True
    if not _tg_enqueue(_do_update_trade_live):
        with _live_update_lock:
            _trade_live_queued = False


def _do_update_trade_live():
    global _trade_msg_id, _pending_trade_live, _trade_live_queued
    with _live_update_lock:
        message = _pending_trade_live
        _pending_trade_live = None
        _trade_live_queued = False
    if not message:
        return

    if _trade_msg_id:
        expected_msg_id = _trade_msg_id
        result = _edit_with_markup(expected_msg_id, message, _trade_exit_markup())
        if result == _EDIT_GONE and _trade_msg_id == expected_msg_id:
            _trade_msg_id = None
            _last_edited.pop(expected_msg_id, None)
            _save_state()
        elif result:
            return
        else:
            return

    result = _send(BOT_CHAT_ID, message, reply_markup=_trade_exit_markup())
    if result:
        _trade_msg_id = result["message_id"]
        _last_edited.pop(_trade_msg_id, None)
        _save_state()


def remove_exit_button():
    delete_trade_message()


# ─────────────────────────────────────────────────────────────────────
# SCALP TRADE MESSAGES
# ─────────────────────────────────────────────────────────────────────

def _do_send_scalp_entry(message: str):
    global _scalp_msg_id
    result = _send(BOT_CHAT_ID, message)
    if result:
        _scalp_msg_id = result["message_id"]
        _last_edited.pop(_scalp_msg_id, None)
        _save_state()


def send_scalp_entry(message: str):
    _tg_enqueue(_do_send_scalp_entry, message)


def update_scalp_live(message: str):
    global _pending_scalp_live, _scalp_live_queued
    with _live_update_lock:
        _pending_scalp_live = message
        if _scalp_live_queued:
            return
        _scalp_live_queued = True
    if not _tg_enqueue(_do_update_scalp_live):
        with _live_update_lock:
            _scalp_live_queued = False


def _do_update_scalp_live():
    global _scalp_msg_id, _pending_scalp_live, _scalp_live_queued
    with _live_update_lock:
        message = _pending_scalp_live
        _pending_scalp_live = None
        _scalp_live_queued = False
    if not message:
        return

    if _scalp_msg_id:
        expected_msg_id = _scalp_msg_id
        result = _edit(expected_msg_id, message)
        if result == _EDIT_GONE and _scalp_msg_id == expected_msg_id:
            _scalp_msg_id = None
            _last_edited.pop(expected_msg_id, None)
            _save_state()
        elif result:
            return
        else:
            return

    result = _send(BOT_CHAT_ID, message)
    if result:
        _scalp_msg_id = result["message_id"]
        _last_edited.pop(_scalp_msg_id, None)
        _save_state()


def delete_scalp_message():
    global _scalp_msg_id
    if not _scalp_msg_id:
        return
    mid = _scalp_msg_id
    _scalp_msg_id = None
    _last_edited.pop(mid, None)
    _save_state()
    try:
        _http.post(f"{API_URL}/deleteMessage", json={
            "chat_id": BOT_CHAT_ID, "message_id": mid,
        }, timeout=_tg_http_timeout)
    except Exception:
        pass


def freeze_scalp_message(exit_text: str):
    global _scalp_msg_id
    if not _scalp_msg_id:
        return
    mid = _scalp_msg_id
    _scalp_msg_id = None
    _last_edited.pop(mid, None)
    _save_state()
    _tg_enqueue(_edit, mid, exit_text)


# ─────────────────────────────────────────────────────────────────────
# TRADE CONFIRMATION  — YES / SKIP with 3s auto-execute timeout
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
        f"<i>Auto-execute in 3s</i>"
    )
    kb = {"inline_keyboard": [[
        {"text": "YES — Execute", "callback_data": f"trade_yes_{uid}"},
        {"text": "SKIP",          "callback_data": f"trade_skip_{uid}"},
    ]]}
    _send(BOT_CHAT_ID, msg, reply_markup=kb)

    deadline = time.time() + 3
    while time.time() < deadline:
        if _pending_confirm_resp is not None:
            break
        time.sleep(0.3)

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


def _poll_commands_internal(status_cb=None):
    global _last_update_id, MANUAL_EXIT_REQUESTED, ENGINE_PAUSED
    global ENGINE_STOP_REQUESTED, CE_THRESHOLD_OVERRIDE, PE_THRESHOLD_OVERRIDE
    global _pending_confirm_resp, _poll_fail_count

    try:
        params = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
        if _last_update_id:
            params["offset"] = _last_update_id + 1

        r = _http.get(GET_UPDATES_URL, params=params, timeout=_tg_poll_timeout)
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
                    cb   = status_cb or _status_callback
                    info = cb() if cb else "Status unavailable."
                    _send(BOT_CHAT_ID, info)

                elif cmd == "/help":
                    send_bot(_HELP_TEXT)

                else:
                    send_bot(f"Unknown: {cmd}\n" + _HELP_TEXT)

    except Exception as e:
        _poll_fail_count += 1
        if _poll_fail_count == 1 or _poll_fail_count % 10 == 0:
            _log.warning("[TG] poll_commands error (#%d): %s", _poll_fail_count, _redact(e))
    else:
        _poll_fail_count = 0


_status_callback = None


def poll_commands(status_cb=None):
    global _status_callback
    _ensure_thread()
    if status_cb is not None:
        _status_callback = status_cb


def poll_manual_exit(status_cb=None):
    poll_commands(status_cb=status_cb)


def send_eod_summary(summary: dict):
    from datetime import date
    t      = summary.get("trades", 0)
    pnl    = summary.get("pnl", 0)
    wins   = summary.get("wins", 0)
    losses = summary.get("losses", 0)
    wr     = summary.get("win_rate", 0)
    avg_w  = summary.get("avg_win", 0)
    avg_l  = summary.get("avg_loss", 0)
    best   = summary.get("best", 0)
    worst  = summary.get("worst", 0)

    pnl_str = f"+{pnl:,.0f}" if pnl >= 0 else f"{pnl:,.0f}"
    status  = "PROFIT DAY" if pnl > 0 else ("BREAK EVEN" if pnl == 0 else "LOSS DAY")

    msg = (
        f"<b>END OF DAY — {date.today().strftime('%d %b %Y')}</b>\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"Status    : {status}\n"
        f"Total P&amp;L : <b>₹{pnl_str}</b>\n"
        f"\n"
        f"Trades    : {t}  ({wins}W / {losses}L)\n"
        f"Win Rate  : {wr:.0f}%\n"
        f"Avg Win   : +₹{avg_w:,.0f}\n"
        f"Avg Loss  : ₹{avg_l:,.0f}\n"
        f"Best      : +₹{best:,.0f}\n"
        f"Worst     : ₹{worst:,.0f}\n"
        f"<code>━━━━━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"<i>DRY RUN — no real money</i>"
    )
    # Send synchronously with retries — do NOT use the queue here.
    # The process shuts down immediately after EOD, killing the daemon
    # thread before it can process queued items.
    for attempt in range(3):
        result = _send(BOT_CHAT_ID, msg)
        if result:
            break
        _log.warning("[TG] EOD summary send failed (attempt %d/3)", attempt + 1)
        time.sleep(2)
    for attempt in range(3):
        result = _send(CHANNEL_ID, msg)
        if result:
            break
        time.sleep(2)
