"""Save one Citi Bike GBFS feed snapshot to S3, exactly as served (gzipped).

Invoked by EventBridge with {"feed": "station_status"} or {"feed": "station_information"}.
Key: raw/<feed>/dt=YYYY-MM-DD/<feed>_YYYYMMDDTHHMMSSZ.json.gz, stamped with the feed's own
last_updated time so a retried or duplicate run overwrites the same key instead of adding one.
"""
import gzip
import json
import os
import urllib.request
from datetime import datetime, timezone

import boto3

BASE = "https://gbfs.lyft.com/gbfs/2.3/bkn/en"
FEEDS = {"station_status", "station_information"}
s3 = boto3.client("s3")


def handler(event, _context):
    feed = event.get("feed", "station_status")
    if feed not in FEEDS:
        raise ValueError(f"unknown feed {feed!r}")
    body = urllib.request.urlopen(f"{BASE}/{feed}.json", timeout=20).read()
    updated = datetime.fromtimestamp(json.loads(body)["last_updated"], timezone.utc)
    key = f"raw/{feed}/dt={updated:%Y-%m-%d}/{feed}_{updated:%Y%m%dT%H%M%SZ}.json.gz"
    s3.put_object(Bucket=os.environ["BUCKET"], Key=key, Body=gzip.compress(body),
                  ContentType="application/gzip")  # no Content-Encoding: readers get the .gz as stored
    return {"key": key, "bytes": len(body)}
