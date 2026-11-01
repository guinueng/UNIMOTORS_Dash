"""Polarity, recovery provenance and net-energy regression tests."""

import json
import sqlite3
import struct

import pytest

from bms_reader import BMSReader
from race_runtime import RaceRuntime, validate_profile
from research_store import ResearchStore
from telemetry_protocol import normalize_sample, csv_text, parse_csv
import gps_server as vehicle
import telemetry_server as server
from test_runtime import Clock


def make_runtime(path, clock, sign=-1):
    return RaceRuntime(
        path,
        clock=clock,
        boot="test-boot",
        time_trusted=lambda: True,
        battery_epoch="pack-a",
        current_sign=sign,
    )


@pytest.mark.parametrize("sign,discharge", [(-1, -20), (1, 20)])
def test_both_native_conventions_produce_same_net_energy(tmp_path, sign, discharge):
    c = Clock()
    r = make_runtime(tmp_path, c, sign)
    try:
        for _ in range(2):
            r.update_bms(
                {"voltage": 50, "current": discharge, "power_w": -999, "regen": True}
            )
            c.advance(1)
        s = r.tick()
        assert s["raw_current"] == discharge and s["current"] == 20
        assert s["power_w"] == 1000 and s["regen"] == 0
        assert s["used_wh"] == pytest.approx(1000 / 3600, abs=1e-6)
        assert s["used_ah"] == pytest.approx(20 / 3600, abs=1e-8)
        # Trapezoid includes a transition, then a full second of charging.
        r.update_bms({"voltage": 50, "current": -discharge})
        c.advance(1)
        r.update_bms({"voltage": 50, "current": -discharge})
        assert r.snapshot()["regen"] == 1
        assert r.run["used_wh"] == pytest.approx(0)
        assert r.run["used_ah"] == pytest.approx(0)
        records, rejected = parse_csv(csv_text([r.tick()]))
        assert not rejected
        assert records[0]["current_convention"] == "discharge_positive"
        assert records[0]["current_sign"] == sign
    finally:
        r.close()


@pytest.mark.parametrize("invalid", [0, 2, None, True, "bad", float("nan")])
def test_invalid_polarity_is_rejected(invalid):
    with pytest.raises(ValueError, match="current_sign"):
        validate_profile({"current_sign": invalid})


def test_daly_parser_preserves_native_sign(monkeypatch):
    reader = BMSReader()
    monkeypatch.setattr(
        reader, "_request", lambda *a: [struct.pack(">HHHH", 500, 0, 29800, 800)]
    )
    result = reader.read_soc_vi()
    assert result == {"voltage": 50.0, "current": -20.0, "soc": 80.0}


def test_calibration_is_applied_before_sign_and_not_again_in_replay(
    tmp_path, monkeypatch
):
    c = Clock()
    r = make_runtime(tmp_path, c)
    try:
        r.configure({"current_scale": 2, "current_offset": 1, "voltage_scale": 1.1})
        r.update_bms({"voltage": 50, "current": -10})
        s = r.tick()
        assert s["raw_voltage"] == 50 and s["voltage"] == pytest.approx(55)
        assert s["current"] == 19 and s["raw_current"] == -10
        path = tmp_path / "stored.csv"
        path.write_text(csv_text([s]), encoding="utf8")
        monkeypatch.setattr(vehicle, "RUNTIME", r)
        vehicle.STOP.clear()
        vehicle.replay_input(path, 0)
        assert r.snapshot()["current"] == 19
        assert r.snapshot()["voltage"] == pytest.approx(55)
        assert r.snapshot()["raw_current"] is None
    finally:
        r.close()


def test_mid_race_switch_flushes_old_increment_and_invalidates_budget(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c)
    try:
        r.configure({"confirmed": True, "total_laps": 40, "usable_remaining_wh": 1200})
        r.update_bms({"voltage": 50, "current": -20})
        c.advance(1)
        r.update_bms({"voltage": 50, "current": -20})
        old_segment = r.segment
        r.configure({"current_sign": 1})
        assert r.snapshot()["current"] is None and r.snapshot()["power_w"] is None
        assert r.run["used_wh"] == pytest.approx(1000 / 3600)
        assert r.segment != old_segment and r.segment_parent == old_segment
        prior = [s for s in r.outbox() if s["segment_id"] == old_segment]
        assert sum(s["energy_increment_wh"] for s in prior) == pytest.approx(
            1000 / 3600
        )
        r.update_bms({"voltage": 50, "current": 20})
        assert r.snapshot()["energy_quality"] == "unverified_basis"
        assert r.snapshot()["budget"]["target_wh_lap"] is None
        r.new_race("pack-b")
        assert r.run["profile"]["current_sign"] == 1
        assert r.run["used_wh"] == 0 and r.run["energy_reasons"] == []
        r.set_phase("FINISHED")
    finally:
        r.close()
    reopened = make_runtime(tmp_path, c, -1)
    try:
        assert reopened.run["profile"]["current_sign"] == 1  # persisted selection wins
    finally:
        reopened.close()


