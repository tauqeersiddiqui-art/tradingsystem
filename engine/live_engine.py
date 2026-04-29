# engine/live_engine.py

import os
from datetime import datetime
from ml.predictor_champion import ChampionPredictor


class LiveEngine:

    def __init__(self, ctx):
        self.ctx = ctx

        self.predictor = ChampionPredictor()
        self.learner = ctx.ml_learner

        self.orb_high = None
        self.orb_low = None
        self.orb_done = False

    # ================= ORB ================= #

    def update_orb(self, candles, ts):

        # build ORB using first 30 cycles (paper mode safe)
        if self.ctx.cycle_count < 30:

            price = candles[-1]

            if self.orb_high is None:
                self.orb_high = price
                self.orb_low = price
            else:
                self.orb_high = max(self.orb_high, price)
                self.orb_low = min(self.orb_low, price)

        else:
            if not self.orb_done:
                print(f"[ORB BUILT] High={self.orb_high} Low={self.orb_low}")
            self.orb_done = True

    # ================= FEATURES ================= #

    def build_features(self, candles, highs, lows):

        if len(candles) < 60:
            return None

        close = candles[-1]

        # ================= BASIC ================= #
        ema20 = sum(candles[-20:]) / 20
        ema50 = sum(candles[-50:]) / 50

        # ================= RETURNS ================= #
        returns = (candles[-1] - candles[-2]) / candles[-2]

        # ================= VOLATILITY ================= #
        volatility = (max(candles[-20:]) - min(candles[-20:])) / close

        # ================= ATR ================= #
        atr = max(highs[-14:]) - min(lows[-14:])

        # ================= TREND ================= #
        trend_strength = (ema20 - ema50) / close

        # ================= MACD ================= #
        macd = ema20 - ema50

        # ================= RSI ================= #
        gains = []
        losses = []

        for i in range(-14, -1):
            diff = candles[i+1] - candles[i]
            if diff > 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))

        avg_gain = sum(gains) / 14 if gains else 0.0001
        avg_loss = sum(losses) / 14 if losses else 0.0001

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        # ================= TIME ================= #
        mins_since_open = self.ctx.cycle_count
        mins_to_close = max(0, 375 - self.ctx.cycle_count)

        # ================= OPTIONS PROXY ================= #
        moneyness = (close - ema20) / close
        time_to_expiry_min = mins_to_close

        # ================= BUILD DICT ================= #
        feature_dict = {
            "ema20": ema20,
            "ema50": ema50,
            "macd": macd,
            "returns": returns,
            "volatility": volatility,
            "rsi": rsi,
            "atr": atr,
            "trend_strength": trend_strength,
            "mins_since_open": mins_since_open,
            "mins_to_close": mins_to_close,
            "moneyness": moneyness,
            "time_to_expiry_min": time_to_expiry_min,
        }

        # 🔥 CRITICAL: FILTER TO MODEL FEATURES ONLY
        required = self.predictor.ce_features

        final_features = {}

        for f in required:
            if f in feature_dict:
                final_features[f] = feature_dict[f]
            else:
                # fallback minimal safe value
                final_features[f] = 0.0

        return final_features

    # ================= ENTRY ================= #

    def check_entry(self, candles, highs, lows, ts):

        price = candles[-1]

        features = self.build_features(candles, highs, lows)

        if not features:
            return None

        # 🔥 DEBUG HERE
        print(f"[FEATURE CHECK] built={len(features)} required={len(self.predictor.ce_features)}")

        ce_prob = self.predictor.predict(features, "CE")

        print(f"[ML DEBUG] prob={ce_prob}")
        if ce_prob is None:
            return None

        # threshold (ENV controlled)
        try:
            threshold = float(os.getenv("CHAMPION_THRESHOLD", 0.55))
        except:
            threshold = 0.55

        # ORB breakout check
        breakout = False
        if self.orb_high is not None and price > self.orb_high:
            breakout = True

        # ================= ENTRY LOGIC ================= #

        if ce_prob >= threshold:
            reason = "ML_STRONG"

        elif breakout and ce_prob >= (threshold - 0.05):
            reason = "ORB+ML"

        else:
            print(f"[REJECTED] prob={ce_prob} breakout={breakout}")
            return None

        print(f"[ENTRY SIGNAL] {reason} prob={ce_prob}")

        return {
            "side": "CE",
            "ml_prob": ce_prob,
            "features": features,
            "reason": reason
        }

    # ================= EXIT ================= #

    def check_exit(self, position, ltp, held_seconds):

        entry = position["entry"]

        if ltp <= entry * 0.9:
            return True, "STOP"

        if ltp >= entry * 1.05:
            return True, "TARGET"

        if held_seconds > 300:
            return True, "TIME_EXIT"

        return False, None

    # ================= STEP ================= #

    def step(self, market_data):

        candles = market_data["candles"]
        highs = market_data["highs"]
        lows = market_data["lows"]

        ts = datetime.now()

        self.update_orb(candles, ts)

        return self.check_entry(candles, highs, lows, ts)