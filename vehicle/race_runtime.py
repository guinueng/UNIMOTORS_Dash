"""Local race continuity, valid measurement integration and a durable outbox.

No torque, AIR, BOTS, contactor or motor-control writes exist in this module.
"""

import copy
import json
import math
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

try:
    from telemetry_protocol import (
        VERSION,
        SCHEMA_VERSION,
        CURRENT_CONVENTION,
        finite,
        validate_current_sign,
    )
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
    from telemetry_protocol import (
        VERSION,
        SCHEMA_VERSION,
        CURRENT_CONVENTION,
        finite,
        validate_current_sign,
    )

PHASES = {"READY", "FORMATION", "RACE", "CHANGE", "RESTART", "COOLDOWN", "FINISHED"}
EVENTS = {"practice", "acceleration", "gymkhana", "performance", "endurance"}
DEFAULT_PROFILE = {
    "version": 1,
    "event": "practice",
    "confirmed": False,
    "total_laps": None,
    "formation_counted": False,
    "power_limit_w": 10000.0,
    "voltage_limit_v": 58.0,
    "power_warning_w": None,
    "voltage_warning_v": None,
    "usable_remaining_wh": None,
    "reserve_wh": 0.0,
    "budget_reference_wh": 0.0,
    "budget_reference_gap": 0.0,
    "lap_line": None,
    "sectors": [],
    "direction": 0,
    "lap_min_seconds": 10.0,
    "lap_departure_m": 30.0,
    "gps_max_hdop": 5.0,
    "calibration_id": "uncalibrated",
    "calibration_verified": False,
    "speed_scale": 1.0,
    "speed_offset": 0.0,
    "voltage_scale": 1.0,
    "voltage_offset": 0.0,
    "current_scale": 1.0,
    "current_offset": 0.0,
    "current_sign": -1,
    "source_version": "",
    "approval_evidence": "",
    "course_version": "",
    "crew_limit": None,
    "change_lap_min": None,
    "change_lap_max": None,
    "change_allowance_s": 180.0,
    "restart_allowance_s": 120.0,
    "race_time_limit_s": None,
}
MEASUREMENT_KEYS = (
    "voltage_scale",
    "voltage_offset",
    "current_scale",
    "current_offset",
    "current_sign",
)
DEVICE_KEYS = MEASUREMENT_KEYS + (
    "speed_scale",
    "speed_offset",
    "calibration_id",
    "calibration_verified",
)


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "process-" + uuid.uuid4().hex


def trusted_system_time():
    # A plausible year or a wall-clock jump is NOT proof of synchronization.
    return (
        os.environ.get("UNIMOTORS_TIME_TRUSTED") == "1"
        or Path("/run/systemd/timesync/synchronized").exists()
    )


def distance_m(a, b):
    r = 6371000.0
    p, q = math.radians(a[0]), math.radians(b[0])
    dp, dl = q - p, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p) * math.cos(q) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(min(1.0, math.sqrt(h)))


def validate_profile(changes, current=None):
    profile = copy.deepcopy(current or DEFAULT_PROFILE)
    if not isinstance(changes, dict) or set(changes) - set(DEFAULT_PROFILE):
        raise ValueError("unknown profile field")
    profile.update(changes)
    profile["current_sign"] = validate_current_sign(profile["current_sign"])
    version = finite(profile["version"])
    if version is None or version < 1 or int(version) != version:
        raise ValueError("invalid profile version")
    profile["version"] = int(version)
    if profile["event"] not in EVENTS:
        raise ValueError("unknown event")
    for key in ("confirmed", "formation_counted", "calibration_verified"):
        if not isinstance(profile[key], bool):
            raise ValueError(key + " must be boolean")
    for key, minimum, maximum in (
        ("total_laps", 1, 10000),
        ("power_limit_w", 1, 100000),
        ("voltage_limit_v", 1, 1000),
        ("power_warning_w", 0, 100000),
        ("voltage_warning_v", 0, 1000),
        ("usable_remaining_wh", 0, 100000),
        ("reserve_wh", 0, 100000),
        ("budget_reference_wh", -100000, 100000),
        ("budget_reference_gap", 0, 10000000),
        ("lap_min_seconds", 1, 3600),
        ("lap_departure_m", 1, 1000),
        ("gps_max_hdop", 0.1, 100),
        ("speed_scale", 0.1, 10),
        ("speed_offset", -100, 100),
        ("voltage_scale", 0.1, 10),
        ("voltage_offset", -100, 100),
        ("current_scale", 0.1, 10),
        ("current_offset", -1000, 1000),
        ("crew_limit", 1, 20),
        ("change_lap_min", 0, 10000),
        ("change_lap_max", 0, 10000),
        ("change_allowance_s", 1, 3600),
        ("restart_allowance_s", 1, 3600),
        ("race_time_limit_s", 1, 86400),
    ):
        if profile[key] is None and key in {
            "total_laps",
            "power_warning_w",
            "voltage_warning_v",
            "usable_remaining_wh",
            "crew_limit",
            "change_lap_min",
            "change_lap_max",
            "race_time_limit_s",
        }:
            continue
        value = finite(profile[key])
        if value is None or not minimum <= value <= maximum:
            raise ValueError("invalid " + key)
        if key in {"total_laps", "crew_limit", "change_lap_min", "change_lap_max"}:
            if int(value) != value:
                raise ValueError(key + " must be an integer")
            profile[key] = int(value)
        else:
            profile[key] = value
    if profile["direction"] not in (-1, 0, 1):
        raise ValueError("direction must be -1, 0 or 1")
    if len(str(profile["calibration_id"])) > 100:
        raise ValueError("calibration id too long")
    for key in ("source_version", "approval_evidence", "course_version"):
        if not isinstance(profile[key], str) or len(profile[key]) > 500:
            raise ValueError("invalid " + key)
    if not isinstance(profile["sectors"], list) or len(profile["sectors"]) > 10:
        raise ValueError("at most 10 sectors")
    lines = ([profile["lap_line"]] if profile["lap_line"] else []) + profile["sectors"]
    for line in lines:
        if not isinstance(line, list) or len(line) != 2:
            raise ValueError("line must be two [lat,lon] points")
        for point in line:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("invalid point")
            if (
                finite(point[0]) is None
                or finite(point[1]) is None
                or not (-90 <= float(point[0]) <= 90 and -180 <= float(point[1]) <= 180)
            ):
                raise ValueError("invalid coordinates")
        if distance_m(line[0], line[1]) < 1:
            raise ValueError("gate must be at least 1m wide")
    return profile


def validate_saved_run(value):
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise ValueError("invalid saved run")
    if value.get("phase") not in PHASES:
        raise ValueError("invalid saved phase")
    for key in ("used_wh", "used_ah", "distance_km", "gap_seconds", "lap_count"):
        number = finite(value.get(key))
        if number is None or (
            key in {"distance_km", "gap_seconds", "lap_count"} and number < 0
        ):
            raise ValueError("invalid saved " + key)
    if not isinstance(value.get("laps"), list) or not isinstance(
        value.get("sectors"), list
    ):
        raise ValueError("invalid saved lap records")
    legacy = "current_sign" not in value.get("profile", {})
    if not isinstance(value.get("energy_reasons", []), list):
        raise ValueError("invalid saved energy reasons")
    value["profile"] = validate_profile(value.get("profile", {}))
    # v7 integrated native current as discharge-positive. Never retroactively flip it.
    if legacy:
        value["profile"]["current_sign"] = 1
        value.setdefault("energy_reasons", []).append("legacy_polarity_unverified")
    value.setdefault("energy_reasons", [])
    value.setdefault("energy_revision", 0)
    value.setdefault("measured_seconds", 0.0)
    value.setdefault("budget_revision", None)
    if not isinstance(value.get("last_faults", {}), dict):
        raise ValueError("invalid saved fault history")
    for key in ("measured_seconds", "energy_revision"):
        number = finite(value[key])
        if (
            number is None
            or number < 0
            or (key == "energy_revision" and int(number) != number)
        ):
            raise ValueError("invalid saved " + key)
    if not isinstance(value["energy_reasons"], list) or any(
        not isinstance(x, str) for x in value["energy_reasons"]
    ):
        raise ValueError("invalid saved energy reasons")
    return value