def test_upgrade_preserves_legacy_total_requires_manual_confirmation(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c, 1)
    r.run["used_wh"] = -42
    identity = r.run["id"]
    r.close()
    with sqlite3.connect(tmp_path / "race_state.sqlite3") as db:
        saved = json.loads(
            db.execute("SELECT state FROM runs WHERE id=?", (identity,)).fetchone()[0]
        )
        saved["profile"].pop("current_sign")
        for key in (
            "energy_reasons",
            "energy_revision",
            "measured_seconds",
            "budget_revision",
        ):
            saved.pop(key)
        db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(saved), identity))
    c.advance(30)
    restored = make_runtime(tmp_path, c)
    try:
        assert restored.candidate and restored.run["id"] != identity
        restored.resume(True)
        assert restored.run["used_wh"] == -42
        assert restored.snapshot()["energy_quality"] == "unverified_basis"
        assert restored.run["profile"]["current_sign"] == -1
    finally:
        restored.close()


def test_bad_vi_pair_does_not_reuse_cached_current_or_show_power(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c)
    try:
        r.update_bms({"voltage": 50, "current": -20})
        c.advance(1)
        r.update_bms({"voltage": 50, "current": float("nan")})
        assert r.snapshot()["current"] is None
        assert r.snapshot()["power_w"] is None
        c.advance(1)
        r.update_bms({"voltage": 50, "current": -20})
        assert r.run["used_wh"] == 0
        assert r.run["gap_seconds"] == 2
        c.advance(1)
        r.update_bms({"voltage": 50, "current": -20})
        assert r.run["used_wh"] == pytest.approx(1000 / 3600)
        assert r.snapshot()["energy_quality"] == "partial"
    finally:
        r.close()


@pytest.mark.parametrize("has_bms", [False, True])
def test_gps_lap_without_energy_coverage_is_excluded_from_budget(tmp_path, has_bms):
    c = Clock()
    r = make_runtime(tmp_path, c)
    try:
        r.configure(
            {
                "lap_line": [[35, 128.999], [35, 129.001]],
                "lap_min_seconds": 1,
                "lap_departure_m": 1,
                "confirmed": True,
                "total_laps": 40,
                "usable_remaining_wh": 1200,
            }
        )
        r.set_phase("RACE")
        for lat in (34.9999, 35.0001, 34.9999):
            if has_bms:
                r.update_bms({"voltage": 50, "current": -20})
            r.update_gps({"fix": True, "lat": lat, "lon": 129, "speed": 20})
            c.advance(1)
        lap = r.run["laps"][-1]
        assert lap["complete"] and r.run["lap_count"] == 1
        assert lap["energy_complete"] is has_bms
        assert r._budget()["measured_laps"] == int(has_bms)
        assert r.run["sectors"][-1]["energy_complete"] is has_bms
    finally:
        r.close()


def test_external_meter_has_independent_polarity_and_boundary(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c)
    measurement = {
        "source": "meter",
        "calibration_id": "cal",
        "voltage": 50,
        "current": -22,
        "current_sign": -1,
        "age_seconds": 0,
    }
    try:
        r.update_bms({"voltage": 50, "current": -20})
        r.update_external_vi(measurement)
        assert r.snapshot()["external"]["delta_current"] == 2
        c.advance(1)
        r.update_external_vi(measurement)
        expected = 1100 / 3600
        assert r.run["external_used_wh"] == pytest.approx(expected)
        c.advance(1)
        r.update_external_vi(dict(measurement, current_sign=1, current=22))
        assert r.run["external_used_wh"] == pytest.approx(expected)
        assert r.snapshot()["external"]["mixed_basis"] is True
        assert r.run["used_wh"] == 0
    finally:
        r.close()


