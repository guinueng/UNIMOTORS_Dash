#!/usr/bin/env python3
"""Synthetic UDP -> actual receiver -> HTTP/SSE/themes/CSV/v7 pit ACK.

This is protocol integration evidence, not an actual Project CARS game test.
"""
from contextlib import closing
import csv
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from pcars_fixtures import context, telemetry


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(port, path):
    c = HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def until(test, message, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            if test():
                return
        except (OSError, sqlite3.Error):
            pass
        time.sleep(.05)
    raise AssertionError(message)


def main():
    qa = ROOT / ".qa"
    qa.mkdir(exist_ok=True)
    errors = []
    with tempfile.TemporaryDirectory(prefix="unimotors-pcars-qa-") as tmp:
        temp = Path(tmp)
        http_port, pit_port, udp_port = free_port(), free_port(), free_port(socket.SOCK_DGRAM)
        state = temp / "state/pcars1/race_state.sqlite3"
        pit_db = temp / "pit.db"
        env = dict(os.environ, UNIMOTORS_STATE_DIR=str(temp / "state"),
                   UNIMOTORS_TOKEN="synthetic-pcars-test-only", UNIMOTORS_SERVER_PORT=str(pit_port),
                   UNIMOTORS_SERVER_DB=str(pit_db), PYTHONIOENCODING="utf-8", NO_PROXY="127.0.0.1,localhost")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        log = (qa / "pcars-browser.log").open("w", encoding="utf8")
        processes = []
        stop = threading.Event()
        fixture = dict(game=2, enabled=True)
        sender = None
        try:
            processes.append(subprocess.Popen([sys.executable, str(ROOT / "server/telemetry_server.py")],
                                              env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags))
            until(lambda: get(pit_port, "/api/status")[0] == 200, "pit startup")
            processes.append(subprocess.Popen([sys.executable, "-u", str(ROOT / "vehicle/pcars_receiver.py"),
                                              "--bind", "127.0.0.1", "--port", str(http_port),
                                              "--udp-bind", "127.0.0.1", "--udp-port", str(udp_port),
                                              "--upload", "--server", f"ws://127.0.0.1:{pit_port}/ingest",
                                              "--sample-rate", ".1", "--duration", "45",
                                              "--log-dir", str(temp / "logs")],
                                              env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags))
            until(lambda: get(http_port, "/api/state")[0] == 200, "receiver startup")

            def send():
                seq = 0
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    while not stop.wait(.05):
                        if fixture["enabled"]:
                            sock.sendto(telemetry(sequence=seq, game=fixture["game"]), ("127.0.0.1", udp_port))
                            sock.sendto(context(), ("127.0.0.1", udp_port))
                            seq = (seq + 1) % 64

            sender = threading.Thread(target=send, daemon=True)
            sender.start()
            until(lambda: json.loads(get(http_port, "/api/state")[1])["speed"] == 72, "UDP speed not received")

            def acked():
                with closing(sqlite3.connect(state)) as db:
                    return db.execute("SELECT COUNT(*) FROM samples WHERE acked=1").fetchone()[0]

            until(lambda: acked() >= 3, "actual game receiver uploader did not receive ACKs")
            # Also check the actual streamed wire state, rather than only the REST API.
            c = HTTPConnection("127.0.0.1", http_port, timeout=2)
            try:
                c.request("GET", "/stream")
                line = c.getresponse().readline().decode("utf8")
                assert json.loads(line[6:])["input"]["kind"] == "game"
            finally:
                c.close()
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 800, "height": 480})
                page.on("pageerror", lambda e: errors.append(str(e)))
                url = f"http://127.0.0.1:{http_port}"
                for theme in ("v7", "claude"):
                    page.goto(url + "/?theme=" + theme)
                    page.wait_for_function("document.querySelector('#speed').textContent==='72'")
                    assert page.locator("#demoBadge").inner_text() == "PCARS 1 · GAME"
                    assert page.locator("#theme").input_value() == theme
                    for width, height in ((800, 480), (892, 412), (1366, 1024)):
                        page.set_viewport_size(dict(width=width, height=height))
                        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight"), (theme, width, height)
                        for selector in ("#speed", "#theme", "#batt" if theme == "claude" else "#metrics"):
                            box = page.locator(selector).bounding_box()
                            assert box and box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= width + .5 and box["y"] + box["height"] <= height + .5, (theme, selector, box)
                        page.screenshot(path=str(qa / f"pcars-{theme}-{width}x{height}.png"))
                    # Pause clears values while UDP packets and the HTTP stream continue.
                    fixture["game"] = 3
                    page.wait_for_function("document.querySelector('#speed').textContent==='--'")
                    live_clock = "#lapCurrent" if theme == "claude" else "#contentValue"
                    assert page.locator(live_clock).inner_text() == "--:--.-"
                    assert ("일시정지" in page.locator("#alarm" if theme == "claude" else "#alert").inner_text())
                    fixture["game"] = 2
                    page.wait_for_function("document.querySelector('#speed').textContent==='72'")
                    fixture["enabled"] = False
                    page.wait_for_function("document.querySelector('#speed').textContent==='--'", timeout=5000)
                    assert page.locator(live_clock).inner_text() == "--:--.-"
                    fixture["enabled"] = True
                    page.wait_for_function("document.querySelector('#speed').textContent==='72'")
                page.locator("#theme").select_option("v7")
                page.wait_for_selector("#contentValue")
                page.goto(url + "/")
                page.wait_for_selector("#contentValue")
                page.locator("#theme").select_option("claude")
                page.wait_for_selector("#gg")
                page.goto(url + "/")
                page.wait_for_selector("#gg")
                # Game-native session controls cannot be overridden via the UI/API.
                fixture["game"] = 3
                page.wait_for_function("document.querySelector('#speed').textContent==='--'")
                result = page.evaluate("""async()=>{const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json','X-Auth-Token':'synthetic-pcars-test-only'},body:JSON.stringify({action:'new_race',confirm_stationary:true})});return r.status;}""")
                assert result == 400
                browser.close()
            assert not errors, errors
        finally:
            stop.set()
            if sender:
                sender.join(timeout=2)
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            log.close()
        rows = []
        for path in (temp / "logs").glob("telem_*.csv"):
            with path.open(encoding="utf8") as handle:
                rows.extend(csv.DictReader(handle))
        assert len(rows) > 3
        assert any(json.loads(r["meta"])["input"]["frame"].get("rpm") == 4500 for r in rows)
        assert all(r["soc"] == r["voltage"] == r["used_wh"] == r["energy_increment_wh"] == "" for r in rows)
        assert all(r["vehicle"] == "pcars1" for r in rows)
        print("PASS: synthetic PCARS1 UDP -> v7 HTTP/SSE/CSV/pit ACK; 2 themes x 3 viewports; pause/dropout; theme preference; game control isolation")


if __name__ == "__main__":
    main()
