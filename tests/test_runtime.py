import json
import pytest
from race_runtime import RaceRuntime, Gate, validate_profile
from telemetry_protocol import csv_text, parse_csv


class Clock:
    def __init__(self, wall=1800000000, mono=100):
        self.wall, self.mono = wall, mono

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def runtime(path, clock, boot="boot-a", trusted=True, epoch="pack-a"):
    return RaceRuntime(
        path, clock=clock, boot=boot, time_trusted=lambda: trusted, battery_epoch=epoch
    )


def test_query_does_not_integrate_or_refresh_a_sensor(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    r.update_bms({"voltage": 50, "current": 20, "soc": 80})
    c.advance(1)
    r.update_bms({"voltage": 50, "current": 20})
    expected = 1000 / 3600
    assert r.snapshot()["used_wh"] == pytest.approx(expected, abs=1e-6)
    for _ in range(5):
        r.snapshot()
    assert r.snapshot()["used_wh"] == pytest.approx(expected, abs=1e-6)
    c.advance(2)
    r.update_bms({"remain_ah": 60})
    assert r.snapshot()["voltage"] is None
    assert r.snapshot()["current"] is None
    assert r.snapshot()["used_wh"] == pytest.approx(expected, abs=1e-6)
    r.close()


def test_auto_resume_preserves_total_but_not_live_sensor(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    r.update_bms({"voltage": 50, "current": 20})
    c.advance(1)
    r.update_bms({"voltage": 50, "current": 20})
    r.tick()
    identity = r.run["id"]
    r.close()
    c.advance(30)
    resumed = runtime(tmp_path, c, boot="boot-b")
    assert resumed.run["id"] == identity
    assert resumed.snapshot()["used_wh"] == pytest.approx(1000 / 3600, abs=1e-6)
    assert resumed.snapshot()["speed"] is None
    assert resumed.snapshot()["voltage"] is None
    resumed.update_bms({"voltage": 50, "current": 20})
    assert resumed.run["used_wh"] == pytest.approx(1000 / 3600)
    assert resumed.run["lap_uncertain"]
    assert resumed.run["gap_seconds"] == 30
    resumed.close()


@pytest.mark.parametrize(
    "seconds,trusted,epoch",
    [(301, True, "pack-a"), (30, False, "pack-a"), (30, True, "pack-b")],
)
def test_unsafe_automatic_join_stays_pending(tmp_path, seconds, trusted, epoch):
    c = Clock()
    r = runtime(tmp_path, c)
    r.close()
    c.advance(seconds)
    resumed = runtime(tmp_path, c, boot="boot-b", trusted=trusted, epoch=epoch)
    assert resumed.candidate
    assert resumed.recovery == "candidate"
    with pytest.raises(ValueError):
        resumed.resume(False)
    resumed.close()


def test_regeneration_and_off_interval_are_not_double_counted(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    r.update_bms({"voltage": 50, "current": -10})
    c.advance(1)
    r.update_bms({"voltage": 50, "current": -10})
    assert r.run["used_wh"] == pytest.approx(-500 / 3600)
    c.advance(20)
    r.update_bms({"voltage": 50, "current": 20})
    assert r.run["used_wh"] == pytest.approx(-500 / 3600)
    assert r.run["gap_seconds"] == 20
    r.close()


def test_outbox_survives_reboot_until_ack(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    r.tick()
    r.persist()
    ids = [s["sample_id"] for s in r.outbox()]
    assert ids
    r.close()
    c.advance(5)
    r = runtime(tmp_path, c, boot="boot-b")
    assert set(ids) <= {s["sample_id"] for s in r.outbox()}
    r.acknowledge(ids)
    assert not set(ids) & {s["sample_id"] for s in r.outbox()}
    r.close()


def test_finished_race_is_not_resumed(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    identity = r.run["id"]
    r.set_phase("FINISHED")
    r.close()
    c.advance(1)
    r = runtime(tmp_path, c, boot="boot-b")
    assert r.run["id"] != identity and not r.candidate
    r.close()


def test_csv_quoted_alarm_and_partial_row():
    text = csv_text(
        [
            {
                "ts": "2026-10-05T00:00:00Z",
                "vehicle": "car1",
                "session": "a",
                "alarms": 'A, "B"',
                "used_wh": 12.3,
            }
        ]
    )
    records, errors = parse_csv(text)
    assert not errors and records[0]["alarms"] == 'A, "B"'
    assert records[0]["used_wh"] == 12.3
    _, errors = parse_csv(text + "bad,partial\n")
    assert errors


def test_profile_invalid_values_and_budget(tmp_path):
    with pytest.raises(ValueError):
        validate_profile({"total_laps": 0})
    with pytest.raises(ValueError):
        validate_profile({"voltage_limit_v": float("nan")})
    r = runtime(tmp_path, Clock())
    r.configure(
        {
            "event": "endurance",
            "total_laps": 40,
            "confirmed": True,
            "usable_remaining_wh": 1200,
            "reserve_wh": 200,
        }
    )
    r.update_bms({"voltage": 50, "current": 0})
    assert r.snapshot()["budget"]["target_wh_lap"] == 25
    r.run["lap_uncertain"] = True
    assert r.snapshot()["budget"]["target_wh_lap"] is None
    r.close()


def test_pending_reboots_preserve_staged_energy_and_linked_ids(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    r.update_bms({"voltage": 50, "current": 20})
    c.advance(1)
    r.update_bms({"voltage": 50, "current": 20})
    original = r.run["id"]
    r.close()
    c.advance(10)
    r = runtime(tmp_path, c, boot="boot-b", trusted=False)
    provisional = r.run["id"]
    r.update_bms({"voltage": 50, "current": 20})
    c.advance(1)
    r.update_bms({"voltage": 50, "current": 20})
    r.close()
    c.advance(5)
    r = runtime(tmp_path, c, boot="boot-c", trusted=False)
    r.resume(True)
    assert r.run["id"] == original
    assert r.run["used_wh"] == pytest.approx(2000 / 3600)
    assert r.snapshot()["voltage"] is None
    resume = [x for x in r.events_outbox() if x["kind"] == "resume"][-1]
    assert provisional in resume["detail"]["provisional_ids"]
    r.close()


def test_new_race_does_not_assign_previous_increment_to_new_run(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    r.update_bms({"voltage": 50, "current": 20})
    c.advance(1)
    r.update_bms({"voltage": 50, "current": 20})
    r.new_race("pack-b")
    s = r.tick()
    assert s["used_wh"] == 0 and s["energy_increment_wh"] == 0
    r.close()


def test_corrupt_database_keeps_live_display_with_record_warning(tmp_path):
    (tmp_path / "race_state.sqlite3").write_bytes(b"corrupt data")
    r = runtime(tmp_path, Clock())
    r.update_bms({"voltage": 50, "current": 20})
    assert r.snapshot()["voltage"] == 50
    assert not r.health()["durable"]
    assert not r.health()["storage_ok"]
    r.close()
    assert (tmp_path / "race_state.sqlite3").read_bytes() == b"corrupt data"


def test_wall_jump_changes_segment_not_physical_energy(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    first = r.tick()["segment_id"]
    r.update_bms({"voltage": 50, "current": 20})
    c.advance(1)
    c.wall += 3600
    r.update_bms({"voltage": 50, "current": 20})
    s = r.tick()
    assert s["segment_id"] != first
    assert r.run["used_wh"] == pytest.approx(1000 / 3600)
    assert json.loads(s["meta"])["segment_parent"] == first
    r.close()


def test_gate_exact_line_and_departure_guard():
    gate = Gate([[0, -0.001], [0, 0.001]], departure=30, minimum=1)
    assert not gate.update([-0.0001, 0], 0)
    assert not gate.update([0, 0], 0.5)
    assert gate.update([0.0001, 0], 1)
    # Repeated jitter adds path distance, but never actually departs by 30m.
    for i in range(2, 100):
        assert not gate.update([(-1 if i % 2 else 1) * 0.00001, 0], i)
    assert not gate.update([-0.0005, 0], 100)
    assert gate.update([0.0001, 0], 101)


def test_driver_phase_timers_and_snapshot_staleness(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c, trusted=False)
    r.driver("driver2", {"mode": "endurance", "brightness": 0.7})
    r.set_phase("CHANGE")
    c.advance(5)
    assert r.snapshot()["change_elapsed"] == 5
    r.set_phase("RESTART")
    c.advance(2)
    assert r.snapshot()["restart_elapsed"] == 2
    r.update_gps({"fix": True, "lat": 35, "lon": 129, "speed": 40})
    r.update_bms(
        {"voltage": 50, "current": 10, "faults": {"dangers": ["과열"], "warnings": []}}
    )
    r.tick()
    c.advance(3)
    s = r.snapshot()
    assert s["speed"] is None and s["voltage"] is None
    assert any(w["source"] == "BMS_history" for w in s["warnings"])
    r.close()


def test_external_meter_is_readonly_comparison_and_expires(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    r.update_bms({"voltage": 50, "current": 10})
    r.update_external_vi(
        {
            "source": "hall-test",
            "calibration_id": "bench-1",
            "voltage": 50.2,
            "current": 11,
            "age_seconds": 0,
        }
    )
    e = r.snapshot()["external"]
    assert e["delta_current"] == 1
    assert r.run["used_wh"] == 0
    c.advance(2)
    assert r.snapshot()["external"]["status"] == "stale"
    r.close()


def test_command_is_history_after_restart_and_cancel_can_be_delivered(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    command = {
        "id": "cmd",
        "vehicle": "car1",
        "race_session_id": r.run["id"],
        "issued_at": c.time(),
        "expires_at": c.time() + 30,
        "message": "준비",
    }
    r.receive_command(dict(command, status="queued"))
    r.close()
    c.advance(1)
    r = runtime(tmp_path, c, boot="boot-b")
    assert not r.snapshot()["commands"]
    r.receive_command(dict(command, status="received"))
    assert r.snapshot()["commands"]
    r.receive_command(dict(command, status="cancelled"))
    assert not r.snapshot()["commands"]
    r.close()


def test_command_target_expiration_and_seen(tmp_path):
    c = Clock()
    r = runtime(tmp_path, c)
    command = {
        "id": "command-1",
        "vehicle": "car1",
        "race_session_id": r.run["id"],
        "issued_at": c.time(),
        "expires_at": c.time() + 30,
        "message": "교체 준비",
    }
    r.receive_command(command)
    assert r.snapshot()["commands"][0]["source"] == "TEAM"
    r.acknowledge_command(command["id"])
    assert r.snapshot()["commands"][0]["status"] == "seen"
    c.advance(31)
    assert r.snapshot()["commands"] == []
    with pytest.raises(ValueError):
        r.receive_command(command)
    r.close()


def test_unwritable_state_path_keeps_display_but_never_claims_recorded(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("existing file")
    r = runtime(blocked, Clock())
    r.update_bms({"voltage": 50, "current": 10})
    assert r.snapshot()["voltage"] == 50
    assert not r.health()["durable"] and not r.health()["storage_ok"]
    r.close()
    assert blocked.read_text() == "existing file"


def test_local_report_does_not_mix_an_unrelated_finished_race(tmp_path):
    r = runtime(tmp_path, Clock())
    first = r.run["id"]
    r.event("first-race-marker")
    r.persist()
    r.new_race()
    report = r.report()
    assert first not in report["aliases"]
    assert not any(e["kind"] == "first-race-marker" for e in report["events"])
    r.close()