def test_schema_upgrade_acks_old_immutable_payload_without_null_columns(tmp_path):
    server.DB_PATH = str(tmp_path / "server.db")
    server.db_init()
    store = ResearchStore(server.DB_PATH)
    sample = {"ts": "clock", "vehicle": "car1", "session": "seg", "sample_id": "old-v7"}
    record = normalize_sample(sample)
    for key in (
        "raw_voltage",
        "raw_current",
        "current_sign",
        "current_convention",
        "energy_quality",
        "energy_revision",
        "measured_seconds",
    ):
        record.pop(key)
    with store.connect() as db:
        db.execute(
            "INSERT INTO records_v7 VALUES(?,?,?,?,?,?)",
            ("old-v7", "car1", "seg", "seg", json.dumps(record), 0),
        )
    assert store.ingest([sample])["accepted"] == ["old-v7"]
    assert store.ingest([dict(sample, current_sign=-1)])["rejected"]


def test_cumulative_net_increments_ignore_restored_total_and_keep_zero(tmp_path):
    server.DB_PATH = str(tmp_path / "server.db")
    server.db_init()
    store = ResearchStore(server.DB_PATH)
    records = []
    for i, (segment, total, increment) in enumerate(
        [("a", 10, 10), ("a", 8, -2), ("b", 8, 0), ("b", 0, -8)]
    ):
        records.append(
            {
                "ts": f"2026-10-06T00:00:0{i}Z",
                "vehicle": "car1",
                "session": segment,
                "segment_id": segment,
                "sample_id": f"sample-{i}",
                "used_wh": total,
                "energy_increment_wh": increment,
                "current_convention": "discharge_positive",
                "energy_quality": "measured",
            }
        )
    assert not store.ingest(records)["rejected"]
    result = server.db_query_cumulative("car1")["stats"]
    assert result["used_wh"] == 0
    assert result["energy_basis"] == "measured_increments"


def test_unrelated_profile_change_does_not_reset_lap_or_budget_anchor(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c)
    try:
        r.configure({"usable_remaining_wh": 1200})
        r.update_bms({"voltage": 50, "current": -20})
        c.advance(1)
        r.update_bms({"voltage": 50, "current": -20})
        r.lap_started = 100
        anchor = r.run["profile"]["budget_reference_wh"]
        r.configure({"power_warning_w": 8000})
        assert r.run["profile"]["budget_reference_wh"] == anchor
        assert r.lap_started == 100
    finally:
        r.close()


def test_danger_history_survives_reboot_until_fresh_alarm_response(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c)
    r.update_bms(
        {"voltage": 50, "current": -10, "faults": {"dangers": ["과열"], "warnings": []}}
    )
    r.close()
    c.advance(30)
    r = make_runtime(tmp_path, c)
    try:
        assert r.snapshot()["voltage"] is None
        assert any(
            w["source"] == "BMS_history" and w["priority"] == 0
            for w in r.snapshot()["warnings"]
        )
        r.update_bms({"voltage": 50, "current": -10})
        assert any(w["source"] == "BMS_history" for w in r.snapshot()["warnings"])
        r.update_bms({"faults": {"dangers": [], "warnings": []}})
        assert not any(w["source"] == "BMS_history" for w in r.snapshot()["warnings"])
    finally:
        r.close()


def test_pending_profile_switch_cannot_auto_resume_old_basis(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c, 1)
    r.close()
    c.advance(301)
    r = make_runtime(tmp_path, c)
    try:
        assert r.candidate
        r.configure({"current_sign": -1})
        r.resume_window = 600
        r.try_auto_resume()
        assert r.candidate
        r.resume(True)
        assert r.run["profile"]["current_sign"] == -1
        assert r.snapshot()["energy_quality"] == "unverified_basis"
    finally:
        r.close()


def test_replay_override_cannot_flip_stored_canonical_csv(tmp_path, monkeypatch):
    c = Clock()
    r = make_runtime(tmp_path, c)
    try:
        r.update_bms({"voltage": 50, "current": -10})
        path = tmp_path / "stored.csv"
        path.write_text(csv_text([r.tick()]), encoding="utf8")
        monkeypatch.setattr(vehicle, "RUNTIME", r)
        with pytest.raises(ValueError, match="stored canonical CSV"):
            vehicle.replay_input(path, 0, -1)
    finally:
        r.close()
