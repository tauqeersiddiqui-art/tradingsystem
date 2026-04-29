import time


class ExecutionEngine:

    def __init__(self, broker, config):
        self.broker = broker
        self.config = config

    def execute_entry(self, symbol, side, qty):

        if self.config.DRY_RUN or getattr(self.broker, "is_paper", False):
            price = self.broker.ltp(symbol)
            print(f"[DRY ENTRY] {symbol} qty={qty} price={price}")
            return {"order_id": f"dry_{int(time.time())}", "price": price}

        try:
            order_id = self.broker.kite.place_order(
                variety=self.broker.kite.VARIETY_REGULAR,
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type=self.broker.kite.TRANSACTION_TYPE_BUY,
                quantity=qty,
                order_type=self.broker.kite.ORDER_TYPE_MARKET,
                product=self.broker.kite.PRODUCT_MIS
            )
            return {"order_id": order_id, "price": self.broker.ltp(symbol)}
        except Exception as e:
            print("[ORDER ERROR]", e)
            return None

    def execute_exit(self, symbol, qty):

        if self.config.DRY_RUN or getattr(self.broker, "is_paper", False):
            price = self.broker.ltp(symbol)
            print(f"[DRY EXIT] {symbol} price={price}")
            return {"order_id": f"dry_exit_{int(time.time())}", "price": price}

        try:
            order_id = self.broker.kite.place_order(
                variety=self.broker.kite.VARIETY_REGULAR,
                exchange="NFO",
                tradingsymbol=symbol,
                transaction_type=self.broker.kite.TRANSACTION_TYPE_SELL,
                quantity=qty,
                order_type=self.broker.kite.ORDER_TYPE_MARKET,
                product=self.broker.kite.PRODUCT_MIS
            )
            return {"order_id": order_id, "price": self.broker.ltp(symbol)}
        except Exception as e:
            print("[EXIT ERROR]", e)
            return None