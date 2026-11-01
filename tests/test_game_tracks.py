import json
import math
import sqlite3

import pytest

import game_tracks as gt
from pcars_fixtures import context, telemetry
from pcars_protocol import decode_packet
from pcars_receiver import GameRuntime
from research_store import ResearchStore
from telemetry_protocol import csv_text, parse_csv
import telemetry_server as server
from test_runtime import Clock


def setup_runtime(tmp_path):
    c = Clock()
    r = GameRuntime(tmp_path / "game", clock=c)
    r.update_context(decode_packet(context()))
    return c, r


def feed(r, clock, **kw):
    clock.advance(.2)
    r.update_game(decode_packet(telemetry(**kw)))
    s = r.tick()
    assert r.persist(force=True)
    return s


def test_coordinates_are_native_signed_and_stay_out_of_gps():
    f = decode_packet(telemetry(x=-1234, y=20, z=2345, lap_distance=500))
    assert (f["world_x"], f["world_y"], f["world_z"]) == (-1234, 20, 2345)
    assert f["lap_distance"] == 500 and f["track_length"] == 3000


def test_position_is_committed_with_sample_and_survives_restart_and_csv(tmp_path):
    c, r = setup_runtime(tmp_path)
    s = feed(r, c)
    race = s["race_session_id"]
    parsed, rejected = parse_csv(csv_text([s]))
    assert not rejected
    assert json.loads(parsed[0]["meta"])["input"]["frame"]["world_x"] == 120
    assert s["fix"] == 0 and s["lat"] is None and s["lon"] is None
    assert r.game_trace(race)["points"][0]["z"] == -80
    r.tick()  # Same SDK frame must not create an additional position point.
    r.close()
    reopened = GameRuntime(tmp_path / "game", clock=c)
    assert reopened.game_trace(race)["total"] == 1
    assert reopened.game_races()[0]["race_session_id"] == race
    reopened.close()
    server.DB_PATH = str(tmp_path / "pit.db")
    server.db_init()
    store = ResearchStore(server.DB_PATH)
    assert store.ingest(parsed)["accepted"] == [s["sample_id"]]
    store.ingest(parsed)
    assert store.game_trace("pcars1", race)["total"] == 1
    assert server.db_count_target("pcars1")["game_positions"] == 1
    server.db_delete_data("pcars1")
    assert store.game_trace("pcars1", race)["total"] == 0


def test_pause_dropout_teleport_lap_and_track_boundaries_are_separate(tmp_path):
    c, r = setup_runtime(tmp_path)
    try:
        race = feed(r, c, x=0)["race_session_id"]
        feed(r, c, x=10)
        feed(r, c, game=3, x=10)
        feed(r, c, x=11)
        c.advance(2)
        feed(r, c, x=12)
        feed(r, c, x=500)
        feed(r, c, x=501, lap=4, completed=3)
        result = r.game_trace(race)
        assert len({p["group"] for p in result["points"]}) == 5
        previous_total = result["total"]
        new_context = decode_packet(context())
        new_context["variation"] = "Other"
        r.update_context(new_context)
        r.tick(); r.persist(force=True)
        assert r.snapshot()["input"]["frame"] == {}
        new = feed(r, c, x=502)
        assert new["race_session_id"] != race
        assert r.game_trace(race)["total"] == previous_total
        assert r.game_trace(new["race_session_id"])["key"] != result["key"]
    finally:
        r.close()


def full_lap(r, c, invalid=False, pause=False):
    race = r.run["id"]
    for i in range(40):
        angle = 2 * math.pi * i / 39
        if pause and i == 20:
            feed(r, c, game=3, completed=0, lap=1)
        f = decode_packet(telemetry(x=round(100*math.cos(angle)), z=round(100*math.sin(angle)),
                                   completed=0, lap=1, lap_distance=round(1000*i/39), track_length=1000))
        f["lap_invalid"] = invalid and i == 15
        c.advance(.2); r.update_game(f); r.tick(); r.persist(force=True)
    feed(r, c, x=100, z=0, completed=1, lap=2, lap_distance=5, track_length=1000)
    return race


def test_valid_full_lap_becomes_reusable_layout_map(tmp_path):
    c, r = setup_runtime(tmp_path)
    try:
        race = full_lap(r, c)
        m = r.game_trace(race)["map"]
        assert m["kind"] == "learned" and len(m["paths"][0]) == 40
        r.new_race("next-game")
        assert gt.find_map(r.db, m["key"])["kind"] == "learned"
        # A different layout must never inherit the same circuit's map.
        assert gt.find_map(r.db, gt.track_key("Study circuit", "Long")) is None
    finally:
        r.close()


@pytest.mark.parametrize("invalid,pause", [(True, False), (False, True)])
def test_invalid_or_interrupted_lap_is_not_a_complete_map(tmp_path, invalid, pause):
    c, r = setup_runtime(tmp_path)
    try:
        assert r.game_trace(full_lap(r, c, invalid, pause))["map"] is None
    finally:
        r.close()


def test_map_matching_is_exact_and_import_is_persistent(tmp_path):
    db = sqlite3.connect(tmp_path / "maps.db")
    gt.ensure_schema(db)
    assert gt.find_map(db, gt.track_key("Zolder", ""))["kind"] == "community_reference"
    assert gt.find_map(db, gt.track_key("Zolder", "unknown layout")) is None
    m = gt.save_map(db, dict(version=1, track="Test", variation="Short", paths=[[[0,0],[10,20]]], kind="official"))
    assert m["kind"] == "custom"
    db.commit(); db.close()
    db = sqlite3.connect(tmp_path / "maps.db")
    assert gt.find_map(db, gt.track_key("test", "short"))["paths"] == [[[0,0],[10,20]]]
    db.close()


@pytest.mark.parametrize("change", [dict(paths=[[[float('nan'),0],[1,2]]]),
                                    dict(paths=[[[0,0]]]), dict(track=""),
                                    dict(image={"data_url":"https://example.com/map.png","bounds":[0,0,1,1]}),
                                    dict(image={"data_url":"data:image/svg+xml;base64,PHN2Zy8+","bounds":[0,0,1,1]})])
def test_invalid_or_active_external_map_content_is_rejected(change):
    value = dict(version=1, track="T", variation="GP", paths=[[[0,0],[1,1]]])
    value.update(change)
    with pytest.raises(ValueError):
        gt.validate_map(value)


def test_coordinate_index_failure_rolls_back_ack_transaction(tmp_path):
    c, r = setup_runtime(tmp_path)
    s = feed(r, c); r.close()
    server.DB_PATH = str(tmp_path / "rollback.db")
    server.db_init()
    store = ResearchStore(server.DB_PATH)
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_position BEFORE INSERT ON game_track_points BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.ingest([s])
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM records_v7").fetchone()[0] == 0
