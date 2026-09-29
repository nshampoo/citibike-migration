"""Poll station_status every INTERVAL seconds and save gzipped snapshots locally.

Used to measure what coarser snapshot intervals would miss before we pick one for AWS.
Usage: python3 collector/poll_local.py [minutes_to_run] [interval_seconds]
"""
import gzip, json, pathlib, sys, time, urllib.request
from datetime import datetime, timezone

URL = "https://gbfs.lyft.com/gbfs/2.3/bkn/en/station_status.json"
OUT = pathlib.Path(__file__).resolve().parent.parent / "data" / "live"

def main(minutes: int, interval: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    end = time.time() + minutes * 60
    while time.time() < end:
        started = time.time()
        try:
            body = urllib.request.urlopen(URL, timeout=20).read()
            updated = json.loads(body)["last_updated"]
            stamp = datetime.fromtimestamp(updated, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (OUT / f"station_status_{stamp}.json.gz").write_bytes(gzip.compress(body))
            print(stamp, flush=True)
        except Exception as e:  # keep polling through transient errors
            print("error", e, flush=True)
        time.sleep(max(0, interval - (time.time() - started)))

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 240, int(sys.argv[2]) if len(sys.argv) > 2 else 60)
