# core/data_manager.py
# FINAL PRODUCTION STABLE VERSION (UPDATED)
# Incremental rolling historical updater
# Zerodha 60-day limit safe
# Auto-detect datetime column
# + Feature window extraction added

import os
import pandas as pd
from datetime import datetime, timedelta


HIST_PATH = "data/historical/nifty_1m_full.csv"
INSTRUMENT_TOKEN = 256265  # NIFTY 50
MAX_DAYS_PER_CALL = 60


class DataManager:

    def __init__(self, broker):
        self.broker = broker
        self.kite = broker.kite

    # ---------------- DETECT DATETIME COLUMN ---------------- #

    def detect_datetime_column(self, df):

        possible_cols = [
            "date", "Date",
            "datetime", "Datetime",
            "timestamp", "Timestamp",
            "time", "Time"
        ]

        for col in possible_cols:
            if col in df.columns:
                return col

        return None

    # ---------------- GET LAST TIMESTAMP ---------------- #

    def get_last_timestamp(self):

        if not os.path.exists(HIST_PATH):
            print("[DATA] Historical file not found.")
            return None

        df = pd.read_csv(HIST_PATH)

        if df.empty:
            print("[DATA] Historical file is empty.")
            return None

        date_col = self.detect_datetime_column(df)

        if not date_col:
            print("[DATA] No valid datetime column found.")
            return None

        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")

        last_ts = df[date_col].max()

        if pd.isna(last_ts):
            return None

        # ensure timezone-naive
        if last_ts.tzinfo is not None:
            last_ts = last_ts.tz_convert(None)

        print(f"[DATA] Last candle in CSV: {last_ts}")

        return last_ts

    # ---------------- FETCH CHUNK ---------------- #

    def fetch_chunk(self, start_dt, end_dt):

        if start_dt >= end_dt:
            return pd.DataFrame()

        try:
            data = self.kite.historical_data(
                INSTRUMENT_TOKEN,
                start_dt,
                end_dt,
                "minute"
            )

            if not data:
                return pd.DataFrame()

            df = pd.DataFrame(data)
            df["date"] = pd.to_datetime(df["date"], errors="coerce")

            # Ensure timezone-naive
            if df["date"].dt.tz is not None:
                df["date"] = df["date"].dt.tz_convert(None)

            return df

        except Exception as e:
            print(f"[DATA CHUNK ERROR] {e}")
            return pd.DataFrame()

    # ---------------- MAIN UPDATE ---------------- #

    def update_historical(self):

        print("[DATA] Checking historical dataset...")

        last_ts = self.get_last_timestamp()

        today = datetime.now().replace(tzinfo=None).date()
        yesterday = today - timedelta(days=1)

        market_close_yesterday = datetime.combine(
            yesterday,
            datetime.min.time()
        ).replace(hour=10, minute=0)  # 10:00 UTC = 15:30 IST

        market_close_yesterday = market_close_yesterday.replace(tzinfo=None)

        # ---- Determine start ---- #

        if last_ts:
            start_dt = last_ts + timedelta(minutes=1)
        else:
            print("[DATA] No previous data found. Starting full backfill.")
            start_dt = datetime(2015, 1, 1, 9, 15)

        if start_dt >= market_close_yesterday:
            print("[DATA] Dataset already up to date.")
            return

        print(f"[DATA] Updating from {start_dt} to {market_close_yesterday}")

        all_chunks = []
        current_start = start_dt

        while current_start < market_close_yesterday:

            chunk_end_dt = min(
                current_start + timedelta(days=MAX_DAYS_PER_CALL),
                market_close_yesterday
            )

            print(
                f"[DATA] Fetching chunk "
                f"{current_start.date()} → {chunk_end_dt.date()}"
            )

            df_chunk = self.fetch_chunk(current_start, chunk_end_dt)

            if not df_chunk.empty:
                all_chunks.append(df_chunk)

            current_start = chunk_end_dt + timedelta(minutes=1)

            if current_start >= market_close_yesterday:
                break

        if not all_chunks:
            print("[DATA] No new data downloaded.")
            return

        df_new = pd.concat(all_chunks)

        # ---- Merge with existing ---- #

        if os.path.exists(HIST_PATH):
            df_old = pd.read_csv(HIST_PATH)

            date_col = self.detect_datetime_column(df_old)

            if date_col:
                df_old[date_col] = pd.to_datetime(df_old[date_col], errors="coerce")

                if df_old[date_col].dt.tz is not None:
                    df_old[date_col] = df_old[date_col].dt.tz_convert(None)

                df_old.rename(columns={date_col: "date"}, inplace=True)
            else:
                print("[DATA] Warning: Could not detect date column in old CSV.")

            df = pd.concat([df_old, df_new])
        else:
            df = df_new

        df.drop_duplicates(subset=["date"], inplace=True)
        df.sort_values("date", inplace=True)

        os.makedirs("data/historical", exist_ok=True)
        df.to_csv(HIST_PATH, index=False, encoding='utf-8')

        print(f"[DATA] Historical updated successfully. Total rows: {len(df)}")

    # ---------------- FEATURE WINDOW (NEW) ---------------- #

    def get_latest_window(self, n=100):
        """
        Returns last N candles for feature generation
        """

        if not os.path.exists(HIST_PATH):
            return None

        df = pd.read_csv(HIST_PATH)

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")

        df = df.dropna()

        return df.tail(n)