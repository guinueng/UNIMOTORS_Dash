#!/usr/bin/env python3
"""Project CARS 1 UDP v1 -> v7 display, CSV and durable outbox.

Runs on the game PC or a machine on the same broadcast LAN. No vehicle hardware
reader is started. Game-native lap timing is kept separate from GPS lap detection.
"""
import argparse
import json
import math
import os
from pathlib import Path
import socket
import threading
import uuid

from pcars_protocol import UDP_PORT, SequenceWindow, decode_packet
from race_runtime import RaceRuntime


class GameRuntime(RaceRuntime):
    def __init__(self, directory, vehicle="pcars1", **kwargs):
        self.frame, self.context = {}, {}
        self.frame_at = None
        self.frame_index = 0
        self.context_changed = False
        self.receiver = {"source_ip": None, "accepted": 0, "invalid": 0,
                         "foreign": 0, "reordered": 0}
        super().__init__(directory, vehicle=vehicle, current_sign=1, **kwargs)
        # A game process cannot establish continuity with an earlier game session.
        self.new_race("game-" + uuid.uuid4().hex)
        self.configure({"source_version": "pcars1_udp_v1; game; no BMS",
                        "confirmed": True})

    def update_context(self, value):
        with self.lock:
            context = {k: value[k] for k in ("car", "car_class", "track", "variation")}
            if self.context and context != self.context:
                self.context_changed = True
            self.context = context

    def update_game(self, value):
        with self.lock:
            now, old = self.clock.monotonic(), self.frame
            if value["active"] and old:
                reset = (self.context_changed or
                         old["game_state"] in (0, 1, 4, 5) or
                         old["race_state"] in (3, 4, 5, 6) or
                         value["session_state"] != old["session_state"] or
                         value["viewed"] != old["viewed"] or
                         (value["laps_completed"] is not None and old["laps_completed"] is not None
                          and value["laps_completed"] < old["laps_completed"]) or
                         (self.frame_at is not None and now - self.frame_at > 5))
                if reset:
                    self.new_race("game-" + uuid.uuid4().hex)
                    self.configure({"source_version": "pcars1_udp_v1; game; no BMS", "confirmed": True})
                    self.event("game_session_boundary", {"reason": "context/state/lap/gap"})
            self.frame, self.frame_at = dict(value), now
            self.frame_index += 1
            if value["active"]:
                self.context_changed = False
            self.run["lap_count"] = value["laps_completed"] or 0

    def snapshot(self):
        with self.lock:
            s = super().snapshot()
            age = None if self.frame_at is None else self.clock.monotonic() - self.frame_at
            fresh = age is not None and 0 <= age <= 1.5
            f = self.frame
            active = fresh and f.get("active", False)
            status = "waiting" if not f else "stale" if not fresh else f["status"]
            if fresh and f["game_state"] == 2 and not f["active"]:
                status = {3: "finished", 4: "disqualified", 5: "retired", 6: "dnf"}.get(f["race_state"], "waiting")
            # Historical laps are explicitly separate; inactive frames never supply live values.
            live = dict(f) if active else {}
            s["input"] = dict(kind="game", transport="pcars1_udp_v1", status=status,
                              age_s=age, frame_index=self.frame_index, context=dict(self.context),
                              frame=live, receiver=dict(self.receiver))
            s["capabilities"] = dict(battery_measurement=False, gps=False, game=True)
            for key in ("used_wh", "used_ah", "distance_km", "gap_seconds", "measured_seconds",
                        "energy_increment_wh", "charge_increment_ah", "current_sign",
                        "current_convention", "race_elapsed", "change_elapsed", "restart_elapsed"):
                s[key] = None
            s.update(speed=live.get("speed_kmh"), g_lat=live.get("local_g_x"),
                     g_lon=live.get("local_g_z"), energy_quality="not_provided",
                     phase="RACE" if active else "GAME_" + status.upper(),
                     lap_count=f.get("laps_completed") if fresh else None,
                     lap_uncertain=not fresh or bool(f.get("lap_invalid")),
                     recovery="game_session", candidate=False,
                     budget={"status": "not_provided", "target_wh_lap": None})
            last, best = f.get("last_lap_s"), f.get("best_lap_s")
            s["lap_timing"] = dict(line=True, number=f.get("current_lap") if fresh else None,
                                   current=live.get("current_lap_s"), last=last, best=best,
                                   last_delta=None if last is None or best is None else last - best,
                                   interrupted=not active or bool(f.get("lap_invalid")))
            s["preflight"] = {k: v for k, v in s["preflight"].items() if k not in ("gps", "bms", "recovery")}
            s["preflight"]["game"] = active
            s["warnings"] = [w for w in s["warnings"] if w["source"] == "storage"]
            if not active:
                names = {"waiting": "게임 UDP 수신 대기", "stale": "게임 수신 끊김 · 현재 값 확인 불가",
                         "paused": "게임 일시정지", "menu": "게임 메뉴", "replay": "게임 리플레이",
                         "restarting": "게임 재시작", "finished": "게임 경기 종료"}
                s["warnings"].append(dict(priority=1, text=names.get(status, "게임 주행 대기 · " + status), source="game"))
            s["alarm_level"] = int(bool(s["warnings"]))
            s["alarms"] = " | ".join(w["text"] for w in s["warnings"])
            meta = json.loads(s["meta"])
            meta.update(input=s["input"], capabilities=s["capabilities"], recovery="game_session",
                        battery_epoch=None, energy_reasons=["game_does_not_supply_bms"],
                        lap_timing=s["lap_timing"])
            s["meta"] = json.dumps(meta, ensure_ascii=False, allow_nan=False)
            return s

    def report(self):
        result = super().report()
        for key in ("used_wh", "used_ah", "distance_km", "measured_seconds", "gap_seconds"):
            result["race"][key] = None
        result.update(input=self.snapshot()["input"],
                      warnings="Project CARS 1 game study; BMS/energy/GPS not provided; not real-vehicle validation")
        return result


