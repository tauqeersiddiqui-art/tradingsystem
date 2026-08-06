# L1_COMPLETION_AND_NEXT_STAGE.md

**Handoff baseline — written end-of-session after the four Level-1 safety fixes.**
**Reader:** an engineer opening this repo tomorrow with no context.
**Status:** L1 hardening COMPLETE. All regression suites green. L2/L3 deferred.

---
---

## 1. EXECUTIVE SUMMARY

The execution layer of this trading system had four **software-induced capital-loss paths** — failures where the bot's own logic, not the market, caused missing trades, wrong-symbol trades, duplicate exposure, or a freshly-opened short after an exit. These date back to the original architecture and were surfaced by an external audit that attempted (and failed) to prove the system's "execution truth" invariants.

**Problems originally found (V1–V4):**
- **V1 — Wrong-symbol recovery adoption:** restart recovery adopted a saved *scalp* position whenever the broker held *any* position, without checking the broker actually held that scalp's symbol → an unbacked local scalp that shorts on exit.
- **V2 — Multiple disagreeing broker reads during recovery:** recovery called `get_positions()`/`has_open_position()` three separate times; a transient failure on the second/third read could flip the outcome (skip adoption → wrong flatten / leave a broker position unmanaged).
- **V3 — `_finalize_entry()` not idempotent:** a late finalizer exception (or crash after SL-persistence) re-ran finalize and placed a **second resting SELL SL-M** → after the real exit, the orphaned first SL fires → **opens a short**; also double trade-count/journal/telegram.
- **V4 — order/position feed skew treated as "immediate":** the code assumed `orders()` and `positions()` advance simultaneously. When the position feed lagged the order feed, a completed fill was wrongly concluded "already closed", the entry guard was released, and a second BUY produced double exposure.

**Root causes:** cross-endpoint trust (order book vs position book), multi-snapshot recovery reads, non-idempotent finalize, and a single flat-read that was misinterpreted as "fill gone."

**Safety improvements completed (all four L1 items):**
- **L1-3** classify-don't-guess: a terminal-but-flat read is *retained for confirmation*, never treated as "closed" (kills guard release).
- **L1-1** recovery uses exactly **one** broker snapshot (single `get_positions()`); no state decision depends on a fresh query.
- **L1-2** every adoption (main AND scalp) is validated **by symbol** against that one snapshot.
- **L1-4** finalize is idempotent: broker SL is deduplicated (find-before-place), local side effects guarded by a per-order marker.

**Overall execution engine status:** the four loss paths are closed and each is permanently locked by regression tests (`phase21` = 95 checks). **No verified software-induced loss path remains that I can reproduce from the code.** Residual items (Section 4) are documented, verified risks — not new inventions — and are fixable at L2 without an architectural rewrite. Deployability verdict in §11.

---

## 2. COMPLETED LEVEL-1 SAFETY FIXES

### 2.1 L1-3 — "Classify, don't guess" (terminal-but-flat handling)

| Field | Detail |
|---|---|
| **Issue** | A COMPLETED order whose holding had not yet appeared in the position feed was treated as "fill already closed" → pending dropped, entry guard released → a second BUY produced double exposure (V4). |
| **Root cause** | Assumed `orders()` and `positions()` are immediately consistent and read "flat" on ONE poll as proof the fill is gone. |
| **Files modified** | `master_runner.py` (5 sites: `_reconcile_pending_entry` COMPLETE-flat + partial-flat, `_pending_scalp` COMPLETE-flat, recovery-restore main + scalp COMPLETE-flat); `data/_phase21_verify.py`. |
| **Invariant fixed** | I1 (no unconfirmed creation) / I2 (no double exposure) / V4. |
| **Regression tests** | B2, B9, B12 (feed skew: flat→holds→position created exactly once), B13 (never released past window), A1–A3. |
| **Adversarial review** | Found fail-closed deadlock (genuine closed-fill held forever, no operator escape) — flagged as L2 item, not an invariant break. |
| **External audit** | No new invariant violated. V1/V2/V3 correctly left open for later L1 items. |
| **Status** | ✅ APPROVED |

