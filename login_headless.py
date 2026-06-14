"""
login_headless.py
-----------------
Zerodha login WITHOUT Selenium — pure HTTP via requests + pyotp.
Works on GitHub Actions (no browser required).
The 6-digit TOTP is auto-generated from KITE_TOTP_SECRET in .env.

Steps:
  1. POST credentials to Zerodha login API
  2. POST auto-generated TOTP
  3. GET KiteConnect login URL → capture request_token from redirect
  4. Exchange for access_token and save to .env
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

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "X-Kite-Version": "3",
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


def login() -> str:
    """
    Perform headless Zerodha login.
    Returns the access_token string.
    """
    session = requests.Session()
    session.headers.update(_HEADERS)

    # ── Step 1: Password login ─────────────────────────────────────────
    log.info("Step 1: submitting credentials ...")
    r1 = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": USER_ID, "password": PASSWORD},
        timeout=30,
    )
    r1.raise_for_status()
    j1 = r1.json()
    if j1.get("status") != "success":
        raise RuntimeError(f"Login API rejected credentials: {j1}")
    request_id = j1["data"]["request_id"]
    log.info(f"Credentials accepted — request_id={request_id}")

    # ── Step 2: TOTP ───────────────────────────────────────────────────
    totp_code = pyotp.TOTP(TOTP_SECRET).now()
    log.info(f"Step 2: submitting TOTP {totp_code} ...")
    r2 = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={
            "user_id":     USER_ID,
            "request_id":  request_id,
            "twofa_value": totp_code,
            "twofa_type":  "totp",
            "skip_session": "",
        },
        timeout=30,
    )
    r2.raise_for_status()
    j2 = r2.json()
    if j2.get("status") != "success":
        raise RuntimeError(f"TOTP API rejected: {j2}")
    log.info("TOTP accepted")

    # ── Step 3: Grab request_token from KiteConnect redirect ───────────
    # After authentication, this URL immediately redirects (302) to the
    # configured redirect_url with ?request_token=XXX in the query string.
    # We DON'T follow the redirect (allow_redirects=False) because the
    # redirect_url may be http://127.0.0.1 (localhost), which isn't
    # reachable on GitHub Actions — we only need the URL, not the page.
    connect_url = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
    log.info("Step 3: getting request_token ...")

    request_token = None
    r3 = session.get(connect_url, allow_redirects=False, timeout=30)

    for _ in range(8):   # follow at most 8 intermediate redirects
        if r3.status_code not in (301, 302, 303, 307, 308):
            break
        location = r3.headers.get("Location", "")
        parsed   = urllib.parse.urlparse(location)
        params   = urllib.parse.parse_qs(parsed.query)
        if "request_token" in params:
            request_token = params["request_token"][0]
            break
        # Only follow if not going to localhost / unreachable host
        if any(h in location for h in ("127.0.0.1", "localhost")):
            break
        r3 = session.get(location, allow_redirects=False, timeout=30)

    if not request_token:
        raise RuntimeError(
            f"Could not extract request_token. "
            f"Last redirect: {r3.headers.get('Location', 'none')}"
        )
    log.info(f"request_token obtained: {request_token[:8]}...")

    # ── Step 4: Exchange for access_token ──────────────────────────────
    log.info("Step 4: generating access_token ...")
    kite = KiteConnect(api_key=API_KEY)
    sess = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = sess["access_token"]
    log.info(f"access_token obtained: {access_token[:8]}...")

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
