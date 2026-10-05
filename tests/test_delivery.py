"""Exercise real HTTP/WS boundaries and an unclean process exit with synthetic data."""

import asyncio
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from aiohttp.test_utils import TestClient, TestServer

import backfill
import gps_server as vehicle
import telemetry_server as server
from bms_reader import BMSReader
from race_runtime import RaceRuntime
from telemetry_protocol import csv_text, parse_csv


def test_backfill_ack_active_partial_retry_and_malformed(tmp_path, monkeypatch):
    records = [
        {
            "ts": "clock",
            "vehicle": "car1",
            "session": "seg",
            "sample_id": f"car1:seg:{i}",
            "used_wh": i,
            "alarms": 'A, "B"',
        }
        for i in range(205)
    ]
    path = tmp_path / "telem_seg.csv"
    path.write_text(csv_text(records), encoding="utf8")
    received = set()
    calls = []

    def reply(request, **kwargs):
        samples, rejected = parse_csv(request.data.decode())
        assert not rejected and len(samples) <= 100
        calls.append(len(samples))
        received.update(s["sample_id"] for s in samples)
        return io.BytesIO(
            json.dumps(
                {"accepted": [s["sample_id"] for s in samples], "rejected": []}
            ).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", reply)
    active = tmp_path / "telem_seg.csv.active"
    active.touch()
    assert backfill.upload_one(path, token="test")
    assert calls == [100, 100, 5] and len(received) == 205
    assert not path.with_suffix(".csv.acked").exists()
    active.unlink()
    assert backfill.upload_one(path, token="test")
    marker = path.with_suffix(".csv.acked")
    assert json.loads(marker.read_text())["rows"] == 205
    marker.unlink()
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(b'{"accepted":[],"rejected":[]}'),
    )
    assert not backfill.upload_one(path, token="test")
    assert not marker.exists()
    monkeypatch.setattr(urllib.request, "urlopen", reply)
    assert backfill.upload_one(path, token="test") and len(received) == 205
    partial = tmp_path / "telem_partial.csv"
    partial.write_text(csv_text(records[:1]) + "bad,partial\n", encoding="utf8")
    assert not backfill.upload_one(partial, token="test")
    assert not partial.with_suffix(".csv.acked").exists()
    malformed = tmp_path / "telem_bad.csv"
    malformed.write_text('ts,vehicle\n"unterminated', encoding="utf8")
    assert not backfill.upload_one(malformed)


def test_fast_bms_keeps_vi_timestamp_before_mos_timeout(monkeypatch):
    reader = BMSReader()
    clock = [100.0]
    monkeypatch.setattr("bms_reader.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        reader, "read_soc_vi", lambda: {"voltage": 50, "current": 10, "soc": 80}
    )

    def slow_mos():
        clock[0] += 0.3
        return {"remain_ah": 20, "state": "discharge"}

    monkeypatch.setattr(reader, "read_mos", slow_mos)
    result = reader.read_fast()
    assert result["_received"]["voltage"] == 100
    assert result["_received"]["remain_ah"] == 100.3
    monkeypatch.setattr(reader, "read_soc_vi", lambda: None)
    result = reader.read_fast()
    assert "voltage" not in result and "voltage" not in result["_received"]


