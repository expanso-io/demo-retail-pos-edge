#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""The demo's data source, driven from outside the pipeline.

    uv run -s scripts/producer.py --rate 2           # records per second
    uv run -s scripts/producer.py --rate 5 --count 50

Posts JSON records to the pipeline's http_server input. Replace the record
body with the demo's real data (files, sensor frames, log lines) and keep it
external: the pipeline should never generate the data it processes.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

URL = os.environ.get("DEMO_INGEST_URL", "http://127.0.0.1:18100/ingest")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=2.0)
    ap.add_argument("--count", type=int, default=0, help="0 = until stopped")
    args = ap.parse_args()
    seq = 0
    while not args.count or seq < args.count:
        seq += 1
        body = json.dumps({"seq": seq, "source": "producer", "ts": time.time()}).encode()
        req = urllib.request.Request(URL, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        time.sleep(1 / args.rate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
