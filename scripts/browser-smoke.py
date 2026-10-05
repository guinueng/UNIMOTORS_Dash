#!/usr/bin/env python3
"""Render actual HTTP/SSE/WS apps with isolated synthetic data; never uses vehicle devices."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
for folder in ("vehicle", "shared"):
    sys.path.insert(0, str(ROOT / folder))
import gps_server as vehicle
from race_runtime import RaceRuntime


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url):
    for _ in range(100):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("HTTP startup timeout")


def no_overflow(page):
    assert page.evaluate("""() => document.documentElement.scrollWidth <= innerWidth
        && document.documentElement.scrollHeight <= innerHeight"""), "800x480 overflow"
    for selector in ("#speed", "#contentValue", "#metrics", "nav"):
        box = page.locator(selector).bounding_box()
        assert box and box["x"] >= 0 and box["y"] >= 0
        assert box["x"] + box["width"] <= 800.5 and box["y"] + box["height"] <= 480.5


def main():
    qa = ROOT / ".qa"
    qa.mkdir(exist_ok=True)
    failures = []
    with tempfile.TemporaryDirectory(prefix="unimotors-qa-") as temp:
        server_port = free_port()
        env = dict(
            os.environ,
            UNIMOTORS_SERVER_PORT=str(server_port),
            UNIMOTORS_SERVER_DB=str(Path(temp) / "server.db"),
            UNIMOTORS_TOKEN="qa-local-only",
            NO_PROXY="127.0.0.1,localhost",
        )
        log = (qa / "browser-server.log").open("w", encoding="utf8")
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "server/telemetry_server.py")],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        runtime = None
        http = None
        workers = []
        stop = threading.Event()
        try:
            pit_url = f"http://127.0.0.1:{server_port}"
            wait_http(pit_url + "/api/status")
            runtime = RaceRuntime(
                Path(temp) / "vehicle",
                battery_epoch="qa-pack",
                boot="qa-boot",
                time_trusted=lambda: True,
            )
            runtime.run.update(used_wh=280, distance_km=17.1, lap_count=12)
            runtime.configure(
                dict(
                    event="endurance",
                    total_laps=40,
                    confirmed=True,
                    usable_remaining_wh=1200,
                    reserve_wh=200,
                    source_version="synthetic QA",
                )
            )
            runtime.run["laps"] = [
                dict(seconds=55, wh=31.8, complete=True, formation=False)
            ]
            runtime.driver(
                "driver1", dict(mode="base", brightness=1, contrast="normal")
            )
            runtime.set_phase("RACE")
            fixture = dict(speed=42, danger=False)
            vehicle.RUNTIME, vehicle.STOP = runtime, stop
            vehicle.UPLOAD_TOKEN, vehicle.UPLOAD_ENABLED = "qa-local-only", True
            vehicle.SERVER_WS_URL = pit_url.replace("http:", "ws:") + "/ingest"
            vehicle.UPLOAD_RATE, vehicle.SSE_RATE, vehicle.REPLAY = 0.1, 0.1, False
            os.environ["NO_PROXY"] = "127.0.0.1,localhost"

            def sampler():
                while not stop.is_set():
                    runtime.update_gps(
                        dict(
                            fix=True,
                            lat=35,
                            lon=129,
                            speed=fixture["speed"],
                            sats=12,
                            hdop=0.8,
                        )
                    )
                    runtime.update_bms(
                        dict(
                            voltage=50,
                            current=14,
                            soc=76,
                            temp_max=34,
                            temp_min=31,
                            cell_v_diff=12,
                            faults={
                                "dangers": ["시험 과열 경고"]
                                if fixture["danger"]
                                else [],
                                "warnings": [],
                            },
                        )
                    )
                    runtime.tick()
                    stop.wait(0.1)

            http = ThreadingHTTPServer(("127.0.0.1", 0), vehicle.Handler)
            for target in (sampler, vehicle.uploader, http.serve_forever):
                worker = threading.Thread(target=target, daemon=True)
                worker.start()
                workers.append(worker)
            driver_url = f"http://127.0.0.1:{http.server_port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 800, "height": 480})
                page = context.new_page()
                page.on("pageerror", lambda e: failures.append(str(e)))
                page.goto(driver_url)
                page.wait_for_function(
                    "document.querySelector('#speed').textContent==='42'"
                )
                for mode in ("base", "endurance", "pit"):
                    if mode == "pit":
                        fixture["speed"] = 0
                        page.wait_for_function(
                            "document.querySelector('#speed').textContent==='0'"
                        )
                    page.locator(f'[data-mode="{mode}"]').click()
                    page.wait_for_timeout(150)
                    no_overflow(page)
                    page.screenshot(path=str(qa / f"driver-{mode}.png"))
                page.locator("#settings").click()
                page.locator("#token").fill("qa-local-only")
                page.locator("#modeInput").select_option("endurance")
                page.locator("#contrast").select_option("high")
                page.locator("#setDriver").click()
                page.wait_for_function(
                    "document.documentElement.dataset.contrast==='high'"
                )
                page.locator("#close").click()
                assert (
                    page.locator('[data-mode="endurance"]').get_attribute(
                        "aria-pressed"
                    )
                    == "true"
                )
                runtime.set_phase("CHANGE")
                page.wait_for_function(
                    "document.querySelector('#phase').textContent==='드라이버 교체'"
                )
                page.screenshot(path=str(qa / "driver-change.png"))
                runtime.set_phase("RACE")
                fixture["danger"] = True
                page.wait_for_function(
                    "document.querySelector('#banner').className==='danger'"
                )
                page.screenshot(path=str(qa / "driver-warning.png"))
                fixture["danger"] = False
                pit_context = browser.new_context(
                    viewport={"width": 1280, "height": 1000}
                )
                pit = pit_context.new_page()
                pit.on("pageerror", lambda e: failures.append(str(e)))
                pit.goto(pit_url + "/research")
                pit.wait_for_function(
                    "document.querySelector('#speed').textContent==='0'"
                )
                pit.locator("#load").click()
                pit.wait_for_function(
                    "document.querySelector('#summary').textContent.includes('표본')"
                )
                pit.locator("#token").fill("qa-local-only")
                pit.locator("#message").fill("교체 준비 · 시험 메시지")
                pit.locator("#send").click()
                page.locator("#order").wait_for(state="visible")
                assert "TEAM" in page.locator("#orderText").inner_text()
                page.locator("#seen").click()
                page.locator("#order").wait_for(state="hidden")
                pit.wait_for_function(
                    "document.querySelector('#commands').textContent.includes('seen')"
                )
                pit.screenshot(path=str(qa / "pit-research.png"), full_page=True)
                # Existing UI is independently accessible for A/B comparison.
                legacy = context.new_page()
                legacy.goto(driver_url + "/legacy")
                assert "UNIMOTORS" in legacy.title()
                # Existing EventSource sockets can survive CDP offline mode.
                # Stop upstream emission as well to exercise the actual watchdog.
                stop.set()
                context.set_offline(True)
                page.wait_for_function(
                    "document.querySelector('#speed').textContent==='--'"
                )
                assert "확인 불가" in page.locator("#alert").inner_text()
                page.screenshot(path=str(qa / "driver-disconnected.png"))
                pit_context.set_offline(True)
                pit.wait_for_function(
                    "document.querySelector('#health').textContent.includes('관제 연결 지연')",
                    timeout=8000,
                )
                assert pit.locator("#speed").inner_text() == "--"
                browser.close()
            assert not failures, failures
            print(
                json.dumps(
                    dict(
                        status="passed",
                        viewport="800x480",
                        checks=[
                            "base/endurance/pit",
                            "same-driver settings",
                            "contrast",
                            "phase timer",
                            "BMS danger",
                            "TEAM WS delivery/driver seen",
                            "reports",
                            "SSE/network watchdog",
                            "legacy UI",
                        ],
                        screenshots=7,
                    )
                )
            )
        finally:
            stop.set()
            if http:
                http.shutdown()
                http.server_close()
            for worker in workers:
                worker.join(timeout=6)
            if runtime:
                runtime.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()


if __name__ == "__main__":
    main()
