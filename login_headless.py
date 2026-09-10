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
import hashlib
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


def _totp_diagnostic() -> None:
    """
    Print enough info to confirm the correct secret is loaded
    without exposing the secret itself.

    Check 1 — ENV file path:
        Tells you WHICH .env was loaded.  If login_headless.py is run
        from a different directory than login.py, this will differ.

    Check 2 — Secret fingerprint:
        SHA-256[:12] is a stable identifier.  Run the same check in
        login.py (or the Python REPL) and compare — if they differ,
        different secrets are loaded.

    Check 3 — Code right now:
        Compare with your Zerodha mobile app at this exact moment.
        If they match → secret is correct.
        If they differ → wrong secret in this .env / GitHub Secret.
    """
    secret = TOTP_SECRET or ""
    preview = (secret[:4] + "…" + secret[-2:]) if len(secret) >= 6 else "TOO_SHORT"
    fp = hashlib.sha256(secret.encode()).hexdigest()[:12]
    totp = pyotp.TOTP(secret)
    window_secs = 30 - int(time.time()) % 30

    log.info("─── TOTP Diagnostic ──────────────────────────────────────")
    log.info(f"  ENV file       : {ENV_FILE}")
    log.info(f"  Secret length  : {len(secret)} chars  preview={preview!r}")
    log.info(f"  Secret SHA-256 : {fp}  (compare with login.py run)")
    log.info(f"  Code right now : {totp.now()}  ({window_secs}s left in window)")
    log.info(f"  Next code      : {totp.at(int(time.time()) + window_secs + 1)}")
    log.info("  → If 'Code right now' ≠ Zerodha app → wrong secret here")
    log.info("  → If SHA-256 differs from login.py  → different .env loaded")
    log.info("──────────────────────────────────────────────────────────")


# ── Safety rails around TOTP submission ───────────────────────────────
#
# WHY THESE EXIST
#   A run on 2026-09-06 failed with "Invalid TOTP. 3 attempt(s) remain before
#   the account is locked." Two defects in the old path caused it:
#     1) RACE: _totp_diagnostic() printed a code with 2s left in its window,
#        then the typing step called TOTP.now() again and got the NEXT code —
#        so the code typed was not the code verified, and could itself roll
#        over mid-submit.
#     2) NO GUARD: a mismatch between intended and actual field contents was
#        only logged (`JS set didn't land`), then submitted anyway, burning a
#        real attempt against a 4-attempt lockout budget.
#
#   Zerodha locks the account after ~4 consecutive bad TOTPs. Locking it takes
#   out live trading, not just a backfill, so this module now:
#     - refuses more than ONE automated submit per process (_ATTEMPT_GUARD),
#     - waits for a fresh window instead of typing a nearly-dead code,
#     - ABORTS before submit if the field does not contain the intended code,
#     - supports DRY_TOTP / --dry-totp: generate, log, snapshot, pause, and
#       never submit. Use that mode for any verification work.

DRY_TOTP = (os.getenv("DRY_TOTP", "").strip().lower() in ("1", "true", "yes", "on"))
MIN_WINDOW_SECS = int(os.getenv("TOTP_MIN_WINDOW_SECS", "8"))


class TOTPSafetyAbort(RuntimeError):
    """Raised instead of submitting a TOTP we are not confident in."""


class _AttemptGuard:
    """At most one automated TOTP submit per process."""

    def __init__(self, limit: int = 1):
        self.limit = limit
        self.used = 0

    def claim(self) -> None:
        if self.used >= self.limit:
            raise TOTPSafetyAbort(
                f"refusing a {self.used + 1}nd automated TOTP submit in one run "
                f"(limit={self.limit}). Zerodha locks the account after ~4 bad "
                f"codes. Re-run the process for another attempt, or use "
                f"--dry-totp to verify the secret without spending one."
            )
        self.used += 1


_ATTEMPT_GUARD = _AttemptGuard(limit=1)


