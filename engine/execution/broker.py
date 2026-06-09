# execution/broker.py
import os
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from kiteconnect import KiteConnect, KiteTicker

load_dotenv(os.path.join(os.getcwd(), ".env"), override=True)


class ZerodhaBroker:

    def __init__(self):
        self.api_key      = os.getenv("KITE_API_KEY",      "").strip()
        self.access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()
        if not self.api_key or not self.access_token:
            raise RuntimeError("API key or access token missing")

        self.kite = KiteConnect(api_key=self.api_key)
        self.kite.set_access_token(self.access_token)

        profile = self.kite.profile()
        print("[BROKER] REST AUTH OK:", profile["user_name"])

        print("[BROKER] Loading instruments...")
        self.instruments    = self.kite.instruments()
        self.instrument_map = {i["tradingsymbol"]: i for i in self.instruments}
        print(f"[BROKER] Loaded {len(self.instruments)} instruments")

        self.option_index = {}
        for inst in self.instruments:
            if inst["segment"] != "NFO-OPT" or inst["name"] != "NIFTY":
                continue
            key = (inst["strike"], inst["instrument_type"])
            self.option_index.setdefault(key, []).append(inst)

        self.ticker          = None
        self._last_ticks     = {}
        self._last_tick_time = time.time()   # grace period: watchdog won't alarm until 60s after start

    # ── websocket ────────────────────────────────────────────────────────────

    def start_feed(self, symbols):
        tokens = [
            self.instrument_map[s]["instrument_token"]
            for s in symbols if s in self.instrument_map
        ]
        if not tokens:
            print("[BROKER] No tokens for feed"); return

        # Close existing ticker before creating a new one
        if self.ticker is not None:
            try:
                self.ticker.close()
            except Exception:
                pass
            self.ticker = None

        self.ticker = KiteTicker(self.api_key, self.access_token)

        def on_ticks(ws, ticks):
            for tick in ticks:
                self._last_ticks[tick["instrument_token"]] = tick
            self._last_tick_time = time.time()

        def on_connect(ws, _):
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

        def on_close(ws, code, reason):
            print(f"[BROKER] Feed closed: {reason}")

        def on_error(ws, code, reason):
            print(f"[BROKER] Feed error {code}: {reason}")

        self.ticker.on_ticks   = on_ticks
        self.ticker.on_connect = on_connect
        self.ticker.on_close   = on_close
        self.ticker.on_error   = on_error
        self.ticker.connect(threaded=True)

    # ── prices ───────────────────────────────────────────────────────────────

    def ltp(self, instrument):
        try:
            if ":" in instrument:
                return list(self.kite.ltp(instrument).values())[0]["last_price"]
            inst = self.instrument_map.get(instrument)
            if not inst:
                return None
            sym  = f"NFO:{instrument}" if inst["segment"].startswith("NFO") else f"NSE:{instrument}"
            data = self.kite.ltp(sym)
            return data[sym]["last_price"] if sym in data else None
        except Exception:
            return None

    def get_bid_ask(self, symbol):
        try:
            inst = self.instrument_map.get(symbol)
            if inst:
                tick  = self._last_ticks.get(inst["instrument_token"])
                if tick:
                    depth = tick.get("depth", {})
                    bids  = depth.get("buy",  [])
                    asks  = depth.get("sell", [])
                    if bids and asks:
                        return bids[0]["price"], asks[0]["price"]
            sym   = symbol if ":" in symbol else f"NFO:{symbol}"
            quote = self.kite.quote([sym])
            data  = quote.get(sym)
            if not data:
                return None, None
            bids = data.get("depth", {}).get("buy",  [])
            asks = data.get("depth", {}).get("sell", [])
            return (bids[0]["price"] if bids else None,
                    asks[0]["price"] if asks else None)
        except Exception:
            return None, None

    # ── historical ───────────────────────────────────────────────────────────

    def get_historical(self, symbol, interval="minute", lookback=200):
        try:
            ts   = symbol.split(":")[1] if ":" in symbol else symbol
            inst = self.instrument_map.get(ts)
            if not inst:
                return []
            to_dt   = datetime.now()
            from_dt = to_dt - timedelta(days=10)
            data    = self.kite.historical_data(inst["instrument_token"], from_dt, to_dt, interval)
            return data[-lookback:]
        except Exception as e:
            print("[HISTORICAL ERROR]", e)
            return []

    # ── options ──────────────────────────────────────────────────────────────

    def get_atm_option(self, option_type="CE", strike_shift=0):
        spot = self.ltp("NSE:NIFTY 50")
        if not spot:
            return None, None
        atm    = round(spot / 50) * 50
        strike = atm - strike_shift * 50 if option_type == "CE" else atm + strike_shift * 50
        opts   = self.option_index.get((strike, option_type))
        if not opts:
            return None, None
        sel = sorted(opts, key=lambda x: x["expiry"])[0]
        return sel["tradingsymbol"], self.ltp(sel["tradingsymbol"])

    def get_option_chain_near_atm(self, strikes_range=5):
        try:
            spot = self.ltp("NSE:NIFTY 50")
            if not spot:
                return []
            atm   = round(spot / 50) * 50
            chain = []
            for i in range(-strikes_range, strikes_range + 1):
                s = atm + i * 50
                ce_list = self.option_index.get((s, "CE"))
                pe_list = self.option_index.get((s, "PE"))
                if not ce_list or not pe_list:
                    continue
                ce   = sorted(ce_list, key=lambda x: x["expiry"])[0]
                pe   = sorted(pe_list, key=lambda x: x["expiry"])[0]
                chain.append({
                    "strike": s,
                    "ce_oi":  self._last_ticks.get(ce["instrument_token"], {}).get("oi", 0),
                    "pe_oi":  self._last_ticks.get(pe["instrument_token"], {}).get("oi", 0),
                })
            return chain
        except Exception:
            return []

    # ── orders ───────────────────────────────────────────────────────────────

    def market_buy(self, symbol, qty):
        return self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR, exchange="NFO",
            tradingsymbol=symbol,
            transaction_type=self.kite.TRANSACTION_TYPE_BUY,
            quantity=qty, product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET,
        )

    def market_sell(self, symbol, qty):
        return self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR, exchange="NFO",
            tradingsymbol=symbol,
            transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=qty, product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET,
        )

    def place_order(self, **kwargs):
        return self.kite.place_order(**kwargs)

    # ── positions ────────────────────────────────────────────────────────────

    def get_positions(self):
        try:
            return self.kite.positions().get("net", [])
        except Exception as e:
            print(f"[BROKER] get_positions: {e}"); return []

    def has_open_position(self):
        try:
            return any(int(p.get("quantity", 0)) != 0 for p in self.get_positions())
        except Exception as e:
            print(f"[BROKER] has_open_position: {e}"); return True

    def get_order_average_price(self, order_id):
        try:
            for o in self.kite.orders():
                if o["order_id"] == order_id:
                    return float(o.get("average_price", 0))
        except Exception as e:
            print(f"[BROKER] get_order_average_price: {e}")
        return None
