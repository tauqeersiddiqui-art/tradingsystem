"""
login_headless.py
-----------------
Zerodha login WITHOUT Selenium — pure HTTP via requests + pyotp.
Works on GitHub Actions (no browser required).
The 6-digit TOTP is auto-generated from KITE_TOTP_SECRET in .env.
"""

import os
import sys
import time
import logging
import urllib.parse

import pyotp
import requests
from dotenv import load_dotenv
from kiteconnect import KiteConnect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [login] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("login")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

load_dotenv(ENV_FILE, override=True)

API_KEY     = os.getenv("KITE_API_KEY")
API_SECRET  = os.getenv("KITE_API_SECRET")
USER_ID     = os.getenv("KITE_USER_ID")
PASSWORD    = os.getenv("KITE_PASSWORD")
TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

for name, val in {
    "KITE_API_KEY":     API_KEY,
    "KITE_API_SECRET":  API_SECRET,
    "KITE_USER_ID":     USER_ID,
    "KITE_PASSWORD":    PASSWORD,
    "KITE_TOTP_SECRET": TOTP_SECRET,
}.items():
    if not val:
        log.critical(f"{name} missing in .env")
        sys.exit(1)

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "X-Kite-Version": "3",
    "Origin":  "https://kite.zerodha.com",
    "Referer": "https://kite.zerodha.com/",
}


def _update_env_token(token: str) -> None:
    lines = []
    found = False
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, "r") as f:
            for line in f:
                if line.startswith("KITE_ACCESS_TOKEN="):
                    lines.append(f"KITE_ACCESS_TOKEN={token}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"\nKITE_ACCESS_TOKEN={token}\n")
    with open(ENV_FILE, "w") as f:
        f.writelines(lines)


def _post_twofa(session, request_id: str, twofa_type: str) -> requests.Response:
    """Generate a fresh TOTP and POST to twofa endpoint."""
    totp_code = pyotp.TOTP(TOTP_SECRET).now()
    log.info(f"Submitting TOTP {totp_code} (twofa_type={twofa_type}) ...")
    r = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id":     USER_ID,
            "request_id":  request_id,
            "twofa_value": totp_code,
            "twofa_type":  twofa_type,
        },
        timeout=30,
    )
    # Always print the response body — critical for debugging 400 errors
    log.info(f"twofa response: HTTP {r.status_code}  body={r.text[:400]}")
    return r


def login() -> str:
    """Perform headless Zerodha login. Returns the access_token string."""

    log.info("=== login_headless v5 ===")

    session = requests.Session()
    session.headers.update(_BASE_HEADERS)

    # ── Step 0: Seed session cookies ───────────────────────────────────
    # Load the KiteConnect login page — same URL the browser opens first.
    # Sets kf_session / kf_version cookies that twofa endpoint validates.
    log.info("Step 0: seeding session cookies from KiteConnect login page ...")
    connect_url = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
    r0 = session.get(connect_url, timeout=30)
    log.info(f"Step 0: HTTP {r0.status_code} | cookies={list(session.cookies.keys())}")

    # ── Step 1: Password login ─────────────────────────────────────────
    log.info("Step 1: submitting credentials ...")
    r1 = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": USER_ID, "password": PASSWORD},
        timeout=30,
    )
    log.info(f"Step 1: HTTP {r1.status_code}  body={r1.text[:300]}")
    if not r1.ok or r1.json().get("status") != "success":
        raise RuntimeError(f"Login API failed: {r1.status_code} {r1.text[:300]}")

    j1         = r1.json()
    request_id = j1["data"]["request_id"]
    twofa_type = j1["data"].get("twofa_type", "totp")
    log.info(f"Credentials accepted — request_id={request_id[:16]}... twofa_type={twofa_type}")

    # ── Step 2: TOTP — retry once if near a 30s boundary ──────────────
    log.info("Step 2: submitting TOTP ...")
    r2 = _post_twofa(session, request_id, twofa_type)

    if not r2.ok:
        log.warning("TOTP attempt 1 failed — waiting 32s for next TOTP window ...")
        time.sleep(32)

        # Re-login to get a fresh request_id (old one may have expired)
        log.info("Step 2 retry: re-logging in to get fresh request_id ...")
        r1b = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": USER_ID, "password": PASSWORD},
            timeout=30,
        )
        log.info(f"Re-login: HTTP {r1b.status_code}  body={r1b.text[:300]}")
        if r1b.ok and r1b.json().get("status") == "success":
            request_id = r1b.json()["data"]["request_id"]
            twofa_type = r1b.json()["data"].get("twofa_type", "totp")

        r2 = _post_twofa(session, request_id, twofa_type)

    if not r2.ok or r2.json().get("status") != "success":
        raise RuntimeError(f"TOTP failed after retry: {r2.status_code} {r2.text[:300]}")

    log.info("TOTP accepted")

    # ── Step 3: Grab request_token from KiteConnect redirect ───────────
    log.info("Step 3: getting request_token ...")
    request_token = None
    r3 = session.get(connect_url, allow_redirects=False, timeout=30)
    log.info(f"connect/login: HTTP {r3.status_code} Location={r3.headers.get('Location','none')[:80]}")

    for _ in range(8):
        if r3.status_code not in (301, 302, 303, 307, 308):
            break
        location = r3.headers.get("Location", "")
        parsed   = urllib.parse.urlparse(location)
        params   = urllib.parse.parse_qs(parsed.query)
        if "request_token" in params:
            request_token = params["request_token"][0]
            break
        if any(h in location for h in ("127.0.0.1", "localhost")):
            request_token = params.get("request_token", [None])[0]
            break
        r3 = session.get(location, allow_redirects=False, timeout=30)
        log.info(f"  redirect: HTTP {r3.status_code} Location={r3.headers.get('Location','none')[:80]}")

    if not request_token:
        raise RuntimeError(
            f"Could not extract request_token. "
            f"Last: HTTP {r3.status_code} Location={r3.headers.get('Location','none')}"
        )
    log.info(f"request_token: {request_token[:8]}...")

    # ── Step 4: Exchange for access_token ──────────────────────────────
    log.info("Step 4: generating access_token ...")
    kite = KiteConnect(api_key=API_KEY)
    sess = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = sess["access_token"]
    log.info(f"access_token: {access_token[:8]}...")
    return access_token


def main() -> None:
    try:
        token = login()
        _update_env_token(token)
        with open(os.path.join(BASE_DIR, "access_token.txt"), "w") as f:
            f.write(token)
        log.info("Login complete — .env and access_token.txt updated")
    except Exception as exc:
        log.critical(f"Headless login failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
