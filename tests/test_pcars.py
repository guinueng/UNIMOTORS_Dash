import json
import struct

import pytest

from pcars_fixtures import context, telemetry
from pcars_protocol import SequenceWindow, decode_packet
from pcars_receiver import DatagramInput, GameRuntime
from telemetry_protocol import csv_text, parse_csv
from test_runtime import Clock


def test_pcars1_offsets_units_and_privacy():
    p = decode_packet(telemetry())
    assert p["speed_kmh"] == 72
    assert (p["rpm"], p["gear"], p["water_c"]) == (4500, "3", 87)
    assert p["fuel_fraction"] == pytest.approx(.65)
    assert (p["current_lap"], p["laps_completed"], p["race_position"]) == (3, 2, 1)
    assert p["local_g_z"] == pytest.approx(-1.96 / 9.80665)
    assert decode_packet(context())["track"] == "Study circuit"
    assert "PRIVATE" not in json.dumps(decode_packet(context()))


@pytest.mark.parametrize("packet", [b"", bytes(12), telemetry()[:-1], telemetry() + b"x",
                                     bytes(1367), telemetry(speed=float("nan")),
                                     telemetry(viewed=2), telemetry(speed=-10)])
def test_bad_packets_do_not_become_live_data(packet):
    with pytest.raises(ValueError):
        decode_packet(packet)


def test_unset_lap_and_invalid_fuel_are_missing_not_zero():
    b = bytearray(telemetry())
    for offset in (12, 16, 20):
        struct.pack_into("<f", b, offset, -123)
    struct.pack_into("<f", b, 116, 99)
    p = decode_packet(b)
    assert all(p[k] is None for k in ("best_lap_s", "last_lap_s", "current_lap_s", "fuel_fraction"))


def test_sequence_wrap_duplicates_reordering_and_gap():
    w = SequenceWindow()
    assert w.accept(63, 0) and w.accept(0, .02)
    assert not w.accept(0, .03) and not w.accept(63, .04)
    assert w.accept(50, .6)  # Counter may wrap many times during a dropout.


def test_game_runtime_freshness_pause_sessions_and_no_fake_bms(tmp_path):
    c = Clock()
    r = GameRuntime(tmp_path, clock=c)
    try:
        receiver = DatagramInput(r)
        source = ("127.0.0.1", 5606)
        assert receiver.receive(telemetry(), source)
        receiver.receive(context(), source)
        initial = r.snapshot()["race_session_id"]
        s = r.tick()
        assert s["speed"] == 72 and s["fix"] == 0 and s["lat"] is None
        assert s["lap_timing"]["current"] == pytest.approx(35.4)
        assert s["input"]["frame"]["gear"] == "3"
        for key in ("voltage", "current", "soc", "used_wh", "used_ah", "energy_increment_wh", "charge_increment_ah"):
            assert s[key] is None
        rows, rejected = parse_csv(csv_text([s]))
        assert not rejected and rows[0]["energy_increment_wh"] is None
        meta = json.loads(rows[0]["meta"])
        assert meta["input"]["context"]["car"] == "Synthetic GT"
        assert "PRIVATE" not in rows[0]["meta"]
        c.advance(.02)
        receiver.receive(telemetry(sequence=1, game=3), source)
        paused = r.snapshot()
        assert paused["speed"] is None and paused["input"]["frame"] == {}
        assert paused["lap_timing"]["current"] is None
        c.advance(.02)
        receiver.receive(telemetry(sequence=2), source)
        assert r.snapshot()["race_session_id"] == initial
        assert not receiver.receive(telemetry(sequence=3), ("127.0.0.2", 5606))
        c.advance(1.6)
        receiver.receive(context(), source)  # Context never refreshes speed.
        stale = r.snapshot()
        assert stale["speed"] is None and stale["input"]["status"] == "stale"
        assert stale["lap_timing"]["current"] is None
        receiver.receive(telemetry(sequence=3, completed=0, lap=1), source)
        assert r.snapshot()["race_session_id"] != initial
        assert r.snapshot()["profile"]["source_version"].startswith("pcars1")
        assert r.report()["race"]["used_wh"] is None
    finally:
        r.close()


def test_duplicate_packets_do_not_refresh_freshness(tmp_path):
    c = Clock()
    r = GameRuntime(tmp_path, clock=c)
    try:
        d = DatagramInput(r, "127.0.0.1")
        d.receive(telemetry(), ("127.0.0.1", 7))
        c.advance(.2)
        assert not d.receive(telemetry(), ("127.0.0.1", 7))
        assert r.snapshot()["input"]["age_s"] == pytest.approx(.2)
        assert r.snapshot()["input"]["receiver"]["reordered"] == 1
        c.advance(2)
        assert not d.receive(telemetry(), ("127.0.0.1", 7))
        assert r.snapshot()["input"]["status"] == "stale"
        assert not d.receive(b"bad", ("127.0.0.1", 7))
    finally:
        r.close()


@pytest.mark.parametrize("game,race,status", [(0, 2, "exited"), (1, 2, "menu"),
                                              (3, 2, "paused"), (4, 2, "restarting"),
                                              (5, 2, "replay"), (2, 3, "finished")])
def test_inactive_game_states_never_expose_live_measurements(tmp_path, game, race, status):
    r = GameRuntime(tmp_path)
    try:
        r.update_game(decode_packet(telemetry(game=game, race=race)))
        s = r.snapshot()
        assert s["speed"] is None and s["input"]["frame"] == {}
        assert s["lap_timing"]["current"] is None
        assert s["input"]["status"] == status and s["warnings"]
    finally:
        r.close()
