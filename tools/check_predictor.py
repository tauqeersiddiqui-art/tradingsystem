#!/usr/bin/env python3
"""
Tool to check predictor thresholds and raw model probabilities.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.predictor_champion import ChampionPredictor

def main():
    print("=== CHECKING PREDICTOR THRESHOLDS ===")
    p = ChampionPredictor()
    print(f"CE Thr: {p.ce_threshold}")
    print(f"PE Thr: {p.pe_threshold}")

    # Try to load a few sample candles via CandleBuilder
    try:
        from engine.data.candle_builder import CandleBuilder
        from engine.config.config import INDEX_TOKEN
        # Create a CandleBuilder with a dummy broker (we won't use live ticks)
        class DummyBroker:
            def __init__(self):
                self._last_ticks = {}
            @property
            def kite(self):
                # This property is used in ltp fallback, but we won't call it in seed_from_csv
                # So we can return a dummy object or just return None and handle in ltp if needed.
                # However, seed_from_csv does not call ltp, so we are safe.
                class DummyKite:
                    def ltp(self, sym):
                        return {}
                return DummyKite()
        broker = DummyBroker()
        cb = CandleBuilder(broker=broker, instrument_token=INDEX_TOKEN, max_candles    =200)
        # Seed from CSV to get some data
        cb.seed_from_csv('data/NIFTY_minute.csv', n=10)  # adjust path if needed
        sample = True
    except Exception as e:
        # If the above fails, try to use the HIST_CSV from config
        try:
            from engine.data.candle_builder import CandleBuilder
            from engine.config.config import INDEX_TOKEN, HIST_CSV
            class DummyBroker:
                def __init__(self):
                    self._last_ticks = {}
                @property
                def kite(self):
                    class DummyKite:
                        def ltp(self, sym):
                            return {}
                    return DummyKite()
            broker = DummyBroker()
            cb = CandleBuilder(broker=broker, instrument_token=INDEX_TOKEN, max_candles=200)
            cb.seed_from_csv(HIST_CSV, n=10)
            sample = True
        except Exception as e2:
            print(f"Could not load sample candles: {e2}")
            sample = False

    print(f"SAMPLE LOADED: {bool(sample)}")

if __name__ == "__main__":
    main()