class Gate:
    def __init__(self, line, direction=0, departure=30, minimum=10):
        self.line, self.direction = line, direction
        self.departure, self.minimum = departure, minimum
        self.previous = None
        self.last_cross = None
        self.travel = 0.0
        self.side_anchor = None

    def reset_segment(self):
        self.previous = None
        self.last_cross = None
        self.travel = 0.0
        self.side_anchor = None

    def update(self, point, mono):
        previous = self.previous
        self.previous = (point, mono)
        if not previous or not self.line:
            return False
        a, t = previous
        moved = distance_m(a, point)
        if mono <= t or moved > (mono - t) * 70 + 5:  # plausibility, not a rule limit
            self.reset_segment()
            self.previous = (point, mono)
            return False
        u, v = self.line

        def cross(x, y, z):
            return (y[1] - x[1]) * (z[0] - x[0]) - (y[0] - x[0]) * (z[1] - x[1])

        # Departure is geometric, not accumulated GPS jitter while idling.
        scale = math.cos(math.radians((u[0] + v[0]) / 2))
        dx, dy = (v[1] - u[1]) * scale, v[0] - u[0]
        px, py = (point[1] - u[1]) * scale, point[0] - u[0]
        projection = max(0, min(1, (px * dx + py * dy) / (dx * dx + dy * dy)))
        separation = math.hypot(px - projection * dx, py - projection * dy) * 111195
        self.travel = max(self.travel, separation)
        if self.side_anchor is None and cross(u, v, a) != 0:
            self.side_anchor = a
        anchor = self.side_anchor or a
        s0, s1 = cross(u, v, anchor), cross(u, v, point)
        intersects = s0 * s1 < 0 and cross(a, point, u) * cross(a, point, v) <= 0
        if cross(u, v, a) == 0:
            intersects = (
                s0 * s1 < 0 and cross(anchor, point, u) * cross(anchor, point, v) <= 0
            )
        if s1 != 0:
            self.side_anchor = point
        oriented = not self.direction or (s1 - s0) * self.direction > 0
        ready = self.last_cross is None or (
            mono - self.last_cross >= self.minimum and self.travel >= self.departure
        )
        if intersects and oriented and ready:
            self.last_cross, self.travel = mono, 0.0
            return True
        return False