def totp_with_fresh_window(secret: str, min_window: int = MIN_WINDOW_SECS,
                           sleeper=time.sleep, clock=time.time):
    """
    Return (code, seconds_left) for a code with room left to be submitted.

    Typing a code with 2s left is how the 2026-09-06 run failed. If the current
    window is nearly dead we wait it out and use the next one, so the code that
    gets logged is the same code that gets typed.
    """
    left = 30 - int(clock()) % 30
    if left < min_window:
        log.info(f"TOTP window has only {left}s left (< {min_window}s) — "
                 f"waiting for the next window so the code cannot roll mid-submit")
        sleeper(left + 1)
        left = 30 - int(clock()) % 30
    return pyotp.TOTP(secret).at(int(clock())), left


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
    _totp_diagnostic()

    request_token = None
    connect_url   = f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    screenshot    = os.path.join(BASE_DIR, "logs", "login_state.png")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            slow_mo=300,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
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

                # Use a code with room left in its window (see the safety-rails
                # comment above): the old code called TOTP.now() here after the
                # diagnostic had already printed a DIFFERENT code, so the code
                # verified by the user was never the code submitted.
                totp_code, _win_left = totp_with_fresh_window(TOTP_SECRET)
                log.info(f"Typing TOTP {totp_code} into {sel!r} "
                         f"({_win_left}s left in window)")

                loc = page.locator(sel).first
                loc.click()
                page.wait_for_timeout(200)

                # Zerodha's React number input blocks fill() and press_sequentially.
                # We must set the native value via JS and dispatch both the native
                # 'input' event AND React's synthetic event by triggering the nativeInputValueSetter.
                page.evaluate(
                    """([selector, value]) => {
                        const el = document.querySelector(selector);
                        if (!el) return;
                        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        ).set;
                        nativeInputValueSetter.call(el, value);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    [sel, totp_code]
                )
                page.wait_for_timeout(200)

                actual = loc.input_value()
                log.info(f"Input value after JS set: {actual!r} (expected {totp_code!r})")

                # Final fallback: digit-by-digit
                if actual != totp_code:
                    log.warning("JS set didn't land — retrying with press_sequentially")
                    loc.click(click_count=3)
                    page.wait_for_timeout(100)
                    loc.press_sequentially(totp_code, delay=150)
                    actual = loc.input_value()
                    log.info(f"Input value after press_sequentially: {actual!r}")
                snap = os.path.join(BASE_DIR, "logs", "after_totp_fill.png")
                page.screenshot(path=snap)
                log.info(f"Pre-submit snapshot: {snap}")

                # ── GUARD: never submit a code we did not actually type ──
                # The 2026-09-06 failure logged actual='' and submitted anyway,
                # spending one of four attempts before lockout.
                if actual != totp_code:
                    raise TOTPSafetyAbort(
                        f"field contains {actual!r} but the intended code is "
                        f"{totp_code!r} — NOT submitting. Submitting a wrong "
                        f"code spends one of ~4 attempts before Zerodha locks "
                        f"the account. Inspect {snap} to see the live field."
                    )

                if DRY_TOTP:
                    log.info("=" * 62)
                    log.info("DRY_TOTP: code generated, typed and verified but")
                    log.info("NOT submitted. No login attempt was spent.")
                    log.info(f"  intended code : {totp_code}")
                    log.info(f"  field value   : {actual}")
                    log.info(f"  snapshot      : {snap}")
                    log.info("Compare the code above with your authenticator app.")
                    log.info("  match    -> KITE_TOTP_SECRET is correct; re-run "
                             "without --dry-totp for one real attempt")
                    log.info("  mismatch -> the secret in .env is WRONG; fix it "
                             "before any real attempt")
                    log.info("=" * 62)
                    try:
                        input("Confirm TOTP in authenticator and press Enter to continue...")
                    except EOFError:
                        log.info("(non-interactive stdin — continuing without pause)")
                    raise TOTPSafetyAbort("DRY_TOTP set — aborting before submit by design")

                # This is a real submit: claim the single per-run attempt.
                _ATTEMPT_GUARD.claim()
                log.info("6 digits typed — auto-submit + redirect expected ...")
                _filled = True
                break
            except TOTPSafetyAbort:
                raise
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
    global DRY_TOTP
    import argparse

    ap = argparse.ArgumentParser(
        description="Zerodha headless login (Playwright).",
        epilog="SAFETY: --dry-totp generates and verifies a code WITHOUT "
               "submitting it, so it cannot spend a login attempt. Zerodha "
               "locks the account after ~4 bad codes.",
    )
    ap.add_argument("--dry-totp", action="store_true",
                    help="generate + log + snapshot the TOTP, pause for manual "
                         "verification, and exit WITHOUT submitting")
    ap.add_argument("--totp-only", action="store_true",
                    help="print the TOTP diagnostic locally and exit (no browser, "
                         "no network, cannot affect the account)")
    args = ap.parse_args()

    if args.dry_totp:
        DRY_TOTP = True

    if args.totp_only:
        # Fully offline: local code generation only. Safe to run any number of times.
        _totp_diagnostic()
        code, left = totp_with_fresh_window(TOTP_SECRET or "")
        log.info(f"Fresh-window code: {code}  ({left}s left)")
        log.info("No browser launched, no submit, no attempt spent.")
        log.info("If this code does not match your authenticator app, "
                 "KITE_TOTP_SECRET in .env is wrong.")
        return

    if DRY_TOTP:
        log.info("DRY_TOTP active — will NOT submit a TOTP or spend an attempt.")

    try:
        token = login()
        _update_env_token(token)
        with open(os.path.join(BASE_DIR, "access_token.txt"), "w") as f:
            f.write(token)
        log.info("Login complete — .env and access_token.txt updated")
    except TOTPSafetyAbort as exc:
        # Not a failure: the guard deliberately declined to submit.
        log.warning(f"TOTP submit declined by safety guard: {exc}")
        log.warning("No login attempt was spent.")
        sys.exit(0 if DRY_TOTP else 2)
    except Exception as exc:
        log.critical(f"Headless login failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
