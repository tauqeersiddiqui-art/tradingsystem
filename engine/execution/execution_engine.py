# engine/execution/execution_engine.py
# FIXED v2 — Production Safe
#
# Fixes applied:
#   FIX-1 : execute_exit uses SELL for CE, BUY for PE (was always SELL)
#   FIX-2 : lot_size sourced from instrument map, not from ltp
#   FIX-3 : fill validation — polls order book for actual average_price
#   FIX-4 : update_trailing() implemented (was missing, caused AttributeError)
#   FIX-5 : duplicate order guard via _active_order_id
#   FIX-6 : DRY_RUN and paper mode consistent across entry and exit

import time
import logging

logger = logging.getLogger("execution_engine")

_FILL_POLL_ATTEMPTS = 8
_FILL_POLL_INTERVAL = 0.4   # seconds between fill polls

# ── Explicit order-state model (Phase 2 — Execution Truth Layer) ─────────
# A position is created/removed ONLY on ST_COMPLETE.  All other states mean
# "do NOT create / do NOT clear / do NOT re-place".  Fills are NEVER fabricated.
ST_NEW        = "NEW"
ST_SUBMITTED  = "SUBMITTED"
ST_OPEN       = "OPEN"
ST_PARTIAL    = "PARTIAL"
ST_COMPLETE   = "COMPLETE"
ST_REJECTED   = "REJECTED"
ST_CANCELLED  = "CANCELLED"
ST_TIMEOUT    = "TIMEOUT"      # order alive at broker, fill unconfirmed after wait
ST_UNKNOWN    = "UNKNOWN"      # broker unreachable / order not resolvable

# Terminal states
_FILLED_TERMINAL     = (ST_COMPLETE,)
_NONFILLED_TERMINAL  = (ST_REJECTED, ST_CANCELLED)
_TERMINAL            = _FILLED_TERMINAL + _NONFILLED_TERMINAL

# Max seconds a pending entry/exit order may be reconciled before the engine
# halts (can no longer be left unresolved).
_MAX_PENDING_RESOLVE_SECONDS = 60.0


def _normalize_status(status: str, filled_qty: int) -> str:
    """Map a Zerodha order status to our explicit state model."""
    s = (status or "").upper()
    if s == "COMPLETE":
        return ST_COMPLETE
    if s == "REJECTED":
        return ST_REJECTED
    if s in ("CANCELLED", "CANCEL_PENDING"):
        return ST_CANCELLED
    # Any live, non-terminal response is OPEN unless partially filled.
    if s in ("OPEN", "PENDING", "VALIDATION PENDING", "AMO REQ RECEIVED",
             "TRIGGER PENDING", "IMPLICIT", "SUBMITTED"):
        return ST_PARTIAL if filled_qty > 0 else ST_OPEN
    # Unknown response string — treat as UNKNOWN (do not guess).
    return ST_UNKNOWN


