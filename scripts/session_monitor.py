# scripts/session_monitor.py
# Lightweight full-session watcher: tails master_runner.log and records every
# trade / error / watchdog event to logs/session_monitor.log, plus a heartbeat
# summary every 5 minutes. Run detached alongside master_runner.
import time, os

LOG  = "logs/master_runner.log"
OUT  = "logs/session_monitor.log"
PIDF = "logs/session_pid.txt"

INTERESTING = [
    ("SCALP ENTRY",  "TRADE"),
    ("SCALP EXIT",   "TRADE"),
    ("[ENTRY]",      "TRADE"),
    ("[EXIT",        "TRADE"),
    ("SCALP SKIP",   "SKIP"),
    ("DAILY LOSS",   "ALERT"),
    ("Traceback",    "ERR"),
    ("[ENGINE LOOP ERROR]", "ERR"),
    ("Stopping system",     "ALERT"),
    ("Supervisor",   "WATCH"),
    ("WATCHDOG",     "WATCH"),
    ("FEED HEALTH",  "FEED"),
    ("OPTION FEED",  "FEED"),
]
LOUD = {"TRADE", "ALERT", "ERR"}          # always written
QUIET_PERIOD = 300.0                      # heartbeat cadence (s)

def engine_alive() -> bool:
    try:
        pid = open(PIDF).read().strip()
        out = os.popen(f'tasklist /FI "PID eq {pid}" /NH').read()
        return pid in out
    except Exception:
        return False

def last_feed_line(f) -> str:
    return ""  # filled from tail scan below

def main():
    out = open(OUT, "a", encoding="utf-8", errors="ignore")
    f = open(LOG, "r", encoding="utf-8", errors="ignore")
    f.seek(0, 2)
    out.write(f"=== monitor attached {time.strftime('%H:%M:%S')} ===\n")
    out.flush()
    counts = {}
    last_hb = time.time()
    last_feed = ""
    while True:
        line = f.readline()
        if not line:
            if time.time() - last_hb >= QUIET_PERIOD:
                counts_str = " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "quiet"
                out.write(f"[{time.strftime('%H:%M:%S')}] HB alive={engine_alive()} "
                          f"{counts_str} | {last_feed.strip()[-90:]}\n")
                out.flush()
                last_hb = time.time()
            time.sleep(0.5)
            continue
        for pat, tag in INTERESTING:
            if pat in line:
                counts[tag] = counts.get(tag, 0) + 1
                if tag in LOUD:
                    out.write(line if line.endswith("\n") else line + "\n")
                    out.flush()
                if tag == "FEED":
                    last_feed = line
                break
        # non-blocking heartbeat check even in busy periods
        if time.time() - last_hb >= QUIET_PERIOD + 60:
            counts_str = " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "quiet"
            out.write(f"[{time.strftime('%H:%M:%S')}] HB alive={engine_alive()} {counts_str}\n")
            out.flush()
            last_hb = time.time()

if __name__ == "__main__":
    main()