def test_vehicle_control_auth_motion_and_stale_confirmation(tmp_path, monkeypatch):
    runtime = RaceRuntime(tmp_path, boot="test", time_trusted=lambda: True)
    monkeypatch.setattr(vehicle, "RUNTIME", runtime)
    monkeypatch.setattr(vehicle, "UPLOAD_TOKEN", "test-secret")
    http = ThreadingHTTPServer(("127.0.0.1", 0), vehicle.Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()

    def post(body, token="test-secret"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{http.server_port}/api/control",
            data=json.dumps(body).encode(),
            headers={"X-Auth-Token": token, "Content-Type": "application/json"},
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            return response.code, json.load(response)

    try:
        body = {"action": "phase", "phase": "RACE", "confirm_stationary": True}
        assert post(body, "wrong")[0] == 401
        runtime.update_gps({"fix": True, "lat": 35, "lon": 129, "speed": 30})
        assert post(body)[0] == 400  # Attestation cannot override a fresh moving speed.
        runtime.gps_time = time.monotonic() - 10
        assert post(dict(body, confirm_stationary=False))[0] == 400
        assert post(body)[0] == 200
        assert runtime.snapshot()["phase"] == "RACE"
        assert any(
            e["kind"] == "operator_confirmation" for e in runtime.report()["events"]
        )
    finally:
        http.shutdown()
        http.server_close()
        thread.join()
        runtime.close()


def test_real_uploader_outbox_ack_and_team_seen(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "server.db"))
        monkeypatch.setattr(server, "AUTH_TOKEN", "test-secret")
        server.latest.clear()
        server.recent.clear()
        server.last_seen.clear()
        client = TestClient(TestServer(server.make_app()))
        await client.start_server()
        runtime = RaceRuntime(
            tmp_path / "vehicle", battery_epoch="test", time_trusted=lambda: True
        )
        runtime.tick()
        runtime.persist()
        command = client.server.app[server.RESEARCH_KEY].create_command(
            {
                "vehicle": "car1",
                "race_session_id": runtime.run["id"],
                "message": "교체 준비",
                "ttl_seconds": 30,
            }
        )
        stop = threading.Event()
        monkeypatch.setattr(vehicle, "RUNTIME", runtime)
        monkeypatch.setattr(vehicle, "STOP", stop)
        monkeypatch.setattr(vehicle, "REPLAY", False)
        monkeypatch.setattr(vehicle, "UPLOAD_ENABLED", True)
        monkeypatch.setattr(vehicle, "UPLOAD_TOKEN", "test-secret")
        monkeypatch.setattr(vehicle, "UPLOAD_RATE", 0.05)
        monkeypatch.setattr(
            vehicle,
            "SERVER_WS_URL",
            str(client.make_url("/ingest")).replace("http:", "ws:"),
        )
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        worker = threading.Thread(target=vehicle.uploader, daemon=True)
        worker.start()

        async def until(predicate):
            for _ in range(150):
                if predicate():
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("uploader did not complete")

        try:
            await until(
                lambda: not runtime.outbox() and bool(runtime.snapshot()["commands"])
            )
            with sqlite3.connect(server.DB_PATH) as db:
                assert db.execute("SELECT COUNT(*) FROM records_v7").fetchone()[0] == 1
            runtime.acknowledge_command(command["id"])
            await until(
                lambda: (
                    client.server.app[server.RESEARCH_KEY].command_history("car1")[0][
                        "status"
                    ]
                    == "seen"
                )
            )
        finally:
            stop.set()
            await asyncio.to_thread(worker.join, 6)
            assert not worker.is_alive()
            runtime.close()
            await client.close()

    asyncio.run(scenario())


def test_unclean_process_exit_restores_only_committed_checkpoint(tmp_path):
    script = """
import json,os,sys,time
from race_runtime import RaceRuntime
r=RaceRuntime(sys.argv[1],boot='cut-a',battery_epoch='pack-a',time_trusted=lambda:True)
r.update_bms({'voltage':50,'current':20});time.sleep(.1)
r.update_bms({'voltage':50,'current':20});s=r.tick();r.persist()
print(json.dumps({'race':r.run['id'],'wh':r.run['used_wh'],'sample':s['sample_id']}),flush=True)
time.sleep(.1);r.update_bms({'voltage':50,'current':20})
os._exit(0)
"""
    env = dict(
        os.environ,
        PYTHONPATH=str(vehicle.Path(vehicle.__file__).parent)
        + os.pathsep
        + str(vehicle.Path(vehicle.__file__).resolve().parents[1] / "shared"),
    )
    saved = json.loads(
        subprocess.check_output(
            [sys.executable, "-c", script, str(tmp_path)], env=env, text=True
        )
    )
    r = RaceRuntime(
        tmp_path, boot="cut-b", battery_epoch="pack-a", time_trusted=lambda: True
    )
    try:
        assert r.run["id"] == saved["race"]
        assert r.run["used_wh"] == pytest.approx(saved["wh"])
        assert r.snapshot()["voltage"] is None
        assert saved["sample"] in {s["sample_id"] for s in r.outbox()}
    finally:
        r.close()


def test_replay_cli_uses_separate_state_and_vehicle_identity(tmp_path):
    state = tmp_path / "state"
    r = RaceRuntime(state, boot="live", time_trusted=lambda: True)
    r.tick()
    r.close()
    original = (state / "race_state.sqlite3").read_bytes()
    source = tmp_path / "source.csv"
    source.write_text(
        csv_text(
            [
                {
                    "ts": "2026-10-05",
                    "vehicle": "car1",
                    "session": "input",
                    "sample_id": f"input:{i}",
                    "fix": 1,
                    "lat": 35,
                    "lon": 129,
                    "speed": 25,
                    "voltage": 50,
                    "current": 10,
                }
                for i in range(100)
            ]
        ),
        encoding="utf8",
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(
        os.environ,
        UNIMOTORS_STATE_DIR=str(state),
        UNIMOTORS_LOG_DIR=str(tmp_path / "logs"),
        UNIMOTORS_VEHICLE_PORT=str(port),
        UNIMOTORS_TIME_TRUSTED="1",
        UNIMOTORS_TOKEN="test-only",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(vehicle.Path(vehicle.__file__)),
            "--replay",
            str(source),
            "--replay-interval",
            ".1",
            "--bind",
            "127.0.0.1",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for _ in range(100):
            try:
                with opener.open(
                    f"http://127.0.0.1:{port}/api/state", timeout=1
                ) as reply:
                    snapshot = json.load(reply)
                break
            except OSError:
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read().decode(errors="replace"))
                time.sleep(0.05)
        else:
            raise AssertionError("replay startup timeout")
        assert snapshot["replay"] == 1 and snapshot["vehicle"] == "replay-car1"
        assert snapshot["voltage"] == 50
        assert (state / "replay/race_state.sqlite3").exists()
        assert (state / "race_state.sqlite3").read_bytes() == original
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stderr.close()
