"""Real v7 HTTP/SSE/CSV startup in the same flat layout installed on a Pi."""
import csv
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import pytest

from telemetry_protocol import FIELDS

ROOT = Path(__file__).resolve().parents[1]


def test_flat_installed_demo_runs_v7_and_keeps_real_state_separate(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in ("vehicle/demo_sim.py", "vehicle/gps_server.py", "vehicle/race_runtime.py",
                 "vehicle/driver_dashboard.html", "shared/telemetry_protocol.py"):
        shutil.copyfile(ROOT / name, installed / Path(name).name)
    real_state = tmp_path / "shared-state"
    real_state.mkdir()
    sentinel = real_state / "race_state.sqlite3"
    sentinel.write_bytes(b"actual vehicle state must not be touched")
    env = dict(os.environ, UNIMOTORS_STATE_DIR=str(real_state), UNIMOTORS_CURRENT_SIGN="-1",
               PYTHONIOENCODING="utf-8", NO_PROXY="127.0.0.1,localhost")
    env.pop("PYTHONPATH", None)
    log_path = tmp_path / "demo.log"
    with log_path.open("w", encoding="utf8") as log:
        process = subprocess.Popen([sys.executable, "-u", str(installed / "demo_sim.py"),
                                    "--no-upload", "--bind", "127.0.0.1", "--port", "0",
                                    "--duration", "5", "--start-wait", "0", "--seed", "2026",
                                    "--sample-rate", "0.1", "--log-dir", str(tmp_path / "logs")],
                                   env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            def get(port, path, line=False):
                connection = HTTPConnection("127.0.0.1", port, timeout=2)
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    assert response.status == 200
                    return response.readline() if line else response.read()
                finally:
                    connection.close()

            deadline = time.monotonic() + 8
            snapshot = None
            while time.monotonic() < deadline:
                text = log_path.read_text(encoding="utf8")
                match = re.search(r"http://127\.0\.0\.1:(\d+)", text)
                if match:
                    port = int(match[1])
                    snapshot = json.loads(get(port, "/api/state"))
                    if snapshot.get("speed", 0) and snapshot.get("used_wh", 0) > 0:
                        break
                if process.poll() is not None:
                    pytest.fail("demo exited before fresh data: " + text)
                time.sleep(0.05)
            assert snapshot and snapshot["vehicle"] == "demo", log_path.read_text(encoding="utf8")
            assert snapshot["software_version"] == "7.1.0"
            assert snapshot["phase"] == "RACE" and snapshot["bms_ok"]
            assert snapshot["current_sign"] == 1 and snapshot["current"] > 0
            assert snapshot["used_wh"] > 0 and snapshot["sample_id"]
            assert "synthetic" in snapshot["profile"]["source_version"]
            html = get(port, "/").decode("utf8")
            assert 'id="demoBadge"' in html and 'id="contentValue"' in html
            line = get(port, "/stream", line=True).decode("utf8")
            assert line.startswith("data: ")
            assert json.loads(line[6:])["race_session_id"] == snapshot["race_session_id"]
            assert process.wait(timeout=8) == 0, log_path.read_text(encoding="utf8")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
    assert sentinel.read_bytes() == b"actual vehicle state must not be touched"
    assert (real_state / "demo/race_state.sqlite3").is_file()
    with next((tmp_path / "logs").glob("telem_*.csv")).open(encoding="utf8") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == FIELDS
        rows = list(reader)
    assert len(rows) >= 3 and len({row["sample_id"] for row in rows}) == len(rows)
    assert any(float(row["used_wh"]) > 0 for row in rows)
    assert all(row["vehicle"] == "demo" for row in rows)
    assert not list((tmp_path / "logs").glob("*.active"))


@pytest.mark.parametrize("option,value", [("--sample-rate", "0"), ("--duration", "nan"), ("--port", "65536")])
def test_invalid_demo_timing_is_rejected(option, value):
    result = subprocess.run([sys.executable, str(ROOT / "vehicle/demo_sim.py"), option, value],
                            capture_output=True, timeout=5)
    assert result.returncode == 2
