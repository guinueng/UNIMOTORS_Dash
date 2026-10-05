"""Versioned telemetry identity and CSV parsing; shared by vehicle and server."""

import csv
import hashlib
import io
import json
import math

VERSION = "7.0.0"
LEGACY_FIELDS = [
    "ts",
    "vehicle",
    "session",
    "t_mono",
    "lat",
    "lon",
    "alt",
    "speed",
    "heading",
    "sats",
    "fix",
    "hdop",
    "g_lon",
    "g_lat",
    "voltage",
    "current",
    "soc",
    "power_w",
    "regen",
    "remain_ah",
    "range_km",
    "state",
    "used_ah",
    "used_wh",
    "temp_max",
    "temp_min",
    "cell_v_max",
    "cell_v_min",
    "cell_v_diff",
    "balancing",
    "alarm_level",
    "alarms",
]
EXTRA_FIELDS = [
    "sample_id",
    "race_session_id",
    "segment_id",
    "sequence",
    "boot_id",
    "schema_version",
    "software_version",
    "time_quality",
    "gps_age",
    "bms_age",
    "distance_km",
    "lap_count",
    "energy_increment_wh",
    "charge_increment_ah",
    "gap_seconds",
    "phase",
    "driver_id",
    "recovery_parent",
    "replay",
    "meta",
]
FIELDS = LEGACY_FIELDS + EXTRA_FIELDS
TEXT_FIELDS = {
    "ts",
    "vehicle",
    "session",
    "state",
    "alarms",
    "sample_id",
    "race_session_id",
    "segment_id",
    "boot_id",
    "software_version",
    "time_quality",
    "phase",
    "driver_id",
    "recovery_parent",
    "meta",
}


def finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_sample(record):
    if not isinstance(record, dict):
        raise ValueError("sample must be an object")
    out = {}
    for key in FIELDS:
        value = record.get(key)
        if key in TEXT_FIELDS:
            if isinstance(value, (dict, list)):
                value = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                )
            out[key] = str(value) if value not in (None, "") else None
            if out[key] is not None and len(out[key]) > (
                32768 if key == "meta" else 2048
            ):
                raise ValueError("field too long: " + key)
        else:
            if isinstance(value, bool):
                value = int(value)
            if value in ("", None):
                out[key] = None
            else:
                out[key] = finite(value)
                if out[key] is None:
                    raise ValueError("non-finite number: " + key)
    if not out.get("vehicle") or not out.get("ts"):
        raise ValueError("vehicle and ts are required")
    if not out.get("sample_id"):
        # Compatibility only. v7 IDs never depend on the wall clock.
        identity = json.dumps(
            [out["vehicle"], out["session"], out["ts"]], ensure_ascii=False
        )
        out["sample_id"] = "legacy-" + hashlib.sha256(identity.encode()).hexdigest()
    for key in ("vehicle", "session", "sample_id", "race_session_id", "segment_id"):
        if out.get(key) and len(out[key]) > 160:
            raise ValueError("identifier too long: " + key)
    return out


def parse_csv(text, vehicle=None, session=None):
    reader = csv.DictReader(io.StringIO(text), strict=True)
    if not reader.fieldnames or "ts" not in reader.fieldnames:
        raise ValueError("CSV header must contain ts")
    if len(set(reader.fieldnames)) != len(reader.fieldnames):
        raise ValueError("duplicate CSV headers")
    accepted, rejected = [], []
    try:
        for line, row in enumerate(reader, 2):
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("incomplete row")
                if vehicle:
                    if row.get("vehicle") and row["vehicle"] != vehicle:
                        raise ValueError("vehicle mismatch")
                    row["vehicle"] = vehicle
                if session:
                    if row.get("sample_id") and row.get("session") != session:
                        raise ValueError("v7 session is immutable")
                    row["session"] = session
                accepted.append(normalize_sample(row))
            except ValueError as exc:
                rejected.append({"line": line, "error": str(exc)})
    except csv.Error as exc:
        rejected.append({"line": reader.line_num, "error": str(exc)})
    return accepted, rejected


def csv_text(records):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow(record)
    return buffer.getvalue()
