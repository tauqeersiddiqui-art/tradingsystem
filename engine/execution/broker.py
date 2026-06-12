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

        # Option-chain subscription state.  Populated by subscribe_options();
        # persisted so on_connect can re-subscribe after a watchdog reconnect.
        self._option_tokens:  list = []   # integer instrument tokens
        self._option_symbols: list = []   # tradingsymbols (for logging only)

        # ATM-specific tracking for subscription drift detection and diagnostics
        self._subscribed_atm:  int        = 0     # ATM strike used for last subscription
        self._atm_ce_token:    int | None = None  # instrument_token of the ATM CE
        self._atm_pe_token:    int | None = None  # instrument_token of the ATM PE

    # ── websocket ────────────────────────────────────────────────────────────

    def start_feed(self, symbols):
        tokens = [
            self.instrument_map[s]["instrument_token"]
            for s in symbols if s in self.instrument_map
        ]
        if not tokens:
            print("[BROKER] No tokens for feed"); return
        print(f"[BROKER] start_feed tokens: {dict(zip(symbols, tokens))}")

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
                token = tick["instrument_token"]
                # Preserve OI from a previous FULL-mode tick when a QUOTE-mode
                # packet arrives without the oi key. Zerodha sends mixed packet
                # sizes (44-byte QUOTE on price changes, 184-byte FULL when OI
                # refreshes), so overwriting unconditionally would zero out OI.
                if token in self._last_ticks and "oi" not in tick:
                    prev = self._last_ticks[token]
                    for _k in ("oi", "oi_day_high", "oi_day_low"):
                        if _k in prev:
                            tick[_k] = prev[_k]
                self._last_ticks[token] = tick
            self._last_tick_time = time.time()

        def on_connect(ws, _):
            ws.subscribe(tokens)
            # NIFTY 50 index (token 256265) does not carry bid/ask depth.
            # MODE_FULL on an index token either returns 0 for last_price or
            # silently drops the tick on some Zerodha gateway versions.
            # Use MODE_QUOTE which reliably includes last_price for indices.
            ws.set_mode(ws.MODE_QUOTE, tokens)
            # Restore option-chain subscriptions after any reconnect.
            # On the first connect this list is empty; once subscribe_options()
            # has run, every subsequent reconnect re-applies the same set so OI
            # and option LTPs resume without restarting the engine.
            if self._option_tokens:
                ws.subscribe(self._option_tokens)
                ws.set_mode(ws.MODE_FULL, self._option_tokens)  # MODE_FULL required for OI
                print(
                    f"[BROKER] on_connect: re-subscribed {len(self._option_tokens)}"
                    " option tokens"
                )

        def on_close(ws, code, reason):
            print(f"[BROKER] Feed closed: {reason}")

        def on_error(ws, code, reason):
            print(f"[BROKER] Feed error {code}: {reason}")

        self.ticker.on_ticks   = on_ticks
        self.ticker.on_connect = on_connect
        self.ticker.on_close   = on_close
        self.ticker.on_error   = on_error
        self.ticker.connect(threaded=True)

    # ── option-chain subscriptions ───────────────────────────────────────────

    def subscribe_options(self, strikes_range: int = 5) -> None:
        """
        Subscribe ATM CE/PE options and nearby strikes to the live WebSocket.

        Computes the current ATM from NIFTY LTP, then subscribes the nearest
        weekly/monthly expiry for every strike in [ATM - strikes_range*50 …
        ATM + strikes_range*50] for both CE and PE (11 strikes × 2 sides = 22
        tokens plus the NIFTY index already subscribed = 23 total).

        MODE_QUOTE is used so ticks carry both `last_price` and `oi`.
        Once subscribed, `get_option_chain_near_atm()` will return live OI
        values instead of zero.

        Safe to call at any time after start_feed(); idempotent — re-calling
        refreshes the ATM and re-subscribes to the updated strike set.
        """
        spot = self.ltp("NSE:NIFTY 50")
        if not spot:
            print("[OPTIONS FEED] Cannot compute ATM — no NIFTY spot price yet")
            return

        atm = round(spot / 50) * 50
        self._subscribed_atm = atm

        tokens_to_sub:  list = []
        symbols_to_sub: list = []
        atm_ce_token:   int | None = None
        atm_pe_token:   int | None = None
        atm_ce_sym:     str = "n/a"
        atm_pe_sym:     str = "n/a"

        for i in range(-strikes_range, strikes_range + 1):
            strike = atm + i * 50
            for opt_type in ("CE", "PE"):
                opts = self.option_index.get((strike, opt_type))
                if not opts:
                    continue
                # Nearest expiry first
                inst = sorted(opts, key=lambda x: x["expiry"])[0]
                tok  = inst["instrument_token"]
                sym  = inst["tradingsymbol"]
                tokens_to_sub.append(tok)
                symbols_to_sub.append(sym)
                if strike == atm and opt_type == "CE":
                    atm_ce_token = tok
                    atm_ce_sym   = sym
                if strike == atm and opt_type == "PE":
                    atm_pe_token = tok
                    atm_pe_sym   = sym

        if not tokens_to_sub:
            print("[OPTIONS FEED] No option tokens found near ATM — instrument list may be stale")
            return

        self._option_tokens  = tokens_to_sub
        self._option_symbols = symbols_to_sub
        self._atm_ce_token   = atm_ce_token
        self._atm_pe_token   = atm_pe_token

        # Push subscriptions onto the live WebSocket connection (if up).
        # If the ticker is not yet connected, tokens are stored and on_connect
        # will apply them on first/next connect.
        if self.ticker is not None:
            try:
                self.ticker.subscribe(tokens_to_sub)
                self.ticker.set_mode(self.ticker.MODE_FULL, tokens_to_sub)  # MODE_FULL required for OI
            except Exception as exc:
                print(f"[OPTIONS FEED] subscribe() call failed: {exc}")
                return

        nifty_token  = 256265
        chain_count  = len(tokens_to_sub) - (
            (1 if atm_ce_token else 0) + (1 if atm_pe_token else 0)
        )
        total_tokens = len(tokens_to_sub) + 1   # +1 for NIFTY 50

        print(f"[WS SUBSCRIBED]")
        print(f"  NIFTY      token={nifty_token}")
        print(f"  ATM CE     token={atm_ce_token}  ({atm_ce_sym})")
        print(f"  ATM PE     token={atm_pe_token}  ({atm_pe_sym})")
        print(f"  CHAIN TOKENS={chain_count}  (±{strikes_range} strikes excluding ATM)")
        print(f"  TOTAL TOKENS={total_tokens}")

    def refresh_atm_if_drifted(self, drift_points: int = 100) -> bool:
        """
        Re-subscribe options if NIFTY ATM has moved by drift_points since the
        last subscribe_options() call.  Call periodically from the engine loop.
        Returns True if a re-subscription was triggered, False otherwise.
        """
        if not self._subscribed_atm:
            return False
        spot = self.ltp("NSE:NIFTY 50")
        if not spot:
            return False
        current_atm = round(spot / 50) * 50
        if abs(current_atm - self._subscribed_atm) >= drift_points:
            print(
                f"[OPTIONS FEED] ATM drifted: {self._subscribed_atm} -> {current_atm} "
                f"- refreshing subscriptions"
            )
            self.subscribe_options()
            return True
        return False

    def get_option_feed_diagnostics(self) -> dict:
        """
        Return a snapshot of live option-feed health.
        Used for periodic [OPTION FEED] log in the engine loop.
        """
        ce_oi = (
            self._last_ticks.get(self._atm_ce_token, {}).get("oi", 0)
            if self._atm_ce_token else 0
        )
        pe_oi = (
            self._last_ticks.get(self._atm_pe_token, {}).get("oi", 0)
            if self._atm_pe_token else 0
        )
        chain_live = sum(1 for t in self._option_tokens if t in self._last_ticks)
        return {
            "ce_oi":             ce_oi,
            "pe_oi":             pe_oi,
            "chain_tokens_live": chain_live,
            "chain_tokens_total": len(self._option_tokens),
            "atm":               self._subscribed_atm,
        }

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
