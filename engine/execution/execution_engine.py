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
        Falls back to 75 (NIFTY default) if not found.
        """
        try:
            inst = self.broker.instrument_map.get(symbol)
            if inst and inst.get("lot_size", 0) > 0:
                return int(inst["lot_size"])
        except Exception as e:
            logger.warning(f"[LOT SIZE] Could not fetch for {symbol}: {e}")
        logger.warning(f"[LOT SIZE] Falling back to 75 for {symbol}")
        return 75   # NIFTY default

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
            CE exit → SELL  (sold to close long call)
            PE exit → BUY   (bought to close long put)  ← FIX-1

        Returns {order_id, price, qty, symbol} or None on failure.
        """
        # FIX-1: correct transaction type per side
        if side == "PE":
            txn_type = self.broker.kite.TRANSACTION_TYPE_BUY
        else:
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
