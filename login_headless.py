"""
login_headless.py
-----------------
Zerodha login using headless Chromium via Playwright.
Works on GitHub Actions (Ubuntu) — no Edge/Selenium needed.

Install:  pip install playwright && playwright install chromium --with-deps
"""

import os
import sys
import time
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


def _extract_token(url: str):
    """Return request_token from URL query string, or None."""
    try:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        return params.get("request_token", [None])[0]
    except Exception:
        return None


def login() -> str:
    log.info("=== login_headless v9 (Playwright / headless Chromium) ===")

    request_token = None
    connect_url   = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    screenshot    = os.path.join(BASE_DIR, "logs", "login_state.png")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        # ── Observe every outgoing request — no interception/blocking ──
        # page.on("request") fires the instant the browser initiates a
        # request, BEFORE the TCP connection — so it catches even the
        # navigation to http://127.0.0.1 (connection-refused) that carries
        # the request_token.  page.route("**/*") misses bare-host URLs
        # like http://127.0.0.1 that have no path, so we avoid it here.
        def _on_request(req):
            nonlocal request_token
            if request_token:
                return
            url = req.url
            if "request_token=" in url:
                rt = _extract_token(url)
                if rt:
                    request_token = rt
                    log.info(f"request_token captured from request: {rt[:8]}...")

        def _on_navigate(frame):
            nonlocal request_token
            if request_token or frame != page.main_frame:
                return
            url = frame.url
            if "request_token=" in url:
                rt = _extract_token(url)
                if rt:
                    request_token = rt
                    log.info(f"request_token captured from navigation: {rt[:8]}...")

        page.on("request",        _on_request)
        page.on("framenavigated", _on_navigate)

        # ── Step 1: Open KiteConnect login page ────────────────────────
        log.info("Step 1: opening login page ...")
        try:
            page.goto(connect_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            log.warning(f"goto raised (may be redirect): {e}")
        log.info(f"Page: {page.title()!r}  URL: {page.url[:80]}")

        # ── Step 2: User ID ────────────────────────────────────────────
        log.info("Step 2: entering user ID ...")
        page.wait_for_selector("input[type='text']", timeout=15_000)
        page.fill("input[type='text']", USER_ID)
        page.wait_for_timeout(300)
        page.keyboard.press("Enter")   # keyboard.press = no selector, no timeout

        # ── Step 3: Password ───────────────────────────────────────────
        log.info("Step 3: entering password ...")
        page.wait_for_selector("input[type='password']", timeout=10_000)
        page.wait_for_timeout(300)
        page.fill("input[type='password']", PASSWORD)
        page.wait_for_timeout(300)
        page.keyboard.press("Enter")   # keyboard.press = no selector, no timeout

        # ── Step 4: 2FA / TOTP / app_code ─────────────────────────────
        log.info("Step 4: waiting for 2FA page ...")

        # Wait for the credentials form to leave the DOM before we search
        # for the TOTP input — prevents accidentally filling the password
        # field that's still on screen during the React page transition.
        try:
            page.wait_for_selector(
                "input[type='password']", state="detached", timeout=10_000
            )
            log.info("Credentials page cleared (password field detached)")
        except PWTimeout:
            log.warning("Password field still in DOM after 10s — proceeding anyway")

        page.wait_for_timeout(1_500)   # let the 2FA component fully render
        page.screenshot(path=os.path.join(BASE_DIR, "logs", "after_password.png"))
        log.info(f"2FA page URL: {page.url[:100]}")

        # Log every input on the page so we know exactly what type to use
        try:
            inputs_info = page.eval_on_selector_all(
                "input",
                "els => els.map(e => ({t: e.type, n: e.name, p: e.placeholder, v: !!e.offsetParent}))"
            )
            for i in inputs_info:
                log.info(f"  input: type={i['t']!r} name={i['n']!r} placeholder={i['p']!r} visible={i['v']}")
        except Exception as e:
            log.debug(f"input scan error: {e}")

        # Try each selector in order — confirmed from Zerodha DevTools:
        # the Mobile App Code input is type='number' (not password/text).
        _TOTP_SELECTORS = [
            "input[type='number']",            # ← confirmed from Zerodha DevTools
            "input[type='password']",           # fallback (older Zerodha versions)
            "input[type='text']",
            "input[type='tel']",
            "input[autocomplete='one-time-code']",
            "input:not([type='hidden'])",
        ]
        _filled = False
        for sel in _TOTP_SELECTORS:
            try:
                page.wait_for_selector(sel, state="visible", timeout=4_000)

                totp_code = pyotp.TOTP(TOTP_SECRET).now()
                log.info(f"Typing TOTP {totp_code} digit-by-digit into {sel!r}")

                # Zerodha auto-submits the moment the 6th digit is entered —
                # NO Enter / Continue button needed.
                # Use press_sequentially() so each digit fires the full
                # keydown→keypress→input→keyup event chain that React expects.
                loc = page.locator(sel).first
                loc.click()
                page.wait_for_timeout(200)
                loc.press_sequentially(totp_code, delay=120)

                # Read back to confirm digits landed
                actual = loc.input_value()
                log.info(f"Input value after typing: {actual!r} (expected {totp_code!r})")
                page.screenshot(path=os.path.join(BASE_DIR, "logs", "after_totp_fill.png"))

                log.info("6 digits typed — auto-submit + redirect expected ...")
                _filled = True
                break
            except PWTimeout:
                log.info(f"Selector not found: {sel!r}")
        if not _filled:
            log.warning("No TOTP input found — Zerodha may have skipped 2FA")

        # ── Step 5: Wait for redirect carrying request_token ───────────
        log.info("Step 5: waiting up to 45s for redirect ...")
        deadline = time.time() + 45
        while time.time() < deadline:
            if request_token:
                break
            try:
                page.wait_for_timeout(1_000)
            except Exception:
                break

        # Capture page state before closing (helps diagnose failures)
        log.info(f"Final URL: {page.url[:120]}")
        try:
            page.screenshot(path=screenshot, full_page=True)
            log.info(f"Screenshot saved: {screenshot}")
        except Exception:
            pass

        browser.close()

    if not request_token:
        raise RuntimeError(
            "request_token not captured after 45 s. "
            f"See screenshot: {screenshot}"
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