### 2.2 L1-1 — Single broker snapshot in recovery

| Field | Detail |
|---|---|
| **Issue** | Recovery read broker positions up to 3× (`has_open_position()` + `get_positions()` + Case-B flatten `get_positions()`). A transient failure of a later read flipped the outcome: saved position skipped → flattened or left unmanaged (V2). |
| **Root cause** | Separate reads for each state decision; no single correlated snapshot. |
| **Files modified** | `master_runner.py` (recovery top: one `_positions_snap`; Case-B flatten iterates the snapshot); `data/_phase21_verify.py`. |
| **Invariant fixed** | I5 (no partial snapshot), I6 (deterministic recovery), I7 (deterministic) — V2. |
| **Regression tests** | A12–A16 (static: exactly one `get_positions()`, zero `has_open_position()`, all decisions read `_positions_snap`), D1–D5 (equivalence, failed-read→UNKNOWN, determinism, malformed→UNKNOWN). |
| **Adversarial review** | Caught my own regression: malformed parseable response silently skipped recovery → wrapped derivation so it converges to BROKER_UNKNOWN (locked by D5). |
| **External audit** | V2 closed; no invariant regressed. |
| **Status** | ✅ APPROVED |

### 2.3 L1-2 — Symbol-scoped recovery adoption

| Field | Detail |
|---|---|
| **Issue** | Saved scalp adopted whenever *any* broker position was open, without confirming the broker held the scalp's symbol → unbacked local scalp shorts on exit (V1). |
| **Root cause** | Adoption keyed on bare `_broker_open`, not `snapshot.positions[symbol]`. |
| **Files modified** | `master_runner.py` (Case A scalp condition); `data/_phase21_verify.py`. |
| **Invariant fixed** | I4 (no local position without a broker position) — V1. |
| **Regression tests** | A17 (static symbol guard), E1–E5e (decision-matrix: broker holds X/scalp Y → not adopted; matching-only; flat→nothing; unknown→halt; combinations). |
| **Adversarial review** | Normal saved-scalp case unaffected; fall-through safe (Case B / FLAT_DROP / UNKNOWN). Surfaced pre-existing single-adoption `elif` limitation — out of scope, documented in §5. |
| **External audit** | V1 closed; no invariant regressed. |
| **Status** | ✅ APPROVED |

### 2.4 L1-4 — Finalize idempotency

| Field | Detail |
|---|---|
| **Issue** | `BUY COMPLETE → _finalize_entry → SL created → crash/raise → _finalize_entry again → SECOND SL`. Also double trade-count/journal/telegram (V3). |
| **Root cause** | `_finalize_entry` is a non-idempotent, side-effecting transition; no dedup on the broker SL; unwrapped finalizer tail re-fired it. |
| **Files modified** | `master_runner.py` (`_sl_create` find-before-place dedup; `_FINALIZED_ENTRIES` guard keyed by entry `order_id`; tail of finalize wrapped); `data/_phase21_verify.py`. |
| **Invariant fixed** | I4 (no double execution), I7 (idempotent transitions) — V3. |
| **Regression tests** | F1 (exactly one SL), F2 (counter once), F3 (one journal), F4/F4b (one notification, same position), F5 (crash→restart→reuse, no second SL), F6 (saved-position adoption never re-finalizes). |
| **Adversarial review** | Caught & fixed: guard cross-contamination on reused test order-id; SL counter not wired; F6 slice boundary. Residual: `_FINALIZED_ENTRIES` in-memory (crash-before-persist re-runs local side effects once, SL deduped) and dedup reuses first SL without re-checking trigger — documented, better than two SLs. |
| **External audit** | No duplicate broker side effect remains possible — rejection condition not triggered. |
| **Status** | ✅ APPROVED |

---

## 3. CURRENT EXECUTION ENGINE STATUS

**Architecture:** single-threaded `engine_loop` reconciling every cycle; `ExecutionEngine` (executor) is the single broker-facing adapter; local state persisted to a single JSON `runtime_state.json`.