class DatagramInput:
    def __init__(self, runtime, source_ip=None):
        self.runtime, self.source_ip = runtime, source_ip
        self.window = SequenceWindow()
        self.last_packet = None

    def receive(self, data, address):
        r = self.runtime
        with r.lock:
            if self.source_ip and address[0] != self.source_ip:
                r.receiver["foreign"] += 1
                return False
            try:
                value = decode_packet(data)
            except (ValueError, TypeError):
                r.receiver["invalid"] += 1
                return False
            if value["type"] == "telemetry":
                if data == self.last_packet or not self.window.accept(value["sequence"], r.clock.monotonic()):
                    r.receiver["reordered"] += 1
                    return False
                self.last_packet = bytes(data)
                # Latch one sender until restart; unrelated LAN senders cannot take over.
                self.source_ip = address[0]
                r.receiver["source_ip"] = self.source_ip
                r.update_game(value)
                r.receiver["accepted"] += 1
                return True
            if value["type"] == "context" and self.source_ip == address[0]:
                r.update_context(value)
            return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--udp-bind", default="0.0.0.0")
    parser.add_argument("--udp-port", type=int, default=UDP_PORT)
    parser.add_argument("--source", help="Accept only this game PC's IPv4 address")
    parser.add_argument("--multicast-interface", default="0.0.0.0", help="Local IPv4 for 224.0.0.1 membership (default OS route)")
    parser.add_argument("--bind", default="127.0.0.1", help="HTTP address; 0.0.0.0 for phone/tablet")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--vehicle", default="pcars1")
    parser.add_argument("--log-dir", default="pcars_logs")
    parser.add_argument("--server", help="v7 pit WebSocket URL; requires --upload")
    parser.add_argument("--upload", action="store_true", help="Enable durable pit upload (off by default)")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--sample-rate", type=float, default=0.2, help="CSV interval in seconds")
    args = parser.parse_args()
    if not (0 <= args.port <= 65535 and 0 <= args.udp_port <= 65535):
        parser.error("ports must be 0..65535")
    if not math.isfinite(args.sample_rate) or args.sample_rate <= 0:
        parser.error("sample-rate must be finite and positive")
    if args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        parser.error("duration must be finite and positive")
    if not args.vehicle.startswith("pcars1") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.vehicle):
        parser.error("game vehicle ID must begin pcars1 and contain only letters/digits/_/-")
    for address in (args.source, args.multicast_interface):
        if not address:
            continue
        try:
            socket.inet_pton(socket.AF_INET, address)
        except OSError:
            parser.error("source/multicast-interface must be an IPv4 address")
    import gps_server as gs
    gs.STOP.clear()
    gs.LOG_DIR = str(Path(args.log_dir).resolve())
    gs.VEHICLE_ID, gs.UPLOAD_ENABLED = args.vehicle, args.upload
    gs.TELEM_LOG_RATE, gs.SSE_RATE = args.sample_rate, 0.1
    if args.server:
        gs.SERVER_WS_URL = args.server
    if args.upload and (not gs.WS_AVAILABLE or gs.UPLOAD_TOKEN in ("change-me", "unimotors-secret-change-me")):
        parser.error("upload needs websocket-client and a configured UNIMOTORS_TOKEN")
    state = Path(os.environ.get("UNIMOTORS_STATE_DIR", str(Path(gs.LOG_DIR) / "state"))) / "pcars1"
    rt = gs.RUNTIME = GameRuntime(state, vehicle=args.vehicle)
    gs.SESSION_ID = rt.segment
    receiver = DatagramInput(rt, args.source)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv = None
    workers = []
    deadline = None
    try:
        # Exclusive bind surfaces port conflicts instead of splitting telemetry.
        udp.bind((args.udp_bind, args.udp_port))
        if args.udp_bind == "0.0.0.0":
            udp.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                           socket.inet_aton("224.0.0.1") + socket.inet_aton(args.multicast_interface))
        udp.settimeout(0.2)
        srv = gs.ThreadingHTTPServer((args.bind, args.port), gs.Handler)
        print(f"PCARS1 UDP {args.udp_bind}:{udp.getsockname()[1]} -> http://{args.bind}:{srv.server_address[1]}", flush=True)
        print(f"Game study only · no BMS/GPS · logs={gs.LOG_DIR} · state={state} · upload={args.upload}", flush=True)

        def loop():
            while not gs.STOP.is_set():
                try:
                    data, address = udp.recvfrom(4096)
                except socket.timeout:
                    continue
                receiver.receive(data, address)

        workers = [threading.Thread(target=t, daemon=True) for t in (loop, gs.csv_logger, gs.uploader)]
        for worker in workers:
            worker.start()
        if args.duration is not None:
            deadline = threading.Timer(args.duration, srv.shutdown)
            deadline.daemon = True
            deadline.start()
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        gs.STOP.set()
        if deadline:
            deadline.cancel()
        if srv:
            srv.server_close()
        for worker in workers:
            worker.join(timeout=5)
        udp.close()
        rt.close()


if __name__ == "__main__":
    main()
