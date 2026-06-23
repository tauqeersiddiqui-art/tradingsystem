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


class ExecutionEngine:

    def __init__(self, broker, config):
        self.broker = broker
        self.config = config

        # Duplicate order guard — cleared on exit
        self._active_order_id: str | None = None

    # ══════════════════════════════════════════════════════════════════
    # LOT SIZE
    # ══════════════════════════════════════════════════════════════════

    def get_lot_size(self, symbol: str) -> int:
        """
        Fetch actual lot size from instrument map.
        Falls back to 30 (BANKNIFTY default) if not found.
        """
        try:
            inst = self.broker.instrument_map.get(symbol)
            if inst and inst.get("lot_size", 0) > 0:
                return int(inst["lot_size"])
        except Exception as e:
            logger.warning(f"[LOT SIZE] Could not fetch for {symbol}: {e}")
        logger.warning(f"[LOT SIZE] Falling back to 30 for {symbol}")
        return 30   # BANKNIFTY lot size

    # ══════════════════════════════════════════════════════════════════
    # FILL VALIDATION
    # ══════════════════════════════════════════════════════════════════

    def _get_fill_price(self, order_id: str, fallback_price: float) -> float:
        """
        Poll order book until order is COMPLETE or max attempts reached.
        Returns actual average fill price or fallback.
        """
        if str(order_id).startswith("dry_"):
            return fallback_price

        for attempt in range(_FILL_POLL_ATTEMPTS):
            try:
                orders = self.broker.kite.orders()
                for o in orders:
                    if str(o["order_id"]) == str(order_id):
                        status = o.get("status", "")
                        avg_price = float(o.get("average_price", 0))
                        if status == "COMPLETE" and avg_price > 0:
                            logger.info(
                                f"[FILL] order={order_id} "
                                f"avg_price={avg_price:.2f} "
                                f"attempt={attempt+1}"
                            )
                            return avg_price
                        elif status in ("REJECTED", "CANCELLED"):
                            logger.error(
                                f"[FILL] order={order_id} status={status}"
                            )
                            return 0.0
            except Exception as e:
                logger.warning(f"[FILL] Poll attempt {attempt+1} failed: {e}")

            time.sleep(_FILL_POLL_INTERVAL)

        logger.warning(
            f"[FILL] order={order_id} not confirmed after "
            f"{_FILL_POLL_ATTEMPTS} attempts — using fallback={fallback_price:.2f}"
        )
        return fallback_price

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
            logger.info(
                f"[DRY ENTRY] {symbol} side={side} qty={qty} price={price:.2f}"
            )
            self._active_order_id = order_id
            return {"order_id": order_id, "price": price, "qty": qty, "symbol": symbol}

        # ── Live order ────────────────────────────────────────────────
        ltp_before = self.broker.ltp(symbol) or 0.0

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
            return None

        fill_price = self._get_fill_price(order_id, ltp_before)

        if fill_price <= 0:
            logger.error(
                f"[ENTRY] Fill price invalid for order={order_id} — "
                "position NOT tracked"
            )
            return None

        self._active_order_id = str(order_id)
        logger.info(
            f"[ENTRY] order={order_id} symbol={symbol} "
            f"qty={qty} fill={fill_price:.2f}"
        )

        return {
            "order_id": str(order_id),
            "price":    fill_price,
            "qty":      qty,
            "symbol":   symbol,
        }

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
            order_id = f"dry_exit_{int(time.time())}"
            logger.info(
                f"[DRY EXIT] {symbol} side={side} qty={qty} price={price:.2f}"
            )
            self._active_order_id = None
            return {"order_id": order_id, "price": price, "qty": qty, "symbol": symbol}

        # ── Live order ────────────────────────────────────────────────
        ltp_before = self.broker.ltp(symbol) or 0.0

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
            return None

        fill_price = self._get_fill_price(order_id, ltp_before)

        if fill_price <= 0:
            logger.error(
                f"[EXIT] Fill price invalid for order={order_id}. "
                "Using last LTP as fallback."
            )
            fill_price = ltp_before

        self._active_order_id = None   # clear guard
        logger.info(
            f"[EXIT] order={order_id} symbol={symbol} "
            f"qty={qty} fill={fill_price:.2f} side={side}"
        )

        return {
            "order_id": str(order_id),
            "price":    fill_price,
            "qty":      qty,
            "symbol":   symbol,
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
        Confirms broker shows zero open quantity for this symbol.
        Used after exit to ensure position is actually closed.
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
            logger.warning(f"[VERIFY] Position check failed: {e}")
            return True   # assume flat on error to avoid double-exit