**Broker authority model (now explicit):** the two broker feeds each own one concern, never the other's:
- `orders()` → order lifecycle truth (only it moves an order to COMPLETE/REJECTED/CANCELLED).
- `positions()` → holding truth (only it says "you currently hold N of S").
- Order-ID trackers (`_active_order_id` / `_pending_entry` / `_pending_scalp` / `position` / `scalp_position`) are the *derived* local view. A difference the local view cannot explain → **halt**, never guess.

**Pending order flow:** an entry order placed but not confirmed COMPLETE is retained as a pending (guard `_active_order_id` set, blocks any second BUY). Each cycle it resolves against broker truth: COMPLETE → finalize position; REJECTED/CANCELLED → drop + release; COMPLETE-but-flat → **retained** (L1-3), confirmed on a later snapshot, halted only if flat past the window; UNKNOWN → held.

**Recovery flow (restart / watchdog-restart):** exactly **one** `get_positions()` snapshot (L1-1) → derive `broker_open`/`broker_syms`/flatten-list from it → resolve pending orders against `orders()` → adoption chain keys **every** adopt by `snapshot.positions[symbol]` (L1-2) → resume a single deterministic state. Unknown/malformed/broken read → BROKER_UNKNOWN → pause, no transitions.

**Reconciliation flow:** each cycle reconciles pending entry + pending scalp. Differences are classified by *skew-independent* logic (an unexplained diff → hold or halt), never by timing or a re-fetch.

**Entry flow:** gate (allocator, kill switch, ENGINE_PAUSED) → `execute_entry` → if COMPLETE, `_finalize_entry` (place SL, persist, journal, notify) — all duplicated-on-atomic via SL dedup (`_sl_create` checks the broker and reuses an existing SELL-SL-M before placing) + the finalize guard marker.

**Exit flow:** `execute_exit` must reach COMPLETE to clear a position; unconfirmed exit keeps the position (no ghost close); partial fills shrink vs net, never negative.

**Safety mechanisms:** fail-closed on unknown; single-snapshot recovery; symbol-scoped adoption; retain-don't-drop under feed skew; exactly-one-SL via dedup; in-memory finalize guard; tail-wrapped finalize; persistent per-day kill switch.

---

## 4. REMAINING RISKS (verified only — no invented)

| # | Risk | Severity | Likelihood | Impact | Recommended Fix |
|---|---|---|---|---|---|
|R1 | Genuine closed fill (MIS square-off / manual close) held forever as pending-flat → engine stalls, **no operator `/resolve-pending`** to release it. Availability, not capital loss. | Med | Low–Med | Downtime/missed trades until manual edit of state file | L2 `/resolve-pending` command that re-studies the broker once and adopts/drops/keeps per truth. |
|R2 | SL-dedup in `_sl_create` reuses the first open SELL-SL without re-verifying its trigger matches the intended stop. (Correct for the crash/re-finalize case; a stale prior SL would be reused at its old trigger.) | Med | Low | Wrong protection level / earlier or later stop than intended; still one SL, never two | In the reuse branch of `_sl_create`, if trigger ≠ `stop_loss`, `modify_protective_stop` to the intended trigger before returning. |
|R3 | `_FINALIZED_ENTRIES` is in-memory; a crash between SL-placement and `save_state` re-runs local side effects once on restart (SL reused). This is NOT a duplicate broker artifact, but trades_today/journal re-count once. | Low | Low | Double local count for one trade after a specific crash window | L2 persisted finalized-marker (the "mini-register" — deferred). |
|R4 | Recovery adoption `elif` chain adopts at most one of {main, scalp}; in a pathological both-saved state the lower-priority scalp is unmanaged. Pre-existing, generally unreachable (main/scalp are mutually exclusive by gates). | Low | Very Low | An unmanaged second position in a rare both-saved state | L2 restructure adoption into independent symbol-scoped `if`s (derive both). |
|R5 | Startup `main()` gate at `has_open_position()` is a pre-recovery pre-flight block that can be bypassed via `ALLOW_BROKER_POSITION_ON_START`; it reads positions once outside the single-snapshot L1-1 rule. | Med | Low | A bypassed gate → broker position without local awareness at start | Route the gate through the same single-snapshot recovery (L2), or remove the bypass. |

