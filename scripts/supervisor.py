# supervisor.py
# Autonomous session supervisor for the trading engine.
#
# Runs alongside master_runner.py (started independently) and:
#   1. Watches the engine process every N seconds.
#   2. If the engine process dies, auto-restarts it (up to MAX_RESTARTS),
#      with Telegram alerts on every restart.
#   3. Writes a rolling status snapshot (heartbeat + last notable events)
#      to logs/supervisor_status.log so the session can be reviewed later.
#   4. Stops itself after market close (15:40 IST) so it never restarts the
#      engine after hours.
#
# Usage:
#   python scripts/supervisor.py          # poll every 15 s
#   python scripts/supervisor.py 30       # poll every 30 s
#
# Start with:  nohup python -u scripts/supervisor.py > logs/supervisor_console.log 2>&1 &
#
# IMPORTANT: do NOT import telegram.notifier here. send_bot() starts a
# getUpdates long-poll worker thread; a second poller on the same bot token
# causes Telegram 409 conflicts that break the ENGINE's own polling (read
# timeouts, late messages). Send alerts via direct requests instead.

import os
import sys
import time
import subprocess
from datetime import datetime, time as dtime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(BASE, "master_runner.py")
PIDFILE = os.path.join(BASE, "data", ".master_runner.pid")
STATUS_LOG = os.path.join(BASE, "logs", "supervisor_status.log")
ENGINE_LOG = os.path.join(BASE, "logs", "master_runner.log")
MON_LOG = os.path.join(BASE, "logs", "monitor_session.log")

POLL_S = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
MAX_RESTARTS = int(os.getenv("SUPERVISOR_MAX_RESTARTS", "5"))
MARKET_CLOSE = dtime(15, 40)   # stop supervising after this


def _load_env():
    """Load TELEGRAM_BOT_TOKEN / TELEGRAM_BOT_CHAT_ID from .env (best-effort)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BASE, ".env"), override=True)
    except Exception:
        pass


# ── Telegram alert (direct send, NO notifier thread — see module docstring) ──
def tg(msg: str) -> None:
    try:
        _load_env()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat  = os.getenv("TELEGRAM_BOT_CHAT_ID", "").strip()
        if not token or not chat:
            log("[SUPERVISOR] telegram env missing — skipping alert")
            return
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": msg, "parse_mode": "HTML"},
            timeout=8,
        )
    except Exception as e:
        log(f"[SUPERVISOR] Telegram send failed: {e}")


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
    print(line, flush=True)
    with open(STATUS_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def pid_alive(pid: int) -> bool:
    """Windows-safe liveness check.

    NOTE: os.kill(pid, 0) is DANGEROUS on Windows — Python maps non-CTRL
    signals to TerminateProcess, which can kill a healthy process (and
    returned Access-denied here, falsely reporting a live engine as dead,
    which spawned a duplicate engine). Use psutil.pid_exists() instead.
    """
    if pid <= 0:
        return False
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        # Verify it is actually master_runner (guard against reused pids).
        try:
            cmd = " ".join(psutil.Process(pid).cmdline()).lower()
        except Exception:
            return False
        return "master_runner" in cmd
    except ImportError:
        # Fallback: tasklist via subprocess (never os.kill on Windows).
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=10,
            )
            return f"{pid}" in out.stdout and "python" in out.stdout.lower()
        except Exception:
            return False


def read_engine_pid() -> int:
    try:
        return int(open(PIDFILE).read().strip() or "0")
    except (OSError, ValueError):
        return 0


def last_engine_log_line() -> str:
    try:
        size = os.path.getsize(ENGINE_LOG)
        with open(ENGINE_LOG, "rb") as f:
            f.seek(max(0, size - 400))
            return f.read().decode("utf-8", errors="replace").splitlines()[-1].strip()
    except Exception:
        return ""


def engine_log_mtime() -> float:
    try:
        return os.path.getmtime(ENGINE_LOG)
    except OSError:
        return 0.0


def start_engine() -> bool:
    """Start master_runner detached; return True if pid file appears."""
    pidfile = read_engine_pid()
    if pidfile and pid_alive(pidfile):
        log(f"[SUPERVISOR] engine already alive (pid={pidfile}) — no restart needed")
        return True
    if os.path.exists(PIDFILE):  # stale lock from dead process
        try:
            os.remove(PIDFILE)
        except OSError:
            pass
    log("[SUPERVISOR] starting engine...")
    try:
        # PYTHONIOENCODING avoids encoding crashes on Windows console.
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with open(os.path.join(BASE, "logs", "console_run.log"), "a", encoding="utf-8") as out:
            subprocess.Popen(
                [sys.executable, "-u", "master_runner.py"],
                cwd=BASE, env=env, stdout=out, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                close_fds=True,
            )
        return True
    except Exception as e:
        log(f"[SUPERVISOR] start failed: {e}")
        return False


def main():
    log(f"[SUPERVISOR] started | poll={POLL_S}s max_restarts={MAX_RESTARTS} "
        f"engine={ENGINE}")
    restarts = 0
    last_status_ts = 0.0
    last_tg_ts = 0.0

    while True:
        now = datetime.now()
        now_t = now.time()

        if now_t >= MARKET_CLOSE:
            log("[SUPERVISOR] market closed (>=15:40) — stopping supervision")
            break

        pid = read_engine_pid()
        alive = pid_alive(pid) if pid else False
        if not alive:
            log(f"[SUPERVISOR] engine NOT running (pid={pid or 'none'})")
            if restarts >= MAX_RESTARTS:
                log("[SUPERVISOR] max restarts reached — manual intervention required")
                tg("[SUPERVISOR] Engine down + max restarts reached. Manual intervention required.")
                break
            restarts += 1
            tg(f"[SUPERVISOR] Engine stopped — restarting ({restarts}/{MAX_RESTARTS})...")
            if start_engine():
                log(f"[SUPERVISOR] restart {restarts}/{MAX_RESTARTS} launched")
            time.sleep(10)
            continue

        # Heartbeat status every 10 min: engine alive + last log line + monitor events
        if time.time() - last_status_ts >= 600:
            last_status_ts = time.time()
            last_line = last_engine_log_line()
            age_s = time.time() - engine_log_mtime()
            mon_events = 0
            try:
                mon_events = sum(1 for _ in open(MON_LOG, encoding="utf-8", errors="replace"))
            except OSError:
                pass
            log(f"[HEARTBEAT] engine pid={pid} alive | log age={age_s:.0f}s | "
                f"monitor_events={mon_events} | last: {last_line[:120]}")

        # Stale-log watchdog: engine alive but silent >5 min during market hours
        mkt = dtime(9, 15) <= now_t <= dtime(15, 30)
        if mkt and time.time() - engine_log_mtime() > 300 and time.time() - last_tg_ts > 600:
            last_tg_ts = time.time()
            log("[SUPERVISOR] engine alive but log silent >5min — alerting (no restart yet)")
            tg("[SUPERVISOR] Engine alive but silent >5 min. Feed may be down — check.")

        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