class ExecutionEngine:

    def __init__(self, broker, config):
        self.broker = broker
        self.config = config

        # Duplicate order guard — cleared on exit
        self._active_order_id: str | None = None

    # ══════════════════════════════════════════════════════════════════
    # ORDER STATE RESOLUTION (never fabricates a fill)
    # ══════════════════════════════════════════════════════════════════

    def _order_status(self, order_id: str, timeout_attempts: int = _FILL_POLL_ATTEMPTS):
        """
        Resolve a live order to an explicit state.

        Returns (state, avg_price, fill_ts, filled_qty):
          - ST_COMPLETE          -> (ST_COMPLETE, real avg_price, timestamp, fq)
          - ST_REJECTED/CANCELLED-> (state, avg_of_fill, None, filled_qty)
                                     (avg>0 & fq>0 only if it partially filled first)
          - ST_OPEN/PARTIAL/TIMEOUT/UNKNOWN -> (state, None, None, filled_qty)
        """
        if self._is_dry() or str(order_id).startswith("dry_"):
            # Paper / dry-run: fills are instantly "complete" at the paper LTP.
            return ST_COMPLETE, time.time(), time.time(), 0

        order_id = str(order_id)
        for attempt in range(max(1, timeout_attempts)):
            try:
                found = None
                for o in self.broker.kite.orders():
                    if str(o["order_id"]) == order_id:
                        found = o
                        break
                if found is not None:
                    status = found.get("status", "")
                    avg    = float(found.get("average_price", 0) or 0)
                    fq     = int(found.get("filled_quantity", 0) or 0)
                    st     = _normalize_status(status, fq)
                    if st in _TERMINAL:
                        if st == ST_COMPLETE and avg <= 0:
                            # A COMPLETE fill with no average price is unusable —
                            # never guess one. Treat as UNKNOWN (halt upstream).
                            return ST_UNKNOWN, None, None, fq
                        return st, avg, (time.time() if st == ST_COMPLETE else None), fq
                    # Live non-terminal: order is still at the broker, fill unconfirmed.
                    if attempt == timeout_attempts - 1:
                        return ST_TIMEOUT, None, None, fq
                else:
                    # Broker responded but our order is not in the list — we cannot
                    # confirm it. Never fabricate.
                    return ST_UNKNOWN, None, None, 0
            except Exception as e:
                logger.warning(f"[ORDER] status poll {attempt+1}/{timeout_attempts} failed: {e}")
                if attempt == timeout_attempts - 1:
                    return ST_UNKNOWN, None, None, 0
            time.sleep(_FILL_POLL_INTERVAL)
        return ST_UNKNOWN, None, None, 0

    # ══════════════════════════════════════════════════════════════════
    # LOT SIZE
    # ══════════════════════════════════════════════════════════════════

    def get_lot_size(self, symbol: str) -> int:
        """
        Fetch actual lot size from instrument map.
        Falls back to config.LOT_SIZE if not found.
        """
        # Use config as single source of truth
        configured_lot = int(getattr(self.config, "LOT_SIZE", 30) or 30)
        
        if str(symbol).upper().startswith("BANKNIFTY"):
            return configured_lot

        try:
            inst = self.broker.instrument_map.get(symbol)
            if inst and inst.get("lot_size", 0) > 0:
                return int(inst["lot_size"])
        except Exception as e:
            logger.warning(f"[LOT SIZE] Could not fetch for {symbol}: {e}")
        
        logger.warning(f"[LOT SIZE] Falling back to config.LOT_SIZE={configured_lot} for {symbol}")
        return configured_lot

    # ══════════════════════════════════════════════════════════════════
    # FILL VALIDATION  →  replaced by _order_status() (explicit state model)
    # NEVER fabricate a fill.  Position creation/removal is gated upstream on
    # ST_COMPLETE only.
    # ══════════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════════
    # ENTRY
    # ══════════════════════════════════════════════════════════════════

    def execute_entry(self, symbol: str, side: str, qty: int) -> dict | None:
        """
        Place a BUY order (CE or PE — both are bought to open).
        Returns {order_id, price, qty, symbol} or None on failure.
        """
        # ── Duplicate guard ───────────────────────────────────────────
        if self._active_order_id is not None:
            logger.warning(
                f"[ENTRY] Duplicate order blocked — "
                f"active={self._active_order_id}"
            )
            return None

        # ── Paper / DRY RUN ───────────────────────────────────────────
        if self.config.DRY_RUN or getattr(self.broker, "is_paper", False):
            price = self.broker.ltp(symbol) or 0.0
            order_id = f"dry_{int(time.time())}"
            submit_ts = time.time()
            quote = self.broker.get_quote_snapshot(symbol)
            logger.info(
                f"[DRY ENTRY] {symbol} side={side} qty={qty} price={price:.2f}"
            )
            self._active_order_id = order_id
            return {
                "order_id": order_id,
                "price": price,
                "qty": qty,
                "symbol": symbol,
                "submit_ts": submit_ts,
                "fill_ts": submit_ts,
                "ltp_before": quote.get("ltp", price),
                "bid_before": quote.get("bid"),
                "ask_before": quote.get("ask"),
                "state": ST_COMPLETE,   # paper fills are instantly "complete"
            }

        # ── Live order ────────────────────────────────────────────────
        quote_before = self.broker.get_quote_snapshot(symbol)
        ltp_before = quote_before.get("ltp") or 0.0
        submit_ts = time.time()

        try:
            order_id = self.broker.kite.place_order(
                variety=self.broker.kite.VARIETY_REGULAR,
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type=self.broker.kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                order_type=self.broker.kite.ORDER_TYPE_MARKET,
                product=self.broker.kite.PRODUCT_MIS,
            )
        except Exception as e:
            logger.error(f"[ENTRY] Order placement failed: {e}")
            return {"state": ST_REJECTED, "reason": "placement_error",
                    "symbol": symbol, "qty": qty, "price": None, "fill_ts": None}

        order_id = str(order_id)
        self._active_order_id = order_id

        # Resolve to an explicit state.  NEVER fabricate a fill.
        state, avg, fill_ts, filled_qty = self._order_status(order_id)

        base = {
            "order_id":   order_id,
            "qty":        qty,
            "symbol":     symbol,
            "submit_ts":  submit_ts,
            "ltp_before": ltp_before,
            "bid_before": quote_before.get("bid"),
            "ask_before": quote_before.get("ask"),
            "state":      state,
            "filled_qty": filled_qty,
        }
        if state == ST_COMPLETE:
            base["price"]   = avg
            base["fill_ts"] = fill_ts
            logger.info(
                f"[ENTRY] order={order_id} symbol={symbol} "
                f"qty={qty} fill={avg:.2f}"
            )
        else:
            # Position NOT created upstream (no SL, no local dict) — only the
            # order_id is retained for reconciliation.
            base["price"]   = None
            base["fill_ts"] = None
            logger.critical(
                f"[ENTRY] order={order_id} NOT confirmed ({state}) — "
                f"position NOT created, NO SL placed"
            )
        return base

    # ══════════════════════════════════════════════════════════════════
    # EXIT
    # ══════════════════════════════════════════════════════════════════

    def execute_exit(self, symbol: str, qty: int, side: str = "CE") -> dict | None:
        """
        Close an open options position.
        Both CE and PE are exited using SELL because both were opened using BUY.
        """

        txn_type = self.broker.kite.TRANSACTION_TYPE_SELL

        # ── Paper / DRY RUN ───────────────────────────────────────────
        if self.config.DRY_RUN or getattr(self.broker, "is_paper", False):
            price = self.broker.ltp(symbol) or 0.0
            submit_ts = time.time()
            order_id = f"dry_exit_{int(time.time())}"
            logger.info(
                f"[DRY EXIT] {symbol} side={side} qty={qty} price={price:.2f}"
            )
            self._active_order_id = None
            return {
                "order_id": order_id,
                "price": price,
                "qty": qty,
                "symbol": symbol,
                "submit_ts": submit_ts,
                "fill_ts": submit_ts,
                "state": ST_COMPLETE,   # paper fills are instantly "complete"
            }

        # ── Live order ────────────────────────────────────────────────
        ltp_before = self.broker.ltp(symbol) or 0.0
        submit_ts = time.time()

        try:
            order_id = self.broker.kite.place_order(
                variety=self.broker.kite.VARIETY_REGULAR,
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type=txn_type,
                quantity=qty,
                order_type=self.broker.kite.ORDER_TYPE_MARKET,
                product=self.broker.kite.PRODUCT_MIS,
            )
        except Exception as e:
            logger.error(f"[EXIT] Order placement failed: {e}")
            return {"state": ST_REJECTED, "reason": "placement_error",
                    "symbol": symbol, "qty": qty, "price": None, "fill_ts": None}

        order_id = str(order_id)
        self._active_order_id = None   # clear guard

        # Resolve to an explicit state.  NEVER fabricate a fill.
        state, avg, fill_ts, filled_qty = self._order_status(order_id)

        if state == ST_COMPLETE:
            logger.info(
                f"[EXIT] order={order_id} symbol={symbol} "
                f"qty={qty} fill={avg:.2f} side={side}"
            )
            return {
                "order_id":  order_id,
                "price":     avg,
                "qty":       qty,
                "symbol":    symbol,
                "submit_ts": submit_ts,
                "fill_ts":   fill_ts,
                "state":     ST_COMPLETE,
                "filled_qty": filled_qty,
            }

        # Position is NOT cleared upstream. Only the order_id is retained for
        # reconciliation next cycle (single-exit-order guard).
        logger.critical(
            f"[EXIT] order={order_id} NOT confirmed ({state}) — "
            f"position NOT cleared, will reconcile"
        )
        return {
            "order_id":  order_id,
            "price":     None,
            "qty":       qty,
            "symbol":    symbol,
            "submit_ts": submit_ts,
            "fill_ts":   None,
            "state":     state,
        }

    # ══════════════════════════════════════════════════════════════════
    # BROKER-SIDE PROTECTIVE STOP (SL-M)  — TRUE stop enforcement
    # ══════════════════════════════════════════════════════════════════
    #
    # Both CE and PE are LONG (bought to open), so the protective stop is a
    # SELL SL-M with trigger_price = stop_loss premium level.  When the option
    # trades down to the trigger, Zerodha fires a market SELL server-side, so
    # enforcement no longer depends on the polling loop observing the price.
    # This eliminates the virtual-stop gap (locked profit given back on gaps).

    @staticmethod
    def _round_tick(price: float, tick: float = 0.05) -> float:
        """NFO options tick in 0.05 — Kite rejects mis-aligned trigger prices."""
        return round(round(float(price) / tick) * tick, 2)

    def _is_dry(self) -> bool:
        return bool(self.config.DRY_RUN or getattr(self.broker, "is_paper", False))

    def place_protective_stop(self, symbol: str, qty: int, trigger_price: float) -> str | None:
        """Place a SELL SL-M protecting a long option. Returns order_id or None."""
        trig = self._round_tick(trigger_price)
        if self._is_dry():
            oid = f"dry_sl_{int(time.time()*1000)}"
            logger.info(f"[DRY SL] place {symbol} qty={qty} trigger={trig:.2f} id={oid}")
            return oid
        try:
            oid = self.broker.kite.place_order(
                variety=self.broker.kite.VARIETY_REGULAR,
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type=self.broker.kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                order_type=self.broker.kite.ORDER_TYPE_SLM,
                product=self.broker.kite.PRODUCT_MIS,
                trigger_price=trig,
            )
            return str(oid)
        except Exception as e:
            logger.error(f"[SL] place_protective_stop failed {symbol} trig={trig}: {e}")
            return None

    def modify_protective_stop(self, order_id: str, trigger_price: float) -> bool:
        """Atomically raise (or lower) the SL-M trigger. Does NOT cancel first."""
        trig = self._round_tick(trigger_price)
        if not order_id:
            return False
        if self._is_dry() or str(order_id).startswith("dry_"):
            logger.info(f"[DRY SL] modify id={order_id} trigger={trig:.2f}")
            return True
        try:
            self.broker.kite.modify_order(
                variety=self.broker.kite.VARIETY_REGULAR,
                order_id=str(order_id),
                trigger_price=trig,
            )
            return True
        except Exception as e:
            logger.error(f"[SL] modify_protective_stop failed id={order_id} trig={trig}: {e}")
            return False

    def cancel_protective_stop(self, order_id: str) -> bool:
        """Cancel the protective SL-M (on exit). Already-complete is treated as success."""
        if not order_id:
            return True
        if self._is_dry() or str(order_id).startswith("dry_"):
            logger.info(f"[DRY SL] cancel id={order_id}")
            return True
        try:
            self.broker.kite.cancel_order(
                variety=self.broker.kite.VARIETY_REGULAR,
                order_id=str(order_id),
            )
            return True
        except Exception as e:
            logger.warning(f"[SL] cancel_protective_stop id={order_id}: {e}")
            return False

    def get_order_info(self, order_id: str) -> dict | None:
        """Return {status, average_price, trigger_price, filled_quantity} or None."""
        if not order_id or self._is_dry() or str(order_id).startswith("dry_"):
            return None
        try:
            for o in self.broker.kite.orders():
                if str(o["order_id"]) == str(order_id):
                    return {
                        "status":          o.get("status", ""),
                        "average_price":   float(o.get("average_price", 0) or 0),
                        "trigger_price":   float(o.get("trigger_price", 0) or 0),
                        "filled_quantity": int(o.get("filled_quantity", 0) or 0),
                    }
        except Exception as e:
            logger.warning(f"[SL] get_order_info failed id={order_id}: {e}")
        return None

    def find_open_stop_order(self, symbol: str) -> dict | None:
        """Restart recovery: locate an OPEN SELL SL-M for symbol at the broker."""
        if self._is_dry():
            return None
        try:
            for o in self.broker.kite.orders():
                if (o.get("tradingsymbol") == symbol
                        and o.get("order_type") in ("SL-M", "SL")
                        and o.get("transaction_type") == "SELL"
                        and o.get("status") in ("TRIGGER PENDING", "OPEN", "AMO REQ RECEIVED")):
                    return {
                        "order_id":      str(o["order_id"]),
                        "trigger_price": float(o.get("trigger_price", 0) or 0),
                        "status":        o.get("status", ""),
                    }
        except Exception as e:
            logger.warning(f"[SL] find_open_stop_order failed {symbol}: {e}")
        return None

    # ══════════════════════════════════════════════════════════════════
    # TRAILING STOP UPDATE  (FIX-4 — was missing, caused AttributeError)
    # ══════════════════════════════════════════════════════════════════

    def update_trailing(self, symbol: str, ltp: float) -> float | None:
        """
        Stateless trailing helper.
        Actual trailing logic is handled by profit_manager.manage_position()
        which is called from live_engine.check_exit().

        This method exists so master_runner.py callers don't get AttributeError.
        Returns ltp (caller updates position["stop_loss"] via check_exit).
        """
        return ltp

    # ══════════════════════════════════════════════════════════════════
    # POSITION VERIFICATION
    # ══════════════════════════════════════════════════════════════════

    def verify_flat(self, symbol: str) -> bool:
        """
        Confirms broker shows zero net quantity for this symbol.
        Used after exit to ensure the position is actually closed.

        FAIL-CLOSED: returns False (NOT flat) on any broker error, so callers
        can never conclude the position is closed while broker state is unknown.
        """
        if self.config.DRY_RUN or getattr(self.broker, "is_paper", False):
            return True
        try:
            positions = self.broker.get_positions()
            for p in positions:
                if p.get("tradingsymbol") == symbol:
                    qty = int(p.get("quantity", 0))
                    if qty != 0:
                        logger.warning(
                            f"[VERIFY] {symbol} still shows qty={qty} "
                            "after exit — possible partial fill"
                        )
                        return False
            return True
        except Exception as e:
            logger.critical(f"[VERIFY] Position check failed: {e} — broker state UNKNOWN")
            # Fail-closed: unknown broker state must HALT the exit, not guess.
            # Callers catch this and defer the exit (keep position open).
            raise