*(No additional verified risks were identified in this session's codebase. These five are the complete set.)*

---

## 5. DEFERRED ITEMS

#### Level-2 (reliability, additive — no rewrite)
| Item | Why deferred |
|---|---|
| Persisted per-order finalized-marker ("mini-register" in state file) | Safe but touches the persistence contract; L1-4 covers the broker SL artifact efficiently on its own. |
| `/resolve-pending` operator command (release genuine closed-fills) | Command surface, not a current loss path; needs operator UX + input handling. |
| Unified per-instance `state enum` replacing `_active_order_id` / `_pending_*` / `_exit_order_id` | Control-flow refactor — higher regression surface, deliberately avoided during L1. |
| `BROKER_UNKNOWN` on repeated feed timeouts (stale-feed watchdog) | Reliability signal, no current loss path; L1-3 already fail-closes the worst case. |
| R4: independent adoption (both main+scalp in pathological state) | Not a verified current loss; needs adoption restructure. |
| Event-id dedup for analytics/telegram | Cosmetic duplicates; the finalize guard already covers the entry path. |
| SL-dedup trigger re-verify (R2) | Small, additive; was gated by the "don't over-scope" rule — simplest safe next step. |

#### Level-3 (long-term architectural, large rewrite)
| Item | Why deferred |
|---|---|
| Full append-only **Order Register** with atomic rename (derived-truth backbone) | The correct eventual design; a rewrite, explicitly out of L1 scope. |
| Formal per-instrument **state machine** + transition table/forbidden states | Replaces `if/elif` chains; architectural. |
| Per-symbol derived-position engine with register-holding agreement (I6) | Requires the Register. |
| Correlated snapshot service with skew classification | Requires the Register + correlated read. |

---

## 6. REGRESSION TEST INVENTORY

**Runners:** `python data/_phase1_verify.py` (26 ✓) · `python data/_phase2_verify.py` (43 ✓) · `python data/_phase21_verify.py` (95 ✓).
**Discipline:** every `check()` must PASS. Any failure = a proven invariant backdoor.

### Phase 1 — fail-closed broker state (26)
| Test | Purpose | Invariant | Status |
|---|---|---|---|
| `[1] get_positions` | success list / outage raises (never fail-open) | I5 | ✅ |
| `[2] has_open_position` tri-state | flat / open / short / outage (unknown ≠ flat) / malformed raises | I5 | ✅ |
| `[3] verify_flat` | confirmed-flat True / open False / outage raises (halt) | I4 | ✅ |
| `[4] startup gate` | DRY bypass, flat-allow, open-block, UNKNOWN-block (was fail-open), override | I5 | ✅ |
| `[5] recovery fail-closed` | live-flat-not-resumed, live-open-resume, outage → no-resume + PAUSED + unknown flag | I6 | ✅ |

### Phase 2 — execution truth, no fabrication (43)
| Test | Purpose | Invariant | Status |
|---|---|---|---|
| `[1] _normalize_status` | full mapping incl. PARTIAL, never-guess | I9 | ✅ |
| `[2] _order_status` | no fill fabricated: COMPLETE+avg; avg0→UNKNOWN; REJECTED/CANCELLED; OPEN→TIMEOUT fill=None; outage→UNKNOWN; not-found→UNKNOWN; partial→TIMEOUT; partial-exit never negative/full-close/shrink | I10 | ✅ |
| `[3] execute_entry` | COMPLETE→real price; REJECTED→no price; OPEN(timeout)→TIMEOUT no phantom + oid retained; outage→UNKNOWN; placement-fail→REJECTED | I1 | ✅ |
| `[4] execute_exit` | COMPLETE→clear; REJECTED/TIMEOUT→no clear; placement-fail→keep | I2 | ✅ |
| `[5] position create/remove gating` | COMPLETE→CREATE; TIMEOUT→PENDING; REJECTED→abandon; exit-PENDING keeps; placement-fail keeps | I1/I2 | ✅ |
| `[6] duplicate-order guard` | active order → second entry blocked; guard cleared → allowed | I2 | ✅ |

### Phase 2.1 (coverage invariants) — the L1-regression rows are in §2; the legacy core rows:
| Test | Purpose | Invariant | Status |
|---|---|---|---|
| A1–A17 static source assertions | old buggy patterns gone (no time-release; broker-consult guard; no fabrication fallback; exactly-one-snapshot; scalp symbol guard) | I1–I10 | ✅ |
| B1–B13 live `_reconcile_pending_entry` + adversarial broker | point-of-truth per symbol incl. feed skew | I1/I2/I4 | ✅ |
| C1–C5 exhaustive stuck-order/replay | time NEVER releases; COMPLETE is the only position-creating terminal | I8 | ✅ |
| D1–D5 single-snapshot derivation | equivalence + mismatch/unknown/malformed convergence | I5/I6/I7 | ✅ |
| E1–E5e symbol-scoped adoption matrix | V1 regression matrix | I4 | ✅ |
| F1–F6 finalize idempotency + crash dedup | V3 regression matrix | I4/I7 | ✅ |

---

## 7. SAFETY COVERAGE MATRIX

| Hazard | Mitigation | Regression Test | Status | Residual Risk |
|---|---|---|---|---|
| **Duplicate BUY** | entry guard `_active_order_id` held until terminal; pending blocks re-entry | B6–B13, C1–C5, Phase2[6] | ✅ | genuine closed-fill stalls (R1) |
| **Wrong-symbol recovery** | symbol-scoped adoption vs one snapshot | A17, E1/E2/E5b | ✅ | pathological both-saved (R4) |
| **Feed lag** | terminal-but-flat retained; confirm on next snap; halt past window | B2/B9/B12/B13 | ✅ | operator release needed (R1) |
| **Duplicate SL** | broker dedup in `_sl_create` (find-before-place) | F1/F5, F6 | ✅ | trigger not re-verified (R2) |
| **Recovery mismatch** | single `_positions_snap`; all decisions read it; malformed→UNKNOWN | A12–A16, D1–D5 | ✅ | startup gate bypass (R5) |
| **Unknown broker** | fail-closed ENGINE_PAUSED; no adopt/flatten/guess | Phase1[2][4][5], B3/B10 | ✅ | none |
| **Crash during finalize** | guard returns existing position; SL reused; adoption never re-finalizes | F1–F6 | ✅ | in-memory marker (R3) |
| **Partial fill** | qty = confirmed, shrink, never oversell into short | B8/B9, Phase2[2][4] | ✅ | none |
| **Restart during entry** | recovered pending restored HELD; broker truth decides | B1–B6, E-blocks | ✅ | none |
| **Restart during exit** | exit confirmed only by terminal state; position kept on open/reject | Phase2[4][5] | ✅ | none |

---

## 8. PROJECT HEALTH SCORE (1–10, 10 = best)

| Area | Score | Rationale |
|---|---|---|
| **Execution Engine** | **8** | The four loss paths are closed and verified. Remaining R2 (SL reuse trigger) is a small correctness catch, not a loss path. |
| **Recovery** | **8** | Single-snapshot, symbol-scoped, deterministic, fail-closed. Startup gate (R5) and pathological-both-state (R4) are the deductions. |
| **Order Management** | **9** | Trackers + entry guard + pending reconciliation are tight; no forgotten/dropped/duplicated order demonstrated. |
| **Risk Engine** | **7** | Daily-loss kill switch present every cycle; L1 focus was execution, risk-gate tuning not re-audited this session (out of scope). |
| **ML** | **7** | ML pipeline unchanged this session; not the target of L1; retrain path exists. |
| **Strategy** | **7** | Signal generation unchanged; not audited this session. |
| **Backtesting** | **6** | Not exercised this session; no L1 guard covers it. |
| **Production Readiness** | **8** | Execution layer is safe against the four known loss paths; genuine-closed-fill operator escape (R1) and R3 marker worth an L2 pass before the widest deployment. |

---

## 9. NEXT STAGE ROADMAP

Prioritized by impact, additive only — **no full rewrite**:

1. **R2 — SL-dedup trigger re-verify (L2, smallest, safety).** In `_sl_create`, after reusing an existing SL, if its trigger ≠ intended stop-loss, `modify_protective_stop` to reconcile. Closes a validity gap in the dedup.
2. **R1 — `/resolve-pending` operator command (L2).** Re-entry when a genuine closed-fill stalls the engine; restores availability.
3. **R3 — persisted finalized-marker (L2).** Make finalize dedup survive restarts (the "mini-register"), removing the crash-window re-count.
4. **R5 — startup gate through the single snapshot (L2).** Close the `has_open_position()` bypass.

Then reassess; do NOT touch L3 architecture unless the above expose a recurring invariant.

---

## 10. TOMORROW'S FIRST TASK (implementation plan — NOT implementing)

**Objective:** eliminate the most safety-adjacent gap — **SL-dedup trigger re-verify (R2)** — a contained change to `_sl_create`.

- **Files likely affected:** `master_runner.py` (the `if found:` reuse branch in `_sl_create`); `data/_phase21_verify.py` (new F-group checks).
- **Estimated risk:** **Low.** The change is additive within an already-tested function; it only affects the *reuse* path (never the place path), so normal entries are unchanged.
- **Expected benefit:** protection level always matches intended `stop_loss` on the crash/re-finalize reuse path — closes the last verified validity gap in the SL.
- **Regression tests required:**
  - `_sl_create` with a resting SL at a *different* trigger + reuse → `modify_protective_stop` called to reconcile to `stop_loss`.
  - `_sl_create` with a resting SL at the *correct* trigger → no-op modify (reuse).
  - `_sl_create` with no resting SL → place (unchanged).
  - Add as an F-group; must not perturb any existing F1–F6 or the other suites.
- **Success criteria:** phase1 (26), phase2 (43), phase21 (95, now +3) all green; adversarial review shows exactly one SL always at the intended trigger; external audit shows no invariant regression; the change is confined to the reuse branch.

(Start on a new L2 working branch; run the same per-item gate: implement → all suites → adversarial review → external audit → halt on any failure.)

---

## 11. FINAL SELF-ASSESSMENT

**Would you deploy this with real money today?**

**Yes under explicit conditions — but not unconditionally.**

The **execution layer specifically** is ready: all four known software-induced loss paths (V1–V4) are closed, permanently locked by 95 phase21 checks + 43 phase2 + 26 phase1, and I could not demonstrate a surviving duplicate-SL / wrong-symbol / feed-lag / recovery-mismatch / duplicate-BUY path.

**Conditions that gate a go-live:**
1. **R2 closed** (SL reuse trigger re-verify) — a one-branch semantic audit.
2. **R1 operator escape exists** — an operator must resolve a genuine closed-fill hold without hand-editing the state file (recommend `/resolve-pending` before deployment).
3. **R5**: the startup `has_open_position()` gate must route through the single snapshot (or operators will lean on `ALLOW_BROKER_POSITION_ON_START`, a failed-open override).
4. A **live paper session** (DRY_RUN/PAPER) over the next week with the harnesses in the build to catch integration issues.
5. Backtesting & ML are untouched by L1 — no change, but should be re-run on the paper data once a stable day's log exists.

**If the above are not met — verified blockers remain** (in strict order):
- R2 reuse-verify gap — small code change.
- R1 no operator escape from a genuine closed-fill hold.
- R5 startup-gate bypass.
- R3 double-count window (cosmetic, not capital).

So: **proceed to L2 (≈ days), gate on R2/R1/R5, hold one live-observation week of green before going live. I am not recommending unconditional deployment today.**
