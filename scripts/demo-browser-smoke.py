#!/usr/bin/env python3
"""Real v7 demo + local pit server + Chromium, with synthetic isolated databases."""
import json
from contextlib import closing
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from http.client import HTTPConnection

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def until(predicate, message, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, sqlite3.Error):
            pass
        time.sleep(0.1)
    raise AssertionError(message)


def get(port, path):
    connection = HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def count(path, query):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(query).fetchone()[0]


def main():
    qa = ROOT / ".qa"
    qa.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory(prefix="unimotors-demo-qa-") as tmp:
        temp = Path(tmp)
        server_port, demo_port = free_port(), free_port()
        pit_db, state_db = temp / "pit.db", temp / "state/demo/race_state.sqlite3"
        env = dict(os.environ, UNIMOTORS_TOKEN="synthetic-demo-qa-only",
                   UNIMOTORS_SERVER_PORT=str(server_port), UNIMOTORS_SERVER_DB=str(pit_db),
                   UNIMOTORS_STATE_DIR=str(temp / "state"), UNIMOTORS_CURRENT_SIGN="-1",
                   PYTHONIOENCODING="utf-8", NO_PROXY="127.0.0.1,localhost")
        log = (qa / "demo-browser.log").open("w", encoding="utf8")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        def start_pit():
            return subprocess.Popen([sys.executable, str(ROOT / "server/telemetry_server.py")],
                                    env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)

        server, demo = start_pit(), None
        try:
            until(lambda: get(server_port, "/api/status")[0] == 200, "pit startup")
            demo = subprocess.Popen([sys.executable, str(ROOT / "vehicle/demo_sim.py"),
                                     "--bind", "127.0.0.1", "--port", str(demo_port),
                                     "--server", f"ws://127.0.0.1:{server_port}/ingest",
                                     "--duration", "24", "--start-wait", "99", "--seed", "2026",
                                     "--sample-rate", "0.1", "--log-dir", str(temp / "logs")],
                                    env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            until(lambda: get(demo_port, "/api/state")[0] == 200, "demo startup")
            until(lambda: count(state_db, "SELECT COUNT(*) FROM samples WHERE acked=1") >= 3,
                  "actual demo uploader did not receive ACKs")
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 800, "height": 480})
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{demo_port}/")
                page.wait_for_function("data?.vehicle === 'demo' && data?.bms_ok")
                assert page.locator("#demoBadge").is_visible()
                assert page.evaluate("data.current_sign") == 1
                for mode in ("base", "endurance", "pit"):
                    page.locator(f'[data-mode="{mode}"]').click()
                    assert page.locator(f'[data-mode="{mode}"]').get_attribute("aria-pressed") == "true"
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight")
                    page.screenshot(path=str(qa / f"demo-v7-{mode}.png"))
                before = count(pit_db, "SELECT COUNT(*) FROM records_v7 WHERE vehicle='demo'")
                server.terminate()
                server.wait(timeout=5)
                until(lambda: count(state_db, "SELECT COUNT(*) FROM samples WHERE acked=0") >= 5,
                      "offline samples were not queued")
                queued = count(state_db, "SELECT COUNT(*) FROM samples WHERE acked=0")
                server = start_pit()
                until(lambda: get(server_port, "/api/status")[0] == 200, "pit restart")
                until(lambda: count(pit_db, "SELECT COUNT(*) FROM records_v7 WHERE vehicle='demo'") >= before + queued,
                      "offline records were not recovered", seconds=10)
                page.screenshot(path=str(qa / "demo-v7-reconnected.png"))
                browser.close()
            assert demo.wait(timeout=28) == 0
            assert not errors, errors
            with closing(sqlite3.connect(pit_db)) as db:
                total, unique = db.execute("SELECT COUNT(*),COUNT(DISTINCT id) FROM records_v7 WHERE vehicle='demo'").fetchone()
                assert total == unique and total > 5
            (qa / "demo-browser-results.json").write_text(json.dumps({
                "passed": True, "modes": ["base", "endurance", "pit"], "viewport": [800, 480],
                "v7_samples_received": total, "offline_samples_recovered": queued,
                "unique_sample_ids": unique, "actual_local_ws_ack": True,
                "errors": errors, "physical_pi_tested": False, "project_cars_game_tested": False,
            }, ensure_ascii=False, indent=2), encoding="utf8")
            print(f"PASS real v7 demo HTTP/SSE/WS/ACK, 3 modes, {queued} offline samples recovered, {total} unique server records")
        finally:
            for process in (demo, server):
                if process and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            log.close()


if __name__ == "__main__":
    main()
