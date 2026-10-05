#!/usr/bin/env python3
"""Stream CSV in bounded batches; delete eligibility requires complete durable ACK."""

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
from telemetry_protocol import normalize_sample, csv_text

DEFAULT_SERVER = os.environ.get("UNIMOTORS_SERVER_HTTP", "http://10.10.0.9:8090")
DEFAULT_TOKEN = os.environ.get("UNIMOTORS_TOKEN", "change-me")
LOG_DIR = os.environ.get("UNIMOTORS_LOG_DIR", os.path.expanduser("~/gps_logs"))


def session_from_filename(path):
    return Path(path).stem.removeprefix("telem_").removeprefix("gps_")


def _upload_one(
    path, server=DEFAULT_SERVER, token=DEFAULT_TOKEN, session=None, vehicle=None
):
    path = Path(path)
    if not path.exists():
        return False
    before = path.stat()
    batch = []
    count = 0
    ok = True

    def send(rows):
        if not rows:
            return True
        data = csv_text(rows).encode("utf8")
        req = urllib.request.Request(
            server.rstrip("/") + "/upload",
            data=data,
            method="POST",
            headers={"Content-Type": "text/csv; charset=utf-8", "X-Auth-Token": token},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                reply = json.load(response)
            return not reply.get("rejected") and set(reply.get("accepted", [])) == {
                r["sample_id"] for r in rows
            }
        except (OSError, ValueError, urllib.error.URLError):
            return False

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        if not reader.fieldnames or "ts" not in reader.fieldnames:
            return False
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            return False
        for row in reader:
            try:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError("partial row")
                if vehicle:
                    if row.get("vehicle") and row["vehicle"] != vehicle:
                        raise ValueError("vehicle mismatch")
                    row["vehicle"] = vehicle
                if session:
                    if row.get("sample_id") and row.get("session") != session:
                        raise ValueError("immutable v7 session")
                    row["session"] = session
                if not row.get("session"):
                    row["session"] = session_from_filename(path)
                record = normalize_sample(row)
                batch.append(record)
                count += 1
            except (ValueError, TypeError):
                ok = False
                continue
            if len(batch) >= 100:
                if not send(batch):
                    return False
                batch = []
        if not send(batch):
            return False
    after = path.stat()
    # Active files are never marked fully acknowledged while the collector writes.
    if (
        ok
        and count
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and not Path(str(path) + ".active").exists()
    ):
        marker = Path(str(path) + ".acked")
        temp = Path(str(marker) + ".tmp")
        with temp.open("w", encoding="utf8") as handle:
            json.dump(
                {"bytes": after.st_size, "mtime_ns": after.st_mtime_ns, "rows": count},
                handle,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, marker)
    return ok and count > 0


def upload_one(
    path, server=DEFAULT_SERVER, token=DEFAULT_TOKEN, session=None, vehicle=None
):
    try:
        return _upload_one(path, server, token, session, vehicle)
    except (OSError, csv.Error, UnicodeError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description="UNIMOTORS full telemetry backfill")
    parser.add_argument("csv", nargs="?")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--session")
    parser.add_argument("--vehicle")
    args = parser.parse_args()
    paths = (
        sorted(Path(LOG_DIR).glob("telem_*.csv"))
        if args.all
        else ([Path(args.csv)] if args.csv else [])
    )
    if not paths:
        parser.print_help()
        return 1
    results = [
        upload_one(p, args.server, args.token, args.session, args.vehicle)
        for p in paths
    ]
    print("Backfill acknowledged:", sum(results), "/", len(results))
    return 0 if all(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
