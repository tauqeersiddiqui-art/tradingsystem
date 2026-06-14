"""
login_headless.py
-----------------
Zerodha login using headless Chromium via Playwright.
Works on GitHub Actions (Ubuntu) — no Edge/Selenium needed.
Playwright runs real JavaScript so Zerodha's fingerprint/session
tokens are set exactly as they are in a real browser.

Install:  pip install playwright && playwright install chromium --with-deps
"""

import os
import sys
import logging
import urllib.parse

import pyotp
from dotenv import load_dotenv
from kiteconnect import KiteConnect
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

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
    """Run headless Chromium, log in to Zerodha, return access_token."""
    log.info("=== login_headless v6 (Playwright / headless Chromium) ===")

    request_token = None
    connect_url   = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context(
            # Appear as a real desktop Chrome on Linux
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        # ── Intercept the final redirect to capture request_token ──────
        # After login + TOTP, Zerodha redirects to your redirect_url
        # (often http://127.0.0.1) with request_token in the query string.
        # We grab it from the route and abort the navigation so Playwright
        # doesn't try to load an unreachable localhost URL.
        def _intercept(route):
            nonlocal request_token
            url = route.request.url
            if "request_token=" in url:
                parsed = urllib.parse.urlparse(url)
                params = urllib.parse.parse_qs(parsed.query)
                rt = params.get("request_token", [None])[0]
                if rt:
                    request_token = rt
                    log.info(f"request_token captured via route: {rt[:8]}...")
                route.abort()   # don't actually connect to localhost
            else:
                route.continue_()

        page.route("**/*", _intercept)

        # ── Step 1: Open login page ────────────────────────────────────
        log.info(f"Step 1: opening KiteConnect login page ...")
        page.goto(connect_url, wait_until="domcontentloaded", timeout=60_000)
        log.info(f"Page title: {page.title()}")

        # ── Step 2: Enter user ID ──────────────────────────────────────
        log.info("Step 2: entering user ID ...")
        page.wait_for_selector("input[type='text']", timeout=15_000)
        page.fill("input[type='text']", USER_ID)
        page.press("input[type='text']", "Enter")

        # ── Step 3: Enter password ─────────────────────────────────────
        log.info("Step 3: entering password ...")
        page.wait_for_selector("input[type='password']", timeout=10_000)
        page.fill("input[type='password']", PASSWORD)
        page.press("input[type='password']", "Enter")

        # ── Step 4: Enter TOTP ─────────────────────────────────────────
        # Wait for the second "password"-type input (the TOTP / app_code field)
        log.info("Step 4: waiting for TOTP field ...")
        try:
            page.wait_for_selector("input[type='password']", timeout=10_000)
            totp_code = pyotp.TOTP(TOTP_SECRET).now()
            log.info(f"Entering TOTP: {totp_code}")
            page.fill("input[type='password']", totp_code)
            page.press("input[type='password']", "Enter")
        except PWTimeout:
            log.info("TOTP field not found — may have been skipped by Zerodha")

        # ── Step 5: Wait for redirect with request_token ───────────────
        log.info("Step 5: waiting for redirect ...")
        try:
            # Wait until the route interceptor fires (request_token captured)
            page.wait_for_function(
                "() => window.location.href.includes('request_token')",
                timeout=30_000,
            )
        except PWTimeout:
            # Route interception already captured it, or it's in the current URL
            pass

        # Fallback: read from current page URL
        if not request_token:
            cur = page.url
            if "request_token=" in cur:
                parsed = urllib.parse.urlparse(cur)
                params = urllib.parse.parse_qs(parsed.query)
                request_token = params.get("request_token", [None])[0]
                log.info(f"request_token from URL: {request_token[:8] if request_token else 'none'}...")

        browser.close()

    if not request_token:
        raise RuntimeError(
            "request_token not captured — login may have failed. "
            "Check screenshot or page state."
        )

    # ── Step 6: Exchange for access_token ─────────────────────────────
    log.info("Step 6: generating access_token ...")
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
