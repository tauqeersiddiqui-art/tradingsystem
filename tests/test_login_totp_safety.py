# tests/test_login_totp_safety.py
#
# Guards the TOTP safety rails added after the 2026-09-06 failure, which ended
# with "Invalid TOTP. 3 attempt(s) remain before the account is locked."
#
# NOTHING HERE TOUCHES THE NETWORK. No browser is launched, no code is ever
# submitted, and no login attempt can be spent by running this file. The two
# defects under test are:
#   1) a code was generated with 2s left in its window, then re-generated at
#      typing time, so the code verified was not the code submitted;
#   2) a mismatch between intended and actual field value was logged and then
#      submitted anyway, burning one of ~4 attempts before lockout.

import importlib
import sys

import pytest

pytest.importorskip("pyotp", reason="pyotp not installed")
pytest.importorskip("dotenv", reason="python-dotenv not installed")
pytest.importorskip("playwright", reason="playwright not installed")

import pyotp  # noqa: E402


@pytest.fixture(scope="module")
def lh():
    """Import login_headless without running it."""
    sys.modules.pop("login_headless", None)
    return importlib.import_module("login_headless")


_SECRET = pyotp.random_base32()


# ── The window race ───────────────────────────────────────────────────

def test_fresh_window_waits_out_a_dying_window(lh):
    """A code with 2s left must NOT be returned — that was the 09-06 bug."""
    slept = []
    # 28s into the window => 2 seconds left.
    fake_now = [30 * 1_000_000 + 28]

    def clock():
        return fake_now[0]

    def sleeper(secs):
        slept.append(secs)
        fake_now[0] += secs           # advance into the next window

    code, left = lh.totp_with_fresh_window(
        _SECRET, min_window=8, sleeper=sleeper, clock=clock)

    assert slept, "must sleep instead of typing a 2s-left code"
    assert left >= 8, f"returned a code with only {left}s left"
    assert code == pyotp.TOTP(_SECRET).at(fake_now[0])


def test_fresh_window_uses_healthy_window_immediately(lh):
    """With plenty of time left, do not waste 30s waiting."""
    slept = []
    fake_now = 30 * 1_000_000 + 3      # 27s left

    code, left = lh.totp_with_fresh_window(
        _SECRET, min_window=8, sleeper=slept.append, clock=lambda: fake_now)

    assert slept == [], "should not sleep when the window is healthy"
    assert left >= 8
    assert code == pyotp.TOTP(_SECRET).at(fake_now)


def test_returned_code_is_stable_for_its_window(lh):
    """
    The code returned must be the code a submit would use. Regression guard:
    the old path called TOTP.now() a second time and could get a different one.
    """
    fake_now = 30 * 1_000_000 + 1
    c1, _ = lh.totp_with_fresh_window(
        _SECRET, min_window=8, sleeper=lambda s: None, clock=lambda: fake_now)
    c2, _ = lh.totp_with_fresh_window(
        _SECRET, min_window=8, sleeper=lambda s: None, clock=lambda: fake_now + 2)
    assert c1 == c2, "same window must yield the same code"


# ── The attempt guard ─────────────────────────────────────────────────

def test_attempt_guard_allows_exactly_one_submit(lh):
    g = lh._AttemptGuard(limit=1)
    g.claim()                                     # first is allowed
    with pytest.raises(lh.TOTPSafetyAbort) as ei:
        g.claim()                                 # second must be refused
    assert "refusing" in str(ei.value).lower()


def test_attempt_guard_message_explains_lockout_risk(lh):
    """The abort has to tell the operator WHY, or they will just retry."""
    g = lh._AttemptGuard(limit=1)
    g.claim()
    with pytest.raises(lh.TOTPSafetyAbort) as ei:
        g.claim()
    msg = str(ei.value).lower()
    assert "lock" in msg, "must warn about account lockout"
    assert "dry-totp" in msg, "must point at the safe alternative"


def test_module_guard_is_single_attempt(lh):
    assert lh._ATTEMPT_GUARD.limit == 1


# ── Wiring ────────────────────────────────────────────────────────────

def test_dry_totp_defaults_off_but_is_env_switchable(lh, monkeypatch):
    """Safety flags must be explicit; nothing silently enables a submit."""
    monkeypatch.setenv("DRY_TOTP", "true")
    sys.modules.pop("login_headless", None)
    reloaded = importlib.import_module("login_headless")
    assert reloaded.DRY_TOTP is True
    monkeypatch.delenv("DRY_TOTP")
    sys.modules.pop("login_headless", None)
    assert importlib.import_module("login_headless").DRY_TOTP is False


def test_safety_abort_is_not_a_generic_error(lh):
    """main() must distinguish 'declined to submit' from 'login broke'."""
    assert issubclass(lh.TOTPSafetyAbort, RuntimeError)


def test_cli_exposes_dry_and_offline_flags(lh):
    """--dry-totp and --totp-only must exist; they are the documented safe paths."""
    import inspect
    src = inspect.getsource(lh.main)
    assert "--dry-totp" in src
    assert "--totp-only" in src


def test_no_bare_totp_now_at_typing_site(lh):
    """
    Regression guard on the exact defect: the typing site must go through
    totp_with_fresh_window(), not call pyotp.TOTP(...).now() directly.
    """
    import inspect
    src = inspect.getsource(lh.login)
    assert "totp_with_fresh_window" in src
    assert "pyotp.TOTP(TOTP_SECRET).now()" not in src, (
        "typing site re-generates the code — reintroduces the window race")
