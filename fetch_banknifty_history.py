import os
import sys
import datetime as dt
import time
import inspect

try:
    # import your repo functions/objects
    from master_runner import init_broker, update_historical_data, HIST_CSV
except Exception as e:
    print("ERROR: cannot import master_runner helpers:", e)
    sys.exit(2)

b = init_broker()

# target CSV and range
target_csv = HIST_CSV
years = 10
total_days = int(years * 365)
chunk_days = 60   # broker / helper enforced 60-day max
end_date = dt.date.today()
start_target = end_date - dt.timedelta(days=total_days)

print(f"Writing to: {target_csv}")
print(f"Fetching history from {start_target} → {end_date} in {chunk_days}-day chunks")

# helper: attempt to call update_historical_data with flexible signatures
def call_update(broker, csv_path, start_dt, end_dt):
    tried = []
    # try common kwarg names
    for kw in ({"lookback_days": (end_dt - start_dt).days},
               {"start_date": start_dt, "end_date": end_dt},
               {"from_date": start_dt, "to_date": end_dt},
               {"start_dt": start_dt, "end_dt": end_dt},
               {"start": start_dt, "end": end_dt}):
        try:
            print("Trying update_historical_data with kwargs:", kw)
            update_historical_data(broker, csv_path, **kw)
            return True
        except TypeError as te:
            tried.append(str(kw))
            # signature mismatch — keep trying other kw patterns
        except Exception as ex:
            print("update_historical_data failed:", ex)
            return False
    # try calling with plain lookback_days (fallback)
    try:
        print("Falling back to update_historical_data(broker, csv, lookback_days=60)")
        update_historical_data(broker, csv_path, lookback_days=chunk_days)
        return True
    except Exception as e:
        print("Fallback update_historical_data failed:", e)
        return False

# If update_historical_data can't fetch specific ranges, try broker-level methods.
def broker_fetch_fallback(broker, token, start_dt, end_dt):
    # common method names to try (adaptable)
    method_names = [
        "historical", "get_historical", "historical_data", "fetch_historical",
        "fetch_ohlc", "get_ohlc", "historical_ohlc", "get_history"
    ]
    for name in method_names:
        meth = getattr(broker, name, None)
        if callable(meth):
            try:
                print(f"Using broker.{name} to fetch {start_dt} → {end_dt}")
                # try a couple of common call signatures
                try:
                    data = meth(start_dt, end_dt, interval="1m")
                except TypeError:
                    data = meth(token, start_dt, end_dt, "minute")
                # dump to CSV (append)
                import pandas as pd
                df = pd.DataFrame(data)
                if df.empty:
                    print("broker method returned no rows for this chunk.")
                    return True
                df.to_csv(target_csv, mode="a", header=not os.path.exists(target_csv), index=False)
                print(f"Appended {len(df)} rows from broker.{name}")
                return True
            except Exception as ex:
                print(f"broker.{name} failed: {ex}")
                # try next
    return False

# If you know the token, put it here (optional). The script will try to discover token if not set.
token = None
if hasattr(sys.modules.get('master_runner'), 'INSTRUMENT_TOKEN'):
    token = getattr(sys.modules['master_runner'], 'INSTRUMENT_TOKEN')
# fallback tokens printed by your live run (example: 260105). Set it manually if you have it:
# token = 260105

# main loop: iterate from end_date backwards in chunks
cursor_end = end_date
cursor_start = max(start_target, cursor_end - dt.timedelta(days=chunk_days-1))

while cursor_end > start_target:
    print(f"\n=== chunk {cursor_start} → {cursor_end} ===")
    ok = call_update(b, target_csv, cursor_start, cursor_end)
    if not ok:
        print("update_historical_data could not fetch this chunk. Trying broker fallback...")
        if token is None:
            print("No token known; broker fallback will try autodiscovery; if that fails, set token manually in the script.")
        ok2 = broker_fetch_fallback(b, token, cursor_start, cursor_end)
        if not ok2:
            print("Both update_historical_data and broker-level fallback failed for chunk. Aborting.")
            sys.exit(3)
    else:
        print("Chunk fetched successfully.")

    # step window back
    cursor_end = cursor_start - dt.timedelta(days=1)
    cursor_start = max(start_target, cursor_end - dt.timedelta(days=chunk_days-1))
    # small pause to avoid rate limits
    time.sleep(1)

print("\nDONE: attempted full fetch. Verify CSV at:", target_csv)

