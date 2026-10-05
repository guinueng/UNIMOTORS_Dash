import asyncio
import json
import sqlite3
import pytest
from aiohttp.test_utils import TestClient, TestServer
import telemetry_server as server
from research_store import ResearchStore
from telemetry_protocol import csv_text


def test_full_field_backfill_and_duplicate_identity(tmp_path):
    server.DB_PATH = str(tmp_path / "server.db")
    server.db_init()
    store = ResearchStore(server.DB_PATH)
    sample = {
        "ts": "bad-initial-wall-clock",
        "vehicle": "car1",
        "session": "segment",
        "race_session_id": "race",
        "segment_id": "segment",
        "sample_id": "car1:segment:1",
        "sequence": 1,
        "used_wh": 87.2,
        "temp_max": 34,
        "alarms": "A, B",
        "energy_increment_wh": 0.2,
    }
    assert store.ingest([sample])["accepted"] == [sample["sample_id"]]
    assert store.ingest([sample])["accepted"] == [sample["sample_id"]]
    conflict = dict(sample, used_wh=100)
    assert store.ingest([conflict])["rejected"]
    with sqlite3.connect(server.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM records_v7").fetchone()[0] == 1
        assert db.execute("SELECT used_wh,temp_max,alarms FROM track").fetchone() == (
            87.2,
            34,
            "A, B",
        )


def test_http_upload_and_ws_ack_are_committed(tmp_path):
    async def scenario():
        server.DB_PATH = str(tmp_path / "integration.db")
        server.AUTH_TOKEN = "test-secret"
        server.latest.clear()
        server.recent.clear()
        server.last_seen.clear()
        client = TestClient(TestServer(server.make_app()))
        await client.start_server()
        try:
            record = {
                "ts": "2026-10-05T00:00:00Z",
                "vehicle": "car1",
                "session": "segment",
                "race_session_id": "race",
                "sample_id": "car1:segment:1",
                "used_wh": 24.2,
                "temp_max": 29,
            }
            response = await client.post(
                "/upload",
                data=csv_text([record]),
                headers={"X-Auth-Token": "test-secret"},
            )
            body = await response.json()
            assert response.status == 200 and body["accepted"] == [record["sample_id"]]
            ws = await client.ws_connect(
                "/ingest", headers={"X-Auth-Token": "test-secret"}
            )
            await ws.send_json({"type": "batch", "records": [record]})
            ack = await ws.receive_json()
            assert ack["type"] == "ack" and ack["accepted"] == [record["sample_id"]]
            with sqlite3.connect(server.DB_PATH) as db:
                assert db.execute("SELECT used_wh FROM summary").fetchone()[0] == 24.2
            await ws.close()
            response = await client.post(
                "/api/commands",
                json={"vehicle": "car1", "race_session_id": "race", "message": "준비"},
            )
            assert response.status == 401
            response = await client.post(
                "/api/commands",
                json={
                    "vehicle": "car1",
                    "race_session_id": "race",
                    "message": "준비",
                    "ttl_seconds": 30,
                },
                headers={"X-Auth-Token": "test-secret"},
            )
            assert response.status == 200
        finally:
            await client.close()

    asyncio.run(scenario())


def test_sample_identity_migration_preserves_same_wall_timestamp(tmp_path):
    server.DB_PATH = str(tmp_path / "migration.db")
    server.db_init()
    with sqlite3.connect(server.DB_PATH) as db:
        db.execute(
            "INSERT INTO summary(ts,vehicle,session,used_wh) VALUES('same','car1','old',4)"
        )
    store = ResearchStore(server.DB_PATH)
    sample = {
        "ts": "same",
        "vehicle": "car1",
        "session": "new",
        "segment_id": "new",
        "sample_id": "car1:new:1",
        "used_wh": 5,
    }
    store.ingest([sample])
    with sqlite3.connect(server.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM summary").fetchone()[0] == 2
        assert (
            db.execute("SELECT used_wh FROM summary WHERE session='old'").fetchone()[0]
            == 4
        )
    ResearchStore(server.DB_PATH)  # Idempotent startup migration.


def test_ack_not_returned_on_transaction_failure(tmp_path):
    server.DB_PATH = str(tmp_path / "fail.db")
    server.db_init()
    store = ResearchStore(server.DB_PATH)
    with sqlite3.connect(server.DB_PATH) as db:
        db.execute(
            "CREATE TRIGGER reject_track BEFORE INSERT ON track BEGIN SELECT RAISE(ABORT,'injected');END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        store.ingest([{"ts": "same", "vehicle": "car1", "sample_id": "x"}])
    with sqlite3.connect(server.DB_PATH) as db:
        assert db.execute("SELECT COUNT(*) FROM records_v7").fetchone()[0] == 0


def test_report_backfill_order_and_aliases(tmp_path):
    server.DB_PATH = str(tmp_path / "report.db")
    server.db_init()
    store = ResearchStore(server.DB_PATH)
    base = {
        "ts": "same",
        "vehicle": "car1",
        "session": "seg-a",
        "segment_id": "seg-a",
        "race_session_id": "race-a",
    }
    store.ingest(
        [
            dict(base, sample_id="late", sequence=9, used_wh=9),
            dict(base, sample_id="early", sequence=1, used_wh=1),
        ]
    )
    assert store.report("car1", "race-a")["latest_used_wh"] == 9
    child = dict(
        base,
        session="seg-b",
        segment_id="seg-b",
        race_session_id="provisional",
        sample_id="child",
        sequence=1,
        used_wh=10,
        meta=json.dumps({"segment_parent": "seg-a"}),
    )
    store.ingest(
        [child],
        [
            {
                "id": "resume",
                "vehicle": "car1",
                "race_session_id": "race-a",
                "kind": "resume",
                "detail": {"provisional": "provisional"},
            }
        ],
    )
    result = store.report("car1", "race-a")
    assert result["latest_used_wh"] == 10 and result["points"] == 3


def test_cancel_not_undone_by_late_received_ack(tmp_path):
    server.DB_PATH = str(tmp_path / "commands.db")
    server.db_init()
    store = ResearchStore(server.DB_PATH)
    command = store.create_command(
        {"vehicle": "car1", "race_session_id": "race", "message": "교체 준비"}
    )
    store.cancel_command(command["id"])
    replies = store.commands("car1", [{"id": command["id"], "status": "received"}])
    assert replies[0]["cancelled"]
    assert store.commands("car1", [{"id": command["id"], "status": "cancelled"}]) == []


def test_confirmed_delete_removes_canonical_records_and_matching_events(tmp_path):
    server.DB_PATH = str(tmp_path / "delete.db")
    server.db_init()
    store = ResearchStore(server.DB_PATH)
    store.ingest(
        [
            {
                "ts": "t",
                "vehicle": "car1",
                "session": "a",
                "segment_id": "a",
                "race_session_id": "race",
                "sample_id": "a",
            },
            {
                "ts": "t",
                "vehicle": "car1",
                "session": "b",
                "segment_id": "b",
                "race_session_id": "race",
                "sample_id": "b",
            },
        ],
        [
            {
                "id": "event-a",
                "vehicle": "car1",
                "race_session_id": "race",
                "segment_id": "a",
                "kind": "boot",
                "ts": 0,
                "detail": {},
            },
            {
                "id": "event-b",
                "vehicle": "car1",
                "race_session_id": "race",
                "segment_id": "b",
                "kind": "boot",
                "ts": 1,
                "detail": {},
            },
        ],
    )
    assert server.db_count_target("car1", "a")["research"] == 1
    assert server.db_delete_data("car1", "a")["deleted_research"] == 1
    report = store.report("car1", "race")
    assert report["points"] == 1 and [e["id"] for e in report["events"]] == ["event-b"]
    server.db_delete_data("car1")
    assert store.report("car1", "race")["points"] == 0