class RaceRuntime:
    def __init__(
        self,
        directory,
        vehicle="car1",
        battery_epoch=None,
        *,
        clock=time,
        boot=None,
        time_trusted=None,
        resume_window=300,
        gps_ttl=1.5,
        bms_ttl=1.5,
        slow_ttl=5.0,
        persist_interval=1.0,
        current_sign=None,
    ):
        self.directory = Path(directory)
        self.vehicle, self.battery_epoch = vehicle, battery_epoch
        self.clock, self.boot = clock, boot or boot_id()
        self.time_trusted = time_trusted or trusted_system_time
        self.resume_window = resume_window
        self.gps_ttl, self.bms_ttl, self.slow_ttl = gps_ttl, bms_ttl, slow_ttl
        self.persist_interval = persist_interval
        self.device_profile = copy.deepcopy(DEFAULT_PROFILE)
        self.device_profile["current_sign"] = validate_current_sign(
            current_sign
            if current_sign is not None
            else os.environ.get("UNIMOTORS_CURRENT_SIGN", "-1")
        )
        self.device_drivers = {}
        self.device_driver_id = "driver1"
        self.device_last_faults = {}
        self.lock = threading.RLock()
        self.durable = True
        self.open_error = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(
                self.directory / "race_state.sqlite3", check_same_thread=False
            )
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        except (sqlite3.Error, OSError) as exc:
            self.open_error = "state database unavailable: " + str(exc)[:120]
            try:
                self.db.close()
            except (AttributeError, sqlite3.Error):
                pass
            self.db = sqlite3.connect(":memory:", check_same_thread=False)
            self.durable = False
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, vehicle TEXT,
                state TEXT, saved_wall REAL, saved_mono REAL, boot TEXT, trusted INTEGER);
            CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY, payload TEXT,
                acked INTEGER DEFAULT 0, created REAL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, payload TEXT, status TEXT);
        """)
        if "acked" not in {r[1] for r in self.db.execute("PRAGMA table_info(events)")}:
            self.db.execute("ALTER TABLE events ADD COLUMN acked INTEGER DEFAULT 0")
        self.db.commit()
        self.db.execute("UPDATE commands SET status='history' WHERE status='received'")
        self.db.commit()
        row = self.db.execute(
            "SELECT id,state,saved_wall,saved_mono,boot,trusted FROM runs "
            "WHERE vehicle=? ORDER BY rowid DESC LIMIT 1",
            (vehicle,),
        ).fetchone()
        self.segment_parent = None
        self.candidate = None
        if row:
            try:
                stored = json.loads(row[1])
                had_polarity = "current_sign" in stored.get("profile", {})
                old = validate_saved_run(stored)
                for key in DEVICE_KEYS:
                    if key != "current_sign" or had_polarity:
                        self.device_profile[key] = old["profile"][key]
                self.device_drivers = copy.deepcopy(old.get("drivers", {}))
                self.device_driver_id = old.get("driver_id", "driver1")
                self.device_last_faults = copy.deepcopy(old.get("last_faults", {}))
                self.segment_parent = old.get("_last_segment_id")
                embedded = old.pop("_candidate", None)
                if embedded and old["phase"] != "FINISHED":
                    base = validate_saved_run(embedded["state"])
                    base["used_wh"] += old.get("used_wh", 0.0)
                    base["used_ah"] += old.get("used_ah", 0.0)
                    base["distance_km"] += old.get("distance_km", 0.0)
                    base["measured_seconds"] += old.get("measured_seconds", 0.0)
                    base["energy_reasons"] = sorted(
                        set(base["energy_reasons"] + old["energy_reasons"])
                    )
                    if any(
                        base["profile"][k] != old["profile"][k]
                        for k in MEASUREMENT_KEYS
                    ):
                        base["energy_reasons"].append("mixed_measurement_basis")
                    base["external_used_wh"] = base.get(
                        "external_used_wh", 0.0
                    ) + old.get("external_used_wh", 0.0)
                    old = base
                    embedded.setdefault("aliases", []).append(row[0])
                    row = (
                        embedded["id"],
                        None,
                        embedded["wall"],
                        embedded["mono"],
                        embedded["boot"],
                        int(embedded["trusted"]),
                    )
                if old.get("phase") != "FINISHED":
                    self.candidate = dict(
                        id=row[0],
                        state=old,
                        wall=row[2],
                        mono=row[3],
                        boot=row[4],
                        trusted=bool(row[5]),
                        aliases=embedded.get("aliases", []) if embedded else [],
                    )
            except (ValueError, TypeError, KeyError):
                self.open_error = "saved state invalid; previous record preserved"
        self.segment = uuid.uuid4().hex
        self.sequence = 0
        self.run = self._new_run()
        self.recovery = "candidate" if self.candidate else "new"
        self.gps, self.bms, self.bms_times = {}, {}, {}
        self.gps_time = None
        self.vi_previous = None
        self.vi_break_from = None
        self.position_previous = None
        self.lap_started = None
        self.lap_start_wh = None
        self.lap_energy_start = None
        self.interrupted_lap = bool(self.candidate)
        self.gates = []
        self.pending = []
        self.events_pending = []
        self.last_save = None
        self.last_save_mono = 0.0
        self.storage_error = self.open_error
        self.replay = False
        self.sector_previous = None
        self.csv_error = None
        self.started_mono = self.clock.monotonic()
        self.started_cpu = time.process_time()
        self.deadline_misses = 0
        self.last_persist_ms = 0.0
        self.pit_received_mono = None
        self.latest = None
        self.last_tick = self.clock.monotonic()
        self.last_tick_wall = self.clock.time()
        self.staging_used = {"wh": 0.0, "ah": 0.0}
        self.increment_wh = self.increment_ah = 0.0
        self.dropped_samples = 0
        self.trends = deque(maxlen=120)
        self.input_issues = []
        self.external = {}
        self.external_previous = None
        self.raw_error = None
        self.raw_dropped = 0
        self.fault_fingerprint = None
        self.event("boot", {"boot_id": self.boot, "candidate": bool(self.candidate)})
        self.try_auto_resume()

    def _new_run(self):
        return dict(
            id=uuid.uuid4().hex,
            phase="READY",
            used_wh=0.0,
            used_ah=0.0,
            distance_km=0.0,
            lap_count=0,
            laps=[],
            sectors=[],
            gap_seconds=0.0,
            lap_uncertain=False,
            profile=copy.deepcopy(self.device_profile),
            driver_id=self.device_driver_id,
            drivers=copy.deepcopy(self.device_drivers),
            energy_reasons=[],
            energy_revision=0,
            measured_seconds=0.0,
            budget_revision=None,
            last_faults=copy.deepcopy(self.device_last_faults),
            battery_epoch=self.battery_epoch,
            parent=None,
            change_started_utc=None,
            change_elapsed=0.0,
        )

    def event(self, kind, detail=None):
        event = dict(
            id=uuid.uuid4().hex,
            vehicle=self.vehicle,
            kind=kind,
            detail=detail or {},
            ts=self.clock.time(),
            segment_id=self.segment,
            race_session_id=self.run["id"],
        )
        with self.lock:
            self.events_pending.append(event)
            if len(self.events_pending) > 1000:
                self.events_pending.pop(0)
                self.storage_error = "event buffer overflow"
        return event

    def _resume_age(self):
        c = self.candidate
        if not c:
            return None
        if c["boot"] == self.boot:
            return self.clock.monotonic() - c["mono"]
        if c["trusted"] and self.time_trusted():
            return self.clock.time() - c["wall"]
        return None

    def try_auto_resume(self):
        with self.lock:
            if self.recovery == "battery_check_required":
                return
            age = self._resume_age()
            if (
                self.candidate
                and age is not None
                and 0 <= age <= self.resume_window
                and self.battery_epoch
                and self.battery_epoch == self.candidate["state"].get("battery_epoch")
                and not self.candidate["state"].get("energy_reasons")
                and all(
                    self.run["profile"][k] == self.candidate["state"]["profile"][k]
                    for k in MEASUREMENT_KEYS
                )
            ):
                self.resume(confirm_same_battery=True, automatic=True)

    def resume(self, confirm_same_battery=False, automatic=False):
        with self.lock:
            if not self.candidate or not confirm_same_battery:
                raise ValueError("confirm the same battery and race")
            if (
                self.battery_epoch
                and self.candidate["state"].get("battery_epoch")
                and self.battery_epoch != self.candidate["state"]["battery_epoch"]
            ):
                raise ValueError("battery epoch changed; start a new race")
            age = self._resume_age()
            if automatic and (age is None or not 0 <= age <= self.resume_window):
                raise ValueError("automatic resume time is not verified")
            staged = copy.deepcopy(self.run)
            old = copy.deepcopy(self.candidate["state"])
            old["used_wh"] += staged["used_wh"]
            old["used_ah"] += staged["used_ah"]
            old["distance_km"] += staged["distance_km"]
            old["measured_seconds"] += staged["measured_seconds"]
            old["energy_reasons"] = sorted(
                set(old["energy_reasons"] + staged["energy_reasons"])
            )
            if self.fault_fingerprint is not None:
                old["last_faults"] = copy.deepcopy(staged["last_faults"])
            if any(old["profile"][k] != staged["profile"][k] for k in MEASUREMENT_KEYS):
                old["energy_reasons"].append("mixed_measurement_basis")
                for key in DEVICE_KEYS:
                    old["profile"][key] = staged["profile"][key]
                old["energy_revision"] = (
                    max(old["energy_revision"], staged["energy_revision"]) + 1
                )
                old["budget_revision"] = None
            old["external_used_wh"] = old.get("external_used_wh", 0.0) + staged.get(
                "external_used_wh", 0.0
            )
            old["lap_uncertain"] = True
            old["gap_seconds"] += max(0.0, age or 0.0)
            self.run = old
            self.battery_epoch = old.get("battery_epoch")
            self.recovery = "automatic" if automatic else "manual"
            self.event(
                "resume",
                {
                    "from": old["id"],
                    "provisional": staged["id"],
                    "provisional_ids": [staged["id"]]
                    + self.candidate.get("aliases", []),
                    "age": age,
                    "unknown_duration": age is None,
                    "automatic": automatic,
                },
            )
            self.candidate = None
            self.sector_previous = None
            self.vi_previous = None
            self.vi_break_from = None
            self.external_previous = None
            self.position_previous = None
            self.lap_started = None
            self.lap_energy_start = None
            self.interrupted_lap = True
            self.gps, self.bms, self.bms_times, self.external = {}, {}, {}, {}
            self.gps_time, self.fault_fingerprint = None, None
            self._configure_gates()
            self.persist(force=True)

    def new_race(self, battery_epoch=None):
        with self.lock:
            self.candidate = None
            self.tick()
            self.run["phase"] = "FINISHED"
            self.persist(force=True)
            self.candidate = None
            self.battery_epoch = battery_epoch
            for key in DEVICE_KEYS:
                self.device_profile[key] = self.run["profile"][key]
            self.device_drivers = copy.deepcopy(self.run["drivers"])
            self.device_driver_id = self.run["driver_id"]
            self.device_last_faults = copy.deepcopy(self.run.get("last_faults", {}))
            self.run = self._new_run()
            self.segment, self.sequence = uuid.uuid4().hex, 0
            self.segment_parent = None
            self.increment_wh = self.increment_ah = 0.0
            self.vi_previous = self.position_previous = None
            self.vi_break_from = None
            self.external_previous = None
            self.lap_started = self.lap_start_wh = None
            self.lap_energy_start = None
            self.interrupted_lap = False
            self.sector_previous = None
            self.recovery = "new"
            self.gps, self.bms, self.bms_times, self.external = {}, {}, {}, {}
            self.gps_time, self.fault_fingerprint = None, None
            if self.durable:
                self.open_error = None
            self.event("new_race")
            self._configure_gates()
            self.persist(force=True)

    def _configure_gates(self):
        p = self.run["profile"]
        lines = ([p["lap_line"]] if p["lap_line"] else []) + p["sectors"]
        self.gates = [
            Gate(line, p["direction"], p["lap_departure_m"], p["lap_min_seconds"])
            for line in lines
        ]

    def configure(self, changes):
        with self.lock:
            previous_profile = self.run["profile"]
            p = validate_profile(changes, self.run["profile"])
            measurement_changed = any(
                p[k] != previous_profile[k] for k in MEASUREMENT_KEYS
            )
            if measurement_changed:
                # Flush the previous basis into its own immutable capture segment.
                self.tick()
                self.segment_parent = self.segment
                self.segment, self.sequence = uuid.uuid4().hex, 0
                self.run["energy_revision"] += 1
                self.run["budget_revision"] = None
                if self.run["measured_seconds"] > 0:
                    self.run["energy_reasons"].append("mixed_measurement_basis")
                self.vi_previous = None
                self.vi_break_from = None
                for key in (
                    "voltage",
                    "current",
                    "power_w",
                    "regen",
                    "raw_voltage",
                    "raw_current",
                ):
                    self.bms.pop(key, None)
                    self.bms_times.pop(key, None)
                self.event(
                    "measurement_basis_changed",
                    {
                        "before": {k: previous_profile[k] for k in MEASUREMENT_KEYS},
                        "after": {k: p[k] for k in MEASUREMENT_KEYS},
                    },
                )
            if "usable_remaining_wh" in changes:
                p["budget_reference_wh"] = self.run["used_wh"]
                p["budget_reference_gap"] = self.run["gap_seconds"]
                self.run["budget_revision"] = self.run["energy_revision"]
            self.run["profile"] = p
            p["version"] = int(self.run.get("_profile_revision", p["version"])) + 1
            self.run["_profile_revision"] = p["version"]
            if measurement_changed or any(
                p[k] != previous_profile[k]
                for k in (
                    "lap_line",
                    "sectors",
                    "direction",
                    "lap_min_seconds",
                    "lap_departure_m",
                    "gps_max_hdop",
                    "speed_scale",
                    "speed_offset",
                )
            ):
                self._configure_gates()
                self.position_previous = None
                self.lap_started = None
                self.lap_energy_start = None
                self.sector_previous = None
            for key in DEVICE_KEYS:
                self.device_profile[key] = p[key]
            self.event("profile", {"profile": p})
            self.persist(force=True)

    def set_phase(self, phase):
        if phase not in PHASES:
            raise ValueError("unknown phase")
        with self.lock:
            previous = self.run["phase"]
            self.run["phase"] = phase
            if phase == "CHANGE" and previous != phase:
                self.run["change_started_utc"] = (
                    self.clock.time() if self.time_trusted() else None
                )
                self.run["change_started_mono"] = self.clock.monotonic()
                self.run["change_boot"] = self.boot
                self.run["change_elapsed"] = 0.0
            if phase == "RESTART" and previous != phase:
                self.run["restart_started_utc"] = (
                    self.clock.time() if self.time_trusted() else None
                )
                self.run["restart_started_mono"] = self.clock.monotonic()
                self.run["restart_boot"] = self.boot
            if phase == "RACE" and not self.run.get("race_started_mono"):
                self.run["race_started_utc"] = (
                    self.clock.time() if self.time_trusted() else None
                )
                self.run["race_started_mono"] = self.clock.monotonic()
                self.run["race_boot"] = self.boot
            self.lap_started = None
            self.position_previous = None
            self.sector_previous = None
            self._configure_gates()
            self.event("phase", {"from": previous, "to": phase})
            self.persist(force=True)

    def driver(self, driver_id, settings=None):
        if not isinstance(driver_id, str) or not 1 <= len(driver_id) <= 40:
            raise ValueError("invalid driver")
        settings = settings or {}
        if not isinstance(settings, dict):
            raise ValueError("driver settings must be an object")
        if set(settings) - {"mode", "brightness", "contrast"}:
            raise ValueError("unknown driver setting")
        if settings.get("mode", "base") not in {"base", "endurance", "pit"}:
            raise ValueError("unknown mode")
        if not 0.2 <= float(settings.get("brightness", 1)) <= 1:
            raise ValueError("brightness must be .2..1")
        if settings.get("contrast", "normal") not in {"normal", "high"}:
            raise ValueError("contrast must be normal or high")
        with self.lock:
            self.run["driver_id"] = driver_id
            self.run["drivers"][driver_id] = settings
            self.event("driver", {"driver_id": driver_id, "settings": settings})
            self.persist(force=True)

    def correct_laps(self, count):
        if (
            isinstance(count, bool)
            or int(count) != float(count)
            or not 0 <= int(count) <= 10000
        ):
            raise ValueError("invalid lap count")
        with self.lock:
            self.run["lap_count"] = int(count)
            self.run["lap_uncertain"] = False
            self.event("lap_correction", {"count": int(count)})
            self.persist(force=True)

    def update_bms(self, values, received=None, *, canonical=False):
        now = self.clock.monotonic()
        with self.lock:
            p = self.run["profile"]
            accepted_vi = set()
            faults = values.get("faults")
            if isinstance(faults, dict):
                self.run["last_faults"] = copy.deepcopy(faults)
                fingerprint = json.dumps(faults, sort_keys=True, ensure_ascii=False)
                if fingerprint != self.fault_fingerprint:
                    self.event("bms_alarm", {"faults": faults, "fresh": True})
                    self.fault_fingerprint = fingerprint
            for key, value in values.items():
                if key in ("power_w", "regen", "raw_voltage", "raw_current"):
                    continue  # Derived only from a valid, matched V/I response.
                if key.startswith("_"):
                    continue
                measured = (received or {}).get(key, now)
                if finite(measured) is None or not 0 <= now - measured <= (
                    self.bms_ttl if key in ("voltage", "current") else self.slow_ttl
                ):
                    if key in ("voltage", "current"):
                        self.bms.pop(key, None)
                        self.bms_times.pop(key, None)
                        self._invalidate_vi()
                    continue
                if measured < self.bms_times.get(key, -1e9):
                    continue
                if key in ("voltage", "current"):
                    number = finite(value)
                    if number is None:
                        self.bms.pop(key, None)
                        self.bms_times.pop(key, None)
                        self._invalidate_vi()
                        self.input_issues.append("BMS " + key)
                        continue
                    value = number
                    if not canonical:
                        self.bms["raw_" + key] = number
                        self.bms_times["raw_" + key] = measured
                        value = number * p[key + "_scale"] + p[key + "_offset"]
                        if key == "current":
                            value *= p["current_sign"]
                    else:
                        self.bms.pop("raw_" + key, None)
                        self.bms_times.pop("raw_" + key, None)
                    if (
                        not math.isfinite(value)
                        or (key == "voltage" and not 0 <= value <= 1000)
                        or (key == "current" and abs(value) > 5000)
                    ):
                        self.bms.pop(key, None)
                        self.bms_times.pop(key, None)
                        self.input_issues.append("BMS " + key)
                        self._invalidate_vi()
                        continue
                    self.input_issues = [
                        x for x in self.input_issues if x != "BMS " + key
                    ]
                    accepted_vi.add(key)
                elif value is None:
                    continue
                self.bms[key] = value
                self.bms_times[key] = measured
            vi_times = [self.bms_times.get(k) for k in ("voltage", "current")]
            if accepted_vi and (
                accepted_vi != {"voltage", "current"}
                or None in vi_times
                or abs(vi_times[0] - vi_times[1]) >= 0.05
            ):
                self._invalidate_vi()
            if (
                accepted_vi == {"voltage", "current"}
                and None not in vi_times
                and abs(vi_times[0] - vi_times[1]) < 0.05
            ):
                at = min(vi_times)
                voltage, current = (
                    finite(self.bms.get("voltage")),
                    finite(self.bms.get("current")),
                )
                if voltage is None or current is None or voltage < 0:
                    self._invalidate_vi()
                    return
                power = voltage * current
                self.bms["power_w"] = power
                self.bms_times["power_w"] = at
                self.bms["regen"] = current < 0
                self.bms_times["regen"] = at
                prev = self.vi_previous
                if self.vi_break_from is not None:
                    missing = max(0, at - self.vi_break_from)
                    self.run["gap_seconds"] += missing
                    self.event(
                        "bms_gap", {"seconds": missing, "reason": "invalid_pair"}
                    )
                    self.vi_break_from = None
                if prev and 0 < at - prev[0] <= self.bms_ttl:
                    dt = at - prev[0]
                    wh, ah = (
                        (prev[1] + power) * 0.5 * dt / 3600,
                        (prev[2] + current) * 0.5 * dt / 3600,
                    )
                    self.run["used_wh"] += wh
                    self.run["used_ah"] += ah
                    self.increment_wh += wh
                    self.increment_ah += ah
                    self.run["measured_seconds"] += dt
                elif prev and at - prev[0] > self.bms_ttl:
                    self.run["gap_seconds"] += at - prev[0]
                    self.event("bms_gap", {"seconds": at - prev[0]})
                self.vi_previous = (at, power, current)
            if values.get("charger_connected") and not self.run.get("_charger_seen"):
                self.run["_charger_seen"] = True
                self.event("charger_connected")
                # Never silently resume a previous battery budget when charging is observed.
                if self.candidate:
                    self.recovery = "battery_check_required"
                self.run["profile"]["usable_remaining_wh"] = None
            if values.get("charger_connected") is False:
                self.run["_charger_seen"] = False

    def _invalidate_vi(self):
        if self.vi_previous and self.vi_break_from is None:
            self.vi_break_from = self.vi_previous[0]
        self.vi_previous = None
        for key in ("power_w", "regen", "raw_voltage", "raw_current"):
            self.bms.pop(key, None)
            self.bms_times.pop(key, None)

    def update_gps(
        self, values, received=None, position_updated=True, position_time=None
    ):
        at = received if received is not None else self.clock.monotonic()
        with self.lock:
            sanitized = dict(values)
            self.input_issues = [
                x for x in self.input_issues if not x.startswith("GPS")
            ]
            for key, lower, upper in (
                ("lat", -90, 90),
                ("lon", -180, 180),
                ("speed", 0, 350),
                ("heading", 0, 360),
                ("alt", -1000, 10000),
                ("hdop", 0, 100),
                ("g_lon", -20, 20),
                ("g_lat", -20, 20),
            ):
                if values.get(key) is not None:
                    number = finite(values[key])
                    if number is None or not lower <= number <= upper:
                        sanitized[key] = None
                        self.input_issues.append("GPS " + key)
            if sanitized.get("lat") is None or sanitized.get("lon") is None:
                sanitized["fix"] = False
            values = sanitized
            self.gps, self.gps_time = sanitized, at
            p = self.run["profile"]
            speed = finite(values.get("speed"))
            if speed is not None:
                self.gps["speed"] = max(0, speed * p["speed_scale"] + p["speed_offset"])
            if not position_updated:
                return
            at = position_time if position_time is not None else at
            valid = (
                values.get("fix")
                and finite(values.get("lat")) is not None
                and finite(values.get("lon")) is not None
                and (
                    finite(values.get("hdop")) is None
                    or values["hdop"] <= p["gps_max_hdop"]
                )
            )
            if not valid:
                self.position_previous = None
                self.lap_started = None
                self.sector_previous = None
                for gate in self.gates:
                    gate.reset_segment()
                return
            point = (values["lat"], values["lon"])
            previous = self.position_previous
            if previous:
                delta, dt = distance_m(previous[0], point), at - previous[1]
                if (
                    0 < dt <= self.gps_ttl
                    and delta <= dt * 70 + 5
                    and (self.gps.get("speed") or 0) >= 2
                ):
                    self.run["distance_km"] += delta / 1000
                elif dt > self.gps_ttl or delta > max(0, dt) * 70 + 5:
                    self.run["lap_uncertain"] = True
                    self.interrupted_lap = True
                    self.lap_started = None
                    for gate in self.gates:
                        gate.reset_segment()
                    self.sector_previous = None
            self.position_previous = (point, at)
            if self.run["phase"] not in {"RACE", "FORMATION"}:
                return
            for index, gate in enumerate(self.gates):
                if not gate.update(point, at):
                    continue
                if self.sector_previous is not None:
                    prior_at, prior_wh, prior_gate, energy_start = self.sector_previous
                    sector = dict(
                        from_gate=prior_gate,
                        to_gate=index,
                        seconds=at - prior_at,
                        wh=self.run["used_wh"] - prior_wh,
                        complete=index == (prior_gate + 1) % len(self.gates),
                        segment_id=self.segment,
                        energy_complete=self._energy_complete(
                            energy_start, at - prior_at
                        ),
                        energy_revision=self.run["energy_revision"],
                    )
                    self.run["sectors"].append(sector)
                    self.run["sectors"] = self.run["sectors"][-200:]
                    self.event("sector", sector)
                self.sector_previous = (
                    at,
                    self.run["used_wh"],
                    index,
                    self._energy_marker(),
                )
                if index == 0 and p["lap_line"]:
                    if self.lap_started is not None:
                        lap = dict(
                            seconds=at - self.lap_started,
                            wh=self.run["used_wh"] - self.lap_start_wh,
                            complete=not self.interrupted_lap,
                            formation=self.run["phase"] == "FORMATION",
                            energy_complete=self._energy_complete(
                                self.lap_energy_start, at - self.lap_started
                            ),
                            energy_revision=self.run["energy_revision"],
                        )
                        self.run["laps"].append(lap)
                        self.run["laps"] = self.run["laps"][-200:]
                        if lap["complete"] and (
                            not lap["formation"] or p["formation_counted"]
                        ):
                            self.run["lap_count"] += 1
                        self.event("lap", lap)
                    self.lap_started, self.lap_start_wh = at, self.run["used_wh"]
                    self.lap_energy_start = self._energy_marker()
                    self.interrupted_lap = False

    def _energy_marker(self):
        return (
            self.run["measured_seconds"],
            self.run["gap_seconds"],
            self.run["energy_revision"],
        )

    def _energy_complete(self, start, duration):
        if start is None or duration <= 0 or self.run["energy_reasons"]:
            return False
        seconds, gap, revision = start
        return bool(
            revision == self.run["energy_revision"]
            and gap == self.run["gap_seconds"]
            and self.run["measured_seconds"] - seconds >= duration * 0.9
            and self.vi_previous is not None
            and 0 <= self.clock.monotonic() - self.vi_previous[0] <= self.bms_ttl
        )

    def _energy_quality(self):
        if self.run["energy_reasons"]:
            return "unverified_basis"
        if self.run["gap_seconds"] > 0:
            return "partial"
        if self.run["measured_seconds"] <= 0:
            return "waiting"
        if (
            self.vi_previous is None
            or self.clock.monotonic() - self.vi_previous[0] > self.bms_ttl
        ):
            return "partial"
        return "measured"

    def update_external_vi(self, data):
        if not isinstance(data, dict):
            raise ValueError("measurement must be an object")
        for key in ("source", "calibration_id"):
            if not isinstance(data.get(key), str) or not 1 <= len(data[key]) <= 100:
                raise ValueError("source and calibration_id are required")
        voltage, current, age = (
            finite(data.get(k)) for k in ("voltage", "current", "age_seconds")
        )
        if (
            voltage is None
            or current is None
            or age is None
            or not 0 <= voltage <= 1000
            or abs(current) > 5000
            or not 0 <= age <= self.bms_ttl
        ):
            raise ValueError("invalid external V/I or age_seconds")
        now = self.clock.monotonic()
        current_sign = validate_current_sign(data.get("current_sign", 1))
        raw_current = current
        current *= current_sign
        with self.lock:
            at = now - age
            previous = self.external_previous
            identity = (data["source"], data["calibration_id"], current_sign)
            if previous and identity != previous[3]:
                self.run["external_mixed_basis"] = True
                self.event(
                    "external_basis_changed",
                    {"source": data["source"], "current_sign": current_sign},
                )
            if (
                previous
                and identity == previous[3]
                and 0 < at - previous[0] <= self.bms_ttl
            ):
                wh = (previous[1] + voltage * current) * 0.5 * (at - previous[0]) / 3600
                self.run["external_used_wh"] = self.run.get("external_used_wh", 0) + wh
            self.external_previous = (at, voltage * current, current, identity)
            self.external = dict(
                source=data["source"],
                calibration_id=data["calibration_id"],
                voltage=voltage,
                current=current,
                raw_current=raw_current,
                current_sign=current_sign,
                received_mono=at,
                verified=data.get("verified") is True,
            )

    def _external_comparison(self, bms):
        e = self.external
        if not e:
            return {"status": "not_connected"}
        age = self.clock.monotonic() - e["received_mono"]
        valid = 0 <= age <= self.bms_ttl
        return dict(
            status="valid" if valid else "stale",
            source=e["source"],
            calibration_id=e["calibration_id"],
            verified=e["verified"],
            age_seconds=age,
            voltage=e["voltage"] if valid else None,
            current=e["current"] if valid else None,
            raw_current=e["raw_current"] if valid else None,
            current_sign=e["current_sign"],
            current_convention=CURRENT_CONVENTION,
            delta_voltage=e["voltage"] - bms["voltage"]
            if valid and bms.get("voltage") is not None
            else None,
            delta_current=e["current"] - bms["current"]
            if valid and bms.get("current") is not None
            else None,
            measured_wh=self.run.get("external_used_wh", 0),
            mixed_basis=self.run.get("external_mixed_basis", False),
            quality="bridge reported age; no official-meter equivalence",
        )

    def _elapsed(self, name):
        mono = self.run.get(name + "_started_mono")
        utc = self.run.get(name + "_started_utc")
        if self.run.get(name + "_boot") == self.boot and mono is not None:
            return max(0, self.clock.monotonic() - mono)
        if utc is not None and self.time_trusted():
            return max(0, self.clock.time() - utc)
        return None

    def receive_command(self, command):
        required = {
            "id",
            "vehicle",
            "race_session_id",
            "issued_at",
            "expires_at",
            "message",
        }
        if not isinstance(command, dict) or not required <= set(command):
            raise ValueError("incomplete command")
        if (
            command["vehicle"] != self.vehicle
            or command["race_session_id"] != self.run["id"]
        ):
            raise ValueError("command targets another race")
        cancelled = (
            command.get("cancelled") is True or command.get("status") == "cancelled"
        )
        command = {key: command[key] for key in required}
        command["source"] = "TEAM"
        if cancelled:
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE commands SET status='cancelled' WHERE id=?",
                    (command["id"],),
                )
                self.event("command_cancelled", {"id": command["id"]})
            return
        now = self.clock.time()
        issued, expires = finite(command["issued_at"]), finite(command["expires_at"])
        if (
            not self.time_trusted()
            or issued is None
            or expires is None
            or not issued <= now <= expires
            or not 0 < expires - issued <= 300
            or len(str(command["message"])) > 120
        ):
            raise ValueError("command expired, future or clock unverified")
        with self.lock, self.db:
            existing = self.db.execute(
                "SELECT payload FROM commands WHERE id=?", (command["id"],)
            ).fetchone()
            if existing and json.loads(existing[0]) != command:
                raise ValueError("command id reused")
            self.db.execute(
                "INSERT OR IGNORE INTO commands VALUES(?,?,?)",
                (command["id"], json.dumps(command, ensure_ascii=False), "received"),
            )
            self.db.execute(
                "UPDATE commands SET status='received' WHERE id=? AND status='history'",
                (command["id"],),
            )
            self.event("command_received", {"id": command["id"]})
            self.pit_received_mono = self.clock.monotonic()

    def acknowledge_command(self, command_id):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE commands SET status='seen' WHERE id=?", (command_id,)
            )
            self.event("command_seen", {"id": command_id})

    def command_receipts(self):
        with self.lock:
            return [
                {"id": row[0], "status": row[1]}
                for row in self.db.execute(
                    "SELECT id,status FROM commands ORDER BY rowid DESC LIMIT 50"
                )
            ]

    def _valid_bms(self, now):
        out, ages = {}, {}
        for key, value in self.bms.items():
            age = now - self.bms_times.get(key, -1e9)
            ttl = (
                self.bms_ttl
                if key
                in {
                    "voltage",
                    "current",
                    "raw_voltage",
                    "raw_current",
                    "power_w",
                    "soc",
                    "regen",
                    "state",
                    "remain_ah",
                    "faults",
                }
                else self.slow_ttl
            )
            ages[key] = max(0, age)
            out[key] = value if 0 <= age <= ttl else None
        return out, ages

    def _budget(self):
        p = self.run["profile"]
        remaining = p["usable_remaining_wh"]
        laps = p["total_laps"]
        valid = (
            p["confirmed"]
            and not self.run["lap_uncertain"]
            and self.run["gap_seconds"] <= p["budget_reference_gap"]
            and self.run["budget_revision"] == self.run["energy_revision"]
            and not self.run["energy_reasons"]
            and remaining is not None
            and laps is not None
            and laps > self.run["lap_count"]
        )
        if not valid:
            return {"status": "unconfigured_or_uncertain", "target_wh_lap": None}
        left = remaining - (self.run["used_wh"] - p["budget_reference_wh"])
        target = max(0, left - p["reserve_wh"]) / (laps - self.run["lap_count"])
        recent = [
            lap["wh"]
            for lap in self.run["laps"][-3:]
            if lap["complete"]
            and lap.get("energy_complete")
            and lap.get("energy_revision") == self.run["energy_revision"]
            and not lap["formation"]
        ]
        return dict(
            status="estimated",
            remaining_wh=left,
            target_wh_lap=target,
            recent_wh_lap=sum(recent) / len(recent) if recent else None,
            remaining_laps=laps - self.run["lap_count"],
            measured_laps=len(recent),
        )

    def snapshot(self):
        with self.lock:
            now = self.clock.monotonic()
            bms, ages = self._valid_bms(now)
            gps_age = now - self.gps_time if self.gps_time is not None else None
            gps_valid = (
                gps_age is not None
                and 0 <= gps_age <= self.gps_ttl
                and self.gps.get("fix")
            )
            gps = self.gps if gps_valid else {}
            faults = bms.get("faults") or {}
            warnings = []
            for text in faults.get("dangers", []):
                warnings.append({"priority": 0, "text": text, "source": "BMS"})
            previous_faults = self.bms.get("faults") or self.run.get("last_faults", {})
            if bms.get("faults") is None and previous_faults.get("dangers"):
                warnings.append(
                    {
                        "priority": 0,
                        "text": "이전 위험 미확인 · BMS 수신 대기",
                        "source": "BMS_history",
                    }
                )
            if (
                bms.get("voltage") is None
                or bms.get("current") is None
                or bms.get("power_w") is None
            ):
                warnings.append(
                    {"priority": 1, "text": "BMS 새 데이터 대기", "source": "validity"}
                )
            if not gps_valid:
                warnings.append(
                    {"priority": 1, "text": "GPS 속도 확인 불가", "source": "validity"}
                )
            if self.storage_error or self.csv_error:
                warnings.append(
                    {
                        "priority": 1,
                        "text": "로컬 기록 경로 확인 필요",
                        "source": "storage",
                    }
                )
            for text in faults.get("warnings", []):
                warnings.append({"priority": 2, "text": text, "source": "BMS"})
            if self.input_issues:
                warnings.append(
                    {
                        "priority": 1,
                        "text": "입력 수치 확인: " + ", ".join(self.input_issues),
                        "source": "plausibility",
                    }
                )
            p = self.run["profile"]
            for field, threshold, message in (
                (
                    "power_w",
                    p["power_warning_w"]
                    if p["power_warning_w"] is not None
                    else p["power_limit_w"],
                    "참고 출력 기준 접근/초과",
                ),
                (
                    "voltage",
                    p["voltage_warning_v"]
                    if p["voltage_warning_v"] is not None
                    else p["voltage_limit_v"],
                    "참고 전압 기준 접근/초과",
                ),
            ):
                if (
                    threshold is not None
                    and bms.get(field) is not None
                    and bms[field] >= threshold
                ):
                    warnings.append(
                        {"priority": 2, "text": message, "source": "team_reference"}
                    )
            budget = self._budget()
            if (
                bms.get("voltage") is None
                or bms.get("current") is None
                or bms.get("power_w") is None
            ):
                budget = {"status": "measurement_unavailable", "target_wh_lap": None}
            if (
                budget.get("recent_wh_lap") is not None
                and budget["recent_wh_lap"] > budget["target_wh_lap"]
            ):
                warnings.append(
                    {
                        "priority": 2,
                        "text": "최근 소비가 랩 예산 초과",
                        "source": "estimate",
                    }
                )
            commands = []
            if self.time_trusted():
                for payload, status in self.db.execute(
                    "SELECT payload,status FROM commands WHERE status IN ('received','seen')"
                ):
                    command = json.loads(payload)
                    if (
                        command["race_session_id"] == self.run["id"]
                        and command["expires_at"] >= self.clock.time()
                    ):
                        commands.append(dict(command, status=status, source="TEAM"))
            change_elapsed = (
                self._elapsed("change") if self.run["phase"] == "CHANGE" else None
            )
            restart_elapsed = (
                self._elapsed("restart") if self.run["phase"] == "RESTART" else None
            )
            health = self.health()
            if health["record_stale"] or health["disk_low"]:
                warnings.append(
                    {
                        "priority": 1,
                        "text": "기록 갱신/저장 여유 확인",
                        "source": "storage",
                    }
                )
            flat = dict(
                ts=datetime.fromtimestamp(self.clock.time(), timezone.utc).isoformat(
                    timespec="milliseconds"
                ),
                vehicle=self.vehicle,
                session=self.segment,
                segment_id=self.segment,
                race_session_id=self.run["id"],
                sequence=self.sequence,
                sample_id=f"{self.vehicle}:{self.segment}:{self.sequence}",
                boot_id=self.boot,
                t_mono=now,
                schema_version=SCHEMA_VERSION,
                software_version=VERSION,
                time_quality="trusted" if self.time_trusted() else "unverified",
                replay=int(self.replay),
                gps_age=gps_age,
                bms_age=ages.get("voltage"),
                used_wh=round(self.run["used_wh"], 6),
                used_ah=round(self.run["used_ah"], 8),
                distance_km=self.run["distance_km"],
                lap_count=self.run["lap_count"],
                gap_seconds=self.run["gap_seconds"],
                current_sign=p["current_sign"],
                current_convention=CURRENT_CONVENTION,
                energy_quality=self._energy_quality(),
                energy_revision=self.run["energy_revision"],
                measured_seconds=self.run["measured_seconds"],
                phase=self.run["phase"],
                driver_id=self.run["driver_id"],
                recovery_parent=self.run.get("parent"),
                alarm_level=max(
                    [2 if x["priority"] == 0 else 1 for x in warnings] or [0]
                ),
                alarms=" | ".join(x["text"] for x in warnings),
            )
            for key in (
                "lat",
                "lon",
                "alt",
                "speed",
                "heading",
                "hdop",
                "g_lon",
                "g_lat",
            ):
                flat[key] = gps.get(key)
            flat["fix"], flat["sats"] = int(bool(gps_valid)), gps.get("sats")
            for key in (
                "voltage",
                "current",
                "raw_voltage",
                "raw_current",
                "soc",
                "power_w",
                "regen",
                "remain_ah",
                "range_km",
                "state",
                "temp_max",
                "temp_min",
                "cell_v_max",
                "cell_v_min",
                "cell_v_diff",
            ):
                flat[key] = bms.get(key)
            if flat.get("regen") is not None:
                flat["regen"] = int(bool(flat["regen"]))
            flat["balancing"] = (
                int(bool(bms["any_balancing"]))
                if bms.get("any_balancing") is not None
                else None
            )
            checks = dict(
                gps=bool(gps_valid),
                bms=bms.get("voltage") is not None
                and bms.get("current") is not None
                and bms.get("power_w") is not None,
                storage=health["storage_ok"] and not health["record_stale"],
                space=not health["disk_low"],
                profile=p["confirmed"],
                clock=self.time_trusted(),
                recovery=not self.candidate,
            )
            flat["meta"] = json.dumps(
                dict(
                    ages=ages,
                    profile=p,
                    recovery=self.recovery,
                    health=health,
                    segment_parent=self.segment_parent,
                    external=self._external_comparison(bms),
                    lap_uncertain=self.run["lap_uncertain"],
                    battery_epoch=self.battery_epoch,
                    calibration_verified=p["calibration_verified"],
                    energy_reasons=sorted(set(self.run["energy_reasons"])),
                ),
                ensure_ascii=False,
            )
            return dict(
                flat,
                bms=bms,
                bms_ok=bms.get("power_w") is not None,
                warnings=sorted(warnings, key=lambda x: x["priority"]),
                commands=commands,
                health=health,
                recovery=self.recovery,
                candidate=bool(self.candidate),
                profile=p,
                budget=budget,
                change_elapsed=change_elapsed,
                restart_elapsed=restart_elapsed,
                preflight=checks,
                external=self._external_comparison(bms),
                race_elapsed=self._elapsed("race"),
                lap_uncertain=self.run["lap_uncertain"],
                driver_settings=self.run["drivers"].get(self.run["driver_id"], {}),
                trends=self.trend_summary(),
            )

    def trend_summary(self):
        if len(self.trends) < 2:
            return {"temp_c_per_min": None, "voltage_change_v": None}
        a, b = self.trends[0], self.trends[-1]
        dt = b[0] - a[0]
        return dict(
            temp_c_per_min=(b[1] - a[1]) * 60 / dt
            if dt > 0 and a[1] is not None and b[1] is not None
            else None,
            voltage_change_v=b[2] - a[2]
            if a[2] is not None and b[2] is not None
            else None,
        )

    def tick(self):
        with self.lock:
            if self.recovery != "battery_check_required":
                self.try_auto_resume()
            now_m, now_w = self.clock.monotonic(), self.clock.time()
            if self.time_trusted():
                for name in ("race", "change", "restart"):
                    if (
                        self.run.get(name + "_started_utc") is None
                        and self.run.get(name + "_boot") == self.boot
                    ):
                        elapsed = self._elapsed(name)
                        if elapsed is not None:
                            self.run[name + "_started_utc"] = now_w - elapsed
            jump = (now_w - self.last_tick_wall) - (now_m - self.last_tick)
            if abs(jump) > 30:
                self.segment_parent = self.segment
                self.segment = uuid.uuid4().hex
                self.sequence = 0
                self.event(
                    "clock_jump",
                    {"seconds": jump, "previous_segment": self.segment_parent},
                )
            self.last_tick, self.last_tick_wall = now_m, now_w
            self.sequence += 1
            snap = self.snapshot()
            snap["energy_increment_wh"], snap["charge_increment_ah"] = (
                self.increment_wh,
                self.increment_ah,
            )
            self.increment_wh = self.increment_ah = 0.0
            self.trends.append(
                (self.clock.monotonic(), snap.get("temp_max"), snap.get("voltage"))
            )
            self.latest = snap
            record = {
                key: value
                for key, value in snap.items()
                if key
                not in {
                    "bms",
                    "bms_ok",
                    "warnings",
                    "commands",
                    "health",
                    "recovery",
                    "candidate",
                    "profile",
                    "budget",
                    "change_elapsed",
                    "restart_elapsed",
                    "preflight",
                    "lap_uncertain",
                    "driver_settings",
                    "trends",
                    "external",
                    "race_elapsed",
                }
            }
            if len(self.pending) >= 3000:
                self.pending.pop(0)
                self.dropped_samples += 1
                self.storage_error = "pending buffer overflow"
                self.event("record_loss", {"dropped": self.dropped_samples})
            self.pending.append(record)
            if self.clock.monotonic() - self.last_save_mono >= self.persist_interval:
                self.persist()
            return snap

    def persist(self, force=False):
        with self.lock:
            started = time.perf_counter()
            try:
                with self.db:
                    self.db.executemany(
                        "INSERT OR IGNORE INTO samples(id,payload,created) VALUES(?,?,?)",
                        [
                            (
                                s["sample_id"],
                                json.dumps(s, ensure_ascii=False, allow_nan=False),
                                self.clock.time(),
                            )
                            for s in self.pending
                        ],
                    )
                    self.db.executemany(
                        "INSERT OR IGNORE INTO events(id,payload) VALUES(?,?)",
                        [
                            (e["id"], json.dumps(e, ensure_ascii=False))
                            for e in self.events_pending
                        ],
                    )
                    saved = dict(self.run)
                    saved["_last_segment_id"] = self.segment
                    if self.candidate:
                        saved["_candidate"] = self.candidate
                    self.db.execute(
                        "INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?)",
                        (
                            self.run["id"],
                            self.vehicle,
                            json.dumps(saved, ensure_ascii=False, allow_nan=False),
                            self.clock.time(),
                            self.clock.monotonic(),
                            self.boot,
                            int(self.time_trusted()),
                        ),
                    )
                self.pending.clear()
                self.events_pending.clear()
                self.storage_error = self.open_error
                self.last_save = self.clock.time() if self.durable else None
                self.last_save_mono = self.clock.monotonic()
                self.last_persist_ms = (time.perf_counter() - started) * 1000
                return True
            except (sqlite3.Error, ValueError, OSError) as exc:
                self.storage_error = type(exc).__name__ + ": " + str(exc)[:140]
                return False

    def outbox(self, limit=100):
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT payload FROM samples WHERE acked=0 ORDER BY rowid LIMIT ?",
                    (int(limit),),
                )
            ]

    def acknowledge(self, ids):
        with self.lock, self.db:
            self.db.executemany(
                "UPDATE samples SET acked=1 WHERE id=?", [(x,) for x in ids]
            )
            self.pit_received_mono = self.clock.monotonic()

    def events_outbox(self, limit=50):
        with self.lock:
            return [
                json.loads(r[0])
                for r in self.db.execute(
                    "SELECT payload FROM events WHERE acked=0 ORDER BY rowid LIMIT ?",
                    (limit,),
                )
            ]

    def acknowledge_events(self, identifiers):
        with self.lock, self.db:
            self.db.executemany(
                "UPDATE events SET acked=1 WHERE id=?", [(x,) for x in identifiers]
            )

    def health(self):
        backlog = self.db.execute(
            "SELECT COUNT(*) FROM samples WHERE acked=0"
        ).fetchone()[0]
        try:
            free_bytes = shutil.disk_usage(self.directory).free
        except OSError:
            free_bytes = 0
        uptime = self.clock.monotonic() - self.started_mono
        memory = None
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    memory = int(line.split()[1]) / 1024
                    break
        except (OSError, ValueError):
            pass
        return dict(
            storage_ok=self.storage_error is None,
            storage_error=self.storage_error,
            csv_ok=self.csv_error is None,
            csv_error=self.csv_error,
            raw_ok=self.raw_error is None,
            raw_error=self.raw_error,
            raw_dropped=self.raw_dropped,
            last_save_utc=self.last_save,
            pending_samples=len(self.pending),
            backlog=backlog,
            durable=self.durable,
            record_stale=self.last_save is None
            or self.clock.monotonic() - self.last_save_mono
            > max(3, self.persist_interval * 3),
            free_mb=round(free_bytes / 1024**2, 1),
            disk_low=free_bytes < 64 * 1024**2,
            pit_age=None
            if self.pit_received_mono is None
            else self.clock.monotonic() - self.pit_received_mono,
            dropped_samples=self.dropped_samples,
            persist_ms=self.last_persist_ms,
            deadline_misses=self.deadline_misses,
            uptime_s=self.clock.monotonic() - self.started_mono,
            process_cpu_s=time.process_time() - self.started_cpu,
            cpu_percent=(time.process_time() - self.started_cpu) * 100 / uptime
            if uptime > 0
            else None,
            rss_mb=memory,
        )

    def report(self):
        with self.lock:
            all_events = [
                json.loads(r[0])
                for r in self.db.execute("SELECT payload FROM events ORDER BY rowid")
            ] + copy.deepcopy(self.events_pending)
            aliases = {self.run["id"]}
            for _ in range(len(all_events) + 1):
                previous = len(aliases)
                for event in all_events:
                    if (
                        event["kind"] == "resume"
                        and event["race_session_id"] in aliases
                    ):
                        detail = event["detail"]
                        aliases.update(detail.get("provisional_ids", []))
                        if detail.get("provisional"):
                            aliases.add(detail["provisional"])
                if len(aliases) == previous:
                    break
            events = [e for e in all_events if e["race_session_id"] in aliases]
            return dict(
                vehicle=self.vehicle,
                race=copy.deepcopy(self.run),
                software_version=VERSION,
                calibration=self.run["profile"]["calibration_id"],
                warnings="TEAM estimates; gaps are unmeasured; official timing/meter remain authoritative",
                recovery=self.recovery,
                aliases=sorted(aliases),
                events=events,
                health=self.health(),
            )

    def close(self):
        self.persist(force=True)
        self.db.close()
