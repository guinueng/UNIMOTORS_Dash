#!/usr/bin/env python3
"""UNIMOTORS v7 클러스터 + 로컬 기록 + durable telemetry.

v7: restart continuity, freshness, local race state and ACK-backed delivery.
See docs/19-cluster-research.md; the following v6 notes describe inherited features.

v5 대비 변경점:
  - GPS 위경도(lat/lon) 추출 추가 (GGA) -> 지도/궤적/서버 전송에 사용
  - G-force 계산 추가 (GPS 속도/방향 미분 -> 종G/횡G). IMU 붙이면 소스만 교체.
  - 서버 전송(uploader) 스레드 추가: telemetry_snapshot 을 WS로 서버에 push.
    * VPN 내부(10.10.0.x) 평문 WS. 끊기면 자동 재연결. 오프라인이어도 로컬 CSV 지속.
  - 모든 레이트를 파일 상단 상수로 (3B+ 이관 후 숫자만 조정).

설계 철학(계승): 차량 Pi는 '빨리 읽고 그대로 저장/전송'. 무거운 분석은 서버에서.
  - 10Hz raw NMEA: 로컬 SD 원본 (정밀 궤적/랩분석용, 주행후 업로드)
  - 기본 5Hz telem CSV: GPS+BMS 요약 (백업/보정)
  - NHz 계기판 표시: 폰/LCD (SSE)
  - NHz 서버 push: WS로 서버 (실시간 대시보드)

의존성: pyserial(python3-serial), python-can(BMS), websocket-client(uploader),
        bms_reader.py 동일 폴더.
        websocket-client 설치: pip3 install websocket-client --break-system-packages
"""

import glob
import argparse
import csv
import hmac
import json
import math
import queue
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import serial
except ImportError:
    serial = None
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
from telemetry_protocol import FIELDS as V7_FIELDS, parse_csv, VERSION
from race_runtime import RaceRuntime

RUNTIME = None
STOP = threading.Event()
REPLAY = False
RAW_QUEUE = queue.Queue(maxsize=5000)

# BMS 모듈 (없거나 python-can 미설치여도 계기판은 떠야 하므로 안전 임포트)
try:
    from bms_reader import BMSReader

    BMS_AVAILABLE = True
except Exception as _e:
    BMS_AVAILABLE = False
    _BMS_IMPORT_ERR = _e

# WebSocket 클라이언트 (없어도 계기판/로컬은 동작. 전송만 비활성)
try:
    import websocket  # websocket-client 패키지

    WS_AVAILABLE = True
except Exception as _we:
    WS_AVAILABLE = False
    _WS_IMPORT_ERR = _we

# ============================================================
#  설정 (3B+ 이관 후 여기 숫자만 조정하면 됨)
# ============================================================
#  ★ 차량 프리셋 — 이 기기가 무슨 차인지만 아래 PRESET 에서 고르면 됨.
#    새 개인차는 PRESETS 에 항목 추가. (env UNIMOTORS_PRESET 로도 지정 가능)
# ============================================================
PRESETS = {
    # 레이싱카: BMS 있음, 서버 전송, 계기판
    "car1": {
        "VEHICLE_ID": "car1",
        "BMS_ENABLED": True,
        "UPLOAD_ENABLED": True,
        "GPS_PORT": "/dev/ttyUSB1",
    },
    # 개인차(스타렉스): BMS 없음, 서버 전송 O, 계기판+로깅
    "starex": {
        "VEHICLE_ID": "starex",
        "BMS_ENABLED": False,
        "UPLOAD_ENABLED": True,
        "GPS_PORT": "/dev/ttyUSB1",
    },
    # 개인차 예비(다른 차 추가용 템플릿)
    "car_personal2": {
        "VEHICLE_ID": "car_personal2",
        "BMS_ENABLED": False,
        "UPLOAD_ENABLED": True,
        "GPS_PORT": "/dev/ttyUSB1",
    },
    # 순수 로컬 로거(서버 전송 안 함, BMS 없음) — 프라이버시/오프라인
    "local_only": {
        "VEHICLE_ID": "local",
        "BMS_ENABLED": False,
        "UPLOAD_ENABLED": False,
        "GPS_PORT": "/dev/ttyUSB1",
    },
}
# ★ 이 기기가 무슨 차인지 여기서 선택 (또는 환경변수 UNIMOTORS_PRESET)
PRESET = os.environ.get("UNIMOTORS_PRESET", "car1")
_P = PRESETS.get(PRESET, PRESETS["car1"])

PORT = int(os.environ.get("UNIMOTORS_VEHICLE_PORT", "8080"))
GPS_PORT = _P["GPS_PORT"]  # 3B+ 이관 후 ls /dev/ttyUSB* 로 재확인 필요
GPS_BAUD = 115200

CAN_CHANNEL = "can0"
BMS_ENABLED = _P["BMS_ENABLED"]

LOG_DIR = os.environ.get("UNIMOTORS_LOG_DIR", "/home/unimotors/gps_logs")
KEEP_FILES = 3
FLUSH_INTERVAL = 1.0

# --- 표시/전송 레이트 (초 단위 주기) ---
# Zero W: 1.0 권장. 3B+/4B: 0.1~0.2 로 낮춰 부드러운 계기판.
DISPLAY_RATE = float(os.environ.get("UNIMOTORS_DISPLAY_RATE", "0.1"))
SSE_RATE = float(os.environ.get("UNIMOTORS_SSE_RATE", "0.2"))
UPLOAD_RATE = 0.5  # 서버 WS push 주기 (2Hz). 상수로 조정.

SPEED_CUTOFF = 2.0
HEADING_MIN_SPEED = 5.0
SPEED_WINDOW = 3

# --- 캡티브 포털 응답 방식 ---
#  "portal"  : 302 리다이렉트 → 폰이 '로그인 필요(캡티브)'로 인식.
#              폰은 셀룰러로 인터넷을 계속 쓰고, WiFi는 계기판 접속에만 사용. (NAT 없을 때 권장)
#  "success" : 204/Success → 폰이 '인터넷 된다'고 판단.
#              ⚠️ LTE 인터넷 공유(NAT)를 켠 경우에만 쓸 것.
#              NAT 없이 쓰면 폰이 모든 트래픽을 WiFi로 보내 인터넷이 끊긴다.
#  "off"     : 캡티브 경로 응답 안 함(404)
CAPTIVE_MODE = "portal"
CAPTIVE_REDIRECT = "http://10.42.0.1:8080/"  # portal 모드에서 안내할 주소

# --- BMS 폴링 주기 ---
BMS_FAST_INTERVAL = 0.33
BMS_SLOW_INTERVAL = 1.0
BMS_ALARM_INTERVAL = 0.5

# --- 텔레메트리 로컬 CSV ---
# ★ 중요: 이 telem CSV 가 backfill.py 로 서버에 올라가 'track'(정밀 궤적)이 된다.
#   즉 서버 궤적 해상도 = TELEM_LOG_RATE.  (gps_*.csv 의 10Hz raw NMEA 는 업로드 안 됨)
#   1.0 = 1Hz (기본, 가벼움) / 0.2 = 5Hz / 0.1 = 10Hz (정밀 분석용, SD 쓰기 10배)
TELEM_ENABLED = True
TELEM_LOG_RATE = float(os.environ.get("UNIMOTORS_SAMPLE_RATE", "0.2"))
TELEM_FLUSH = 2.0

# --- 서버 전송 (WebSocket, VPN 내부) ---
UPLOAD_ENABLED = _P["UPLOAD_ENABLED"]
SERVER_WS_URL = os.environ.get("UNIMOTORS_SERVER_WS", "ws://10.10.0.9:8090/ingest")
UPLOAD_TOKEN = os.environ.get(
    "UNIMOTORS_TOKEN", "change-me"
)  # 간단 인증 토큰 (서버와 일치시킬 것)
VEHICLE_ID = _P["VEHICLE_ID"]  # 다중 차량 식별자 (프리셋에서)

# --- G-force 계산 ---
G_ENABLED = True
G_ACCEL = 9.80665  # 1G (m/s^2)
G_MIN_SPEED = 3.0  # km/h. 이 미만은 G 계산 안 함(저속 GPS 노이즈 방지)

# CSV/전송 공통 스키마 (서버 테이블과 동일하게)
TELEM_FIELDS = [
    "ts",
    "vehicle",
    "session",
    "t_mono",  # t_mono: 시작 후 경과초(시계와 무관)
    "lat",
    "lon",
    "alt",
    "speed",
    "heading",
    "sats",
    "fix",
    "hdop",
    "g_lon",
    "g_lat",  # G-force (종/횡)
    "voltage",
    "current",
    "soc",
    "power_w",
    "regen",
    "remain_ah",
    "range_km",
    "state",
    "used_ah",
    "used_wh",  # 이번 주행 누적 사용량
    "temp_max",
    "temp_min",
    "cell_v_max",
    "cell_v_min",
    "cell_v_diff",
    "balancing",
    "alarm_level",
    "alarms",
]

KST = timezone(timedelta(hours=9))
DEBUG = ("--debug" in sys.argv) or (os.environ.get("GPS_DEBUG") == "1")

# ============================================================
#  ★ 시계 신뢰성 (RTC 없는 Pi 대응)
#  콜드 부팅 시 시스템 시계는 '마지막 종료 시각'에서 시작하고, 나중에
#  NTP(LTE)나 GPS 로 갱신되면서 훌쩍 점프한다. 그러면 ts 기반 주행시간이
#  뻥튀기된다. -> 경과시간은 monotonic 으로 따로 기록하고(t_mono),
#     점프를 감지하면 세션을 새로 끊어 오염 구간을 분리한다.
# ============================================================
T0_MONO = time.monotonic()  # 프로세스 시작 기준 (시계와 무관, 절대 뒤로 안 감)
CLOCK_JUMP_SEC = 30.0  # 이 이상 어긋나면 시계 점프로 판단
SESSION_ID = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
clock_state = {
    "mono": T0_MONO,  # 마지막 점검 시 monotonic
    "wall": time.time(),  # 마지막 점검 시 wall clock
    "jumps": 0,  # 점프 감지 횟수
    "synced": False,  # 점프(=NTP/GPS 동기) 한 번이라도 있었나
}
clock_lock = threading.Lock()


def check_clock_jump():
    """시계 점프 감지. 점프 시 세션 ID 를 새로 끊고 True 반환.
    monotonic 경과분과 wall clock 경과분을 비교해서 판단한다."""
    global SESSION_ID
    now_m = time.monotonic()
    now_w = time.time()
    with clock_lock:
        dm = now_m - clock_state["mono"]  # 실제 흐른 시간
        dw = now_w - clock_state["wall"]  # 시계가 주장하는 시간
        clock_state["mono"] = now_m
        clock_state["wall"] = now_w
        if abs(dw - dm) > CLOCK_JUMP_SEC:
            clock_state["jumps"] += 1
            clock_state["synced"] = True
            new_sess = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
            old = SESSION_ID
            SESSION_ID = new_sess
            print(
                f"[CLOCK] 시계 점프 감지 ({dw - dm:+.0f}s) -> 세션 분리 "
                f"{old} -> {new_sess}",
                flush=True,
            )
            return True
    return False


# 계기판 state (SSE로 폰/LCD에 나감)
state = {
    "speed": 0,
    "heading": None,
    "compass": "--",
    "utc": None,
    "fix": False,
    "sats": 0,
    "hdop": None,
    "lat": None,
    "lon": None,
    "alt": None,
    "g_lon": 0.0,
    "g_lat": 0.0,
    "bms": None,
    "bms_ok": False,
}
state_lock = threading.Lock()

# 배터리 누적 사용량 (이번 주행). 전류/전력 적분.
usage = {"used_ah": 0.0, "used_wh": 0.0, "last_t": None}
usage_lock = threading.Lock()

latest = {"gga": None, "vtg": None}
latest_lock = threading.Lock()
latest_times = {"gga": None, "vtg": None}

COMPASS_16 = [
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
]


def heading_to_compass(deg):
    return COMPASS_16[int((deg + 11.25) % 360 / 22.5)]


def dbg(*a):
    if DEBUG:
        print("[GPS]", *a, flush=True)


def rotate_logs(prefix):
    os.makedirs(LOG_DIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(LOG_DIR, f"{prefix}_*.csv")))
    # A CSV is eligible only after complete, explicit server acknowledgement.
    # Raw NMEA and unacknowledged CSVs are not silently evicted on reboot.
    eligible = []
    for filename in files:
        try:
            info = json.loads(Path(filename + ".acked").read_text(encoding="utf8"))
            stat = Path(filename).stat()
            if (
                info["bytes"] == stat.st_size
                and info["mtime_ns"] == stat.st_mtime_ns
                and not Path(filename + ".active").exists()
            ):
                eligible.append(filename)
        except (OSError, ValueError, KeyError):
            pass
    for f in eligible[: max(0, len(files) - (KEEP_FILES - 1))]:
        try:
            os.remove(f)
            dbg("삭제:", os.path.basename(f))
        except OSError:
            pass


# ============================================================
#  GPS 읽기 (10Hz raw 로깅 + 최신 GGA/VTG 보관) — v5와 동일
# ============================================================
def raw_logger():
    handle = None
    try:
        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        handle = open(
            os.path.join(LOG_DIR, f"gps_{SESSION_ID}.csv"), "a", buffering=8192
        )
        last_flush = time.monotonic()
        while not STOP.is_set() or not RAW_QUEUE.empty():
            try:
                raw = RAW_QUEUE.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                handle.write(raw + "\n")
                if time.monotonic() - last_flush >= FLUSH_INTERVAL:
                    handle.flush()
                    last_flush = time.monotonic()
            except OSError as exc:
                RUNTIME.raw_error = str(exc)[:120]
    except OSError as exc:
        RUNTIME.raw_error = str(exc)[:120]
    finally:
        if handle:
            try:
                handle.close()
            except OSError:
                pass


def gps_reader():
    while not STOP.is_set():
        ser = None
        try:
            if serial is None:
                raise RuntimeError("pyserial is not installed")
            ser = serial.Serial(GPS_PORT, GPS_BAUD, timeout=1)
            while not STOP.is_set():
                raw = ser.readline().decode("ascii", errors="replace").strip()
                if len(raw) < 6 or raw[0] != "$":
                    continue
                if "*" in raw:
                    payload, checksum = raw[1:].rsplit("*", 1)
                    value = 0
                    for char in payload:
                        value ^= ord(char)
                    try:
                        if value != int(checksum[:2], 16):
                            continue
                    except ValueError:
                        continue
                now = time.monotonic()
                tag = raw[3:6]
                if tag in ("GGA", "VTG"):
                    key = tag.lower()
                    with latest_lock:
                        latest[key] = raw
                        latest_times[key] = now
                try:
                    RAW_QUEUE.put_nowait(raw)
                except queue.Full:
                    RUNTIME.raw_dropped += 1
                    RUNTIME.raw_error = "raw queue full"
        except Exception as exc:
            dbg("GPS reconnect:", type(exc).__name__)
            STOP.wait(1)
        finally:
            if ser is not None:
                ser.close()


def _nmea_to_deg(val, hemi):
    """NMEA ddmm.mmmm -> 십진도. lat은 dd, lon은 ddd."""
    if not val or "." not in val:
        return None
    try:
        dot = val.index(".")
        deg_len = dot - 2  # 분은 항상 2자리+소수
        d = float(val[:deg_len])
        m = float(val[deg_len:])
        dec = d + m / 60.0
        if hemi in ("S", "W"):
            dec = -dec
        return round(dec, 6)
    except (ValueError, IndexError):
        return None


def parse_gga(line):
    """$..GGA,hhmmss.ss,lat,N,lon,E,fixq,numsat,hdop,alt,M,...
    fix 여부 + 위성수 + 시각 + 위경도 + 고도(m) + HDOP 반환."""
    p = line.split(",")
    if len(p) < 8:
        return None
    try:
        fixq = int(p[6]) if p[6] else 0
        sats = int(p[7]) if p[7] else 0
    except ValueError:
        return None
    t = p[1]
    utc = f"{t[0:2]}:{t[2:4]}:{t[4:6]}" if len(t) >= 6 else None
    lat = _nmea_to_deg(p[2], p[3]) if fixq >= 1 else None
    lon = _nmea_to_deg(p[4], p[5]) if fixq >= 1 else None
    # 고도(p[9], 단위 p[10]='M') + HDOP(p[8]) — fix 있을 때만 유효
    alt = None
    hdop = None
    if fixq >= 1:
        try:
            alt = float(p[9]) if len(p) > 9 and p[9] else None
        except ValueError:
            alt = None
        try:
            hdop = float(p[8]) if len(p) > 8 and p[8] else None
        except ValueError:
            hdop = None
    return {
        "fix": fixq >= 1,
        "sats": sats,
        "utc": utc,
        "lat": lat,
        "lon": lon,
        "alt": alt,
        "hdop": hdop,
    }


def parse_vtg(line):
    """$..VTG,course_true,T,...,spd_kmh,K,mode  ->  속도(km/h)+진북방향."""
    p = line.split(",")
    if len(p) < 8:
        return None
    try:
        course = float(p[1]) if p[1] else None
        spd_kmh = float(p[7]) if p[7] else None
    except ValueError:
        return None
    return {"spd_kmh": spd_kmh, "course": course}


# ============================================================
#  표시 갱신 + G-force 계산
# ============================================================
def _calc_gforce(prev, cur, dt):
    """이전/현재 (speed_kmh, heading_deg) 로 종G/횡G 추정.
    종G = 가감속 (속도 변화율), 횡G = 코너링 (속도 x 방향변화율).
    GPS 기반이라 근사. IMU 붙이면 이 함수 대신 IMU 값 사용."""
    if dt <= 0 or prev is None:
        return 0.0, 0.0
    v_prev = prev[0] / 3.6  # km/h -> m/s
    v_cur = cur[0] / 3.6
    # 종G: dv/dt
    g_lon = (v_cur - v_prev) / dt / G_ACCEL
    # 횡G: v * (dheading/dt in rad)
    g_lat = 0.0
    if prev[1] is not None and cur[1] is not None and cur[0] >= G_MIN_SPEED:
        dh = cur[1] - prev[1]
        # -180~180 로 정규화 (방향 wrap 처리)
        dh = (dh + 180) % 360 - 180
        omega = math.radians(dh) / dt  # rad/s
        g_lat = (v_cur * omega) / G_ACCEL
    return round(g_lon, 2), round(g_lat, 2)


def display_updater():
    previous = filtered = None
    seen_position = seen_speed = None
    while not STOP.wait(DISPLAY_RATE):
        with latest_lock:
            raw = dict(latest)
            stamps = dict(latest_times)
        now = time.monotonic()
        g = parse_gga(raw["gga"]) if raw["gga"] else None
        v = parse_vtg(raw["vtg"]) if raw["vtg"] else None
        gps_ok = g and stamps["gga"] is not None and now - stamps["gga"] <= 1.5
        speed_ok = v and stamps["vtg"] is not None and now - stamps["vtg"] <= 1.5
        with state_lock:
            if gps_ok:
                state.update(g)
            else:
                state.update(fix=False, lat=None, lon=None, alt=None)
            if not (gps_ok and speed_ok and g["fix"] and v["spd_kmh"] is not None):
                filtered = previous = None
                state.update(speed=None, heading=None, g_lon=None, g_lat=None)
            elif seen_speed != stamps["vtg"]:
                dt = stamps["vtg"] - seen_speed if seen_speed is not None else 0.1
                alpha = 1 - math.exp(-max(0.001, dt) / 0.25)
                filtered = (
                    v["spd_kmh"]
                    if filtered is None
                    else filtered + alpha * (v["spd_kmh"] - filtered)
                )
                state["speed_raw"] = v["spd_kmh"]
                state["speed"] = 0 if filtered < SPEED_CUTOFF else round(filtered, 1)
                state["heading"] = (
                    v["course"] if filtered >= HEADING_MIN_SPEED else None
                )
                state["compass"] = (
                    heading_to_compass(v["course"])
                    if state["heading"] is not None
                    else "--"
                )
                state["g_lon"], state["g_lat"] = _calc_gforce(
                    previous, (filtered, v["course"]), dt
                )
                previous = (filtered, v["course"])
                seen_speed = stamps["vtg"]
            snapshot = dict(state)
        if RUNTIME:
            at = (
                min(x for x in stamps.values() if x is not None)
                if any(x is not None for x in stamps.values())
                else now
            )
            RUNTIME.update_gps(
                snapshot,
                at,
                position_updated=seen_position != stamps["gga"],
                position_time=stamps["gga"],
            )
            seen_position = stamps["gga"]


def bms_poller():
    if not BMS_ENABLED:
        dbg("BMS 비활성")
        return
    if not BMS_AVAILABLE:
        print(f"[BMS] 모듈 임포트 실패 -> BMS 없이 동작: {_BMS_IMPORT_ERR}", flush=True)
        return
    bms_state = {}
    while not STOP.is_set():
        bms = BMSReader(CAN_CHANNEL)
        try:
            bms.open()
            print("[BMS] can0 연결 성공", flush=True)
            with state_lock:
                state["bms_ok"] = True
            t_fast = t_slow = t_alarm = 0.0
            while not STOP.is_set():
                now = time.monotonic()
                changed = False
                if now - t_fast >= BMS_FAST_INTERVAL:
                    r = bms.read_fast()
                    if r:
                        bms_state.update(r)
                        changed = True
                        if RUNTIME:
                            RUNTIME.update_bms(r, r.get("_received"))
                    t_fast = now
                if now - t_slow >= BMS_SLOW_INTERVAL:
                    r = bms.read_slow()
                    if r:
                        bms_state.update(r)
                        changed = True
                        if RUNTIME:
                            RUNTIME.update_bms(r, r.get("_received"))
                    t_slow = now
                if now - t_alarm >= BMS_ALARM_INTERVAL:
                    fa = bms.read_alarms()
                    if fa:
                        bms_state["faults"] = fa
                        changed = True
                        if RUNTIME:
                            RUNTIME.update_bms({"faults": fa})
                    t_alarm = now
                if changed:
                    with state_lock:
                        state["bms"] = dict(bms_state)
                time.sleep(0.05)
        except Exception as e:
            print(f"[BMS] 오류 -> 3초 후 재연결: {e}", flush=True)
            with state_lock:
                state["bms_ok"] = False
            try:
                bms.close()
            except Exception:
                pass
            time.sleep(3)


# ============================================================
#  텔레메트리 스냅샷 (로깅 + 전송 공유)
# ============================================================
def telemetry_snapshot():
    if RUNTIME is not None:
        return RUNTIME.snapshot()
    return {}


def _csv_escape(v):
    if v is None:
        return ""
    s = str(v)
    if "," in s or '"' in s or "\n" in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def csv_logger():
    handle = writer = None
    path = Path(LOG_DIR) / f"telem_{SESSION_ID}.csv"
    active = Path(str(path) + ".active")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_logs("telem")
        handle = path.open("a", newline="", encoding="utf8")
        writer = csv.DictWriter(handle, fieldnames=V7_FIELDS, extrasaction="ignore")
        writer.writeheader()
        active.write_text(SESSION_ID, encoding="utf8")
    except OSError as exc:
        RUNTIME.csv_error = str(exc)[:120]
    deadline = time.monotonic()
    try:
        while not STOP.is_set():
            snap = RUNTIME.tick()
            if writer is not None:
                try:
                    writer.writerow(snap)
                    handle.flush()
                    RUNTIME.csv_error = None
                except OSError as exc:
                    RUNTIME.csv_error = str(exc)[:120]
            deadline += TELEM_LOG_RATE
            remaining = deadline - time.monotonic()
            if remaining < 0:
                RUNTIME.deadline_misses += 1
                deadline = time.monotonic()
            STOP.wait(max(0, remaining))
    finally:
        if handle:
            handle.close()
        try:
            active.unlink()
        except OSError:
            pass


def uploader():
    if not UPLOAD_ENABLED or not WS_AVAILABLE or REPLAY:
        return
    while not STOP.is_set():
        ws = None
        try:
            ws = websocket.create_connection(
                SERVER_WS_URL, header=[f"X-Auth-Token: {UPLOAD_TOKEN}"], timeout=5
            )
            while not STOP.is_set():
                with RUNTIME.lock:
                    live = dict(RUNTIME.latest) if RUNTIME.latest else {}
                records = RUNTIME.outbox(40)
                if live:
                    records = [live] + [
                        r for r in records if r["sample_id"] != live["sample_id"]
                    ]
                ws.send(
                    json.dumps(
                        {
                            "type": "batch",
                            "records": records,
                            "events": RUNTIME.events_outbox(),
                            "receipts": RUNTIME.command_receipts(),
                        },
                        ensure_ascii=False,
                        allow_nan=False,
                    )
                )
                reply = json.loads(ws.recv())
                if reply.get("type") != "ack":
                    raise ValueError("v7 durable acknowledgement required")
                RUNTIME.acknowledge(reply.get("accepted", []))
                RUNTIME.acknowledge_events(reply.get("event_ids", []))
                for command in reply.get("commands", []):
                    try:
                        RUNTIME.receive_command(command)
                    except ValueError as exc:
                        RUNTIME.event(
                            "command_rejected",
                            {"id": command.get("id"), "reason": str(exc)},
                        )
                STOP.wait(UPLOAD_RATE)
        except Exception as exc:
            dbg("upload reconnect:", type(exc).__name__)
            STOP.wait(3)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass


class Handler(BaseHTTPRequestHandler):
    # ★ HTTP/1.1 로 응답해야 nginx 리버스 프록시(proxy_http_version 1.1)와 맞는다.
    #   1.0 + Connection:keep-alive + Content-Length 없음 = 응답 끝을 알 수 없어
    #   nginx 가 SSE 를 흘려보내지 못한다(계기판이 도메인에서 멈추는 원인).
    protocol_version = "HTTP/1.1"
    timeout = 30  # keep-alive 커넥션이 스레드를 물고 있지 않도록

    def log_message(self, *a):
        pass

    def do_GET(self):
        global DEBUG
        p = urlsplit(self.path).path
        if p in (
            "/generate_204",
            "/gen_204",
            "/hotspot-detect.html",
            "/library/test/success.html",
            "/connecttest.txt",
            "/ncsi.txt",
        ):
            self._captive(p)
            return
        if p == "/debug/on":
            DEBUG = True
            self._text("debug ON")
            return
        if p == "/debug/off":
            DEBUG = False
            self._text("debug OFF")
            return
        if p == "/debug":
            self._text(f"debug is {'ON' if DEBUG else 'OFF'}")
            return
        if p in ("/", "/index.html"):
            self._html()
        elif p == "/legacy":
            self._html(legacy=True)
        elif p == "/api/state":
            self._json(telemetry_snapshot())
        elif p == "/api/report":
            self._json(RUNTIME.report())
        elif p == "/stream":
            self._stream()
        else:
            self.send_error(404)

    def _json(self, value, code=200):
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in (
            "/api/control",
            "/api/command/seen",
            "/api/measurements/external",
        ):
            self._json({"error": "not found"}, 404)
            return
        token = self.headers.get("X-Auth-Token", "")
        if UPLOAD_TOKEN in (
            "change-me",
            "unimotors-secret-change-me",
        ) or not hmac.compare_digest(token, UPLOAD_TOKEN):
            self._json({"error": "configured token required"}, 401)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 32768:
                raise ValueError("invalid body size")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            if path == "/api/measurements/external":
                RUNTIME.update_external_vi(body)
            elif path == "/api/command/seen":
                RUNTIME.acknowledge_command(body["id"])
            else:
                snap = RUNTIME.snapshot()
                speed = snap.get("speed")
                if speed is not None and speed >= 2:
                    raise ValueError("stop before changing settings")
                if speed is None and body.get("confirm_stationary") is not True:
                    raise ValueError("stationary confirmation required")
                action = body.get("action")
                if action == "resume":
                    RUNTIME.resume(body.get("same_battery") is True)
                elif action == "new_race":
                    RUNTIME.new_race(body.get("battery_epoch"))
                elif action == "profile":
                    RUNTIME.configure(body.get("profile", {}))
                elif action == "phase":
                    RUNTIME.set_phase(body.get("phase"))
                elif action == "driver":
                    RUNTIME.driver(body.get("driver_id"), body.get("settings"))
                elif action == "lap_correction":
                    RUNTIME.correct_laps(body.get("count"))
                else:
                    raise ValueError("unknown action")
                RUNTIME.event(
                    "operator_confirmation",
                    {
                        "action": action,
                        "stationary_attested": body.get("confirm_stationary") is True,
                    },
                )
            self._json({"ok": True, "state": RUNTIME.snapshot()})
        except (ValueError, KeyError, TypeError) as exc:
            self._json({"error": str(exc)}, 400)

    def _captive(self, p):
        """폰의 인터넷 연결 확인 요청에 응답.
        ⚠️ 이 요청은 포트 80으로 오므로, iptables REDIRECT 80->8080 이 필요하다.
           또한 체크 도메인이 이 Pi 로 해석되도록 dnsmasq DNS 하이재킹이 필요하다.
           (자세한 설정은 마스터 문서 '캡티브 포털' 절 참고)"""
        if CAPTIVE_MODE == "off":
            self.send_error(404)
            return

        if CAPTIVE_MODE == "portal":
            # 302 리다이렉트 → 폰이 '캡티브 포털'로 인식해 셀룰러를 유지한다.
            self.send_response(302)
            self.send_header("Location", CAPTIVE_REDIRECT)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        # CAPTIVE_MODE == "success": 폰이 '인터넷 정상'으로 판단 (NAT 켠 경우에만)
        if p in ("/hotspot-detect.html", "/library/test/success.html"):
            b = b"<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/connecttest.txt":
            b = b"Microsoft Connect Test"  # Windows 는 이 문자열을 기대
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/ncsi.txt":
            b = b"Microsoft NCSI"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        else:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _text(self, s):
        b = s.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _html(self, legacy=False):
        page = Path(__file__).with_name("driver_dashboard.html")
        b = DASHBOARD_HTML.encode() if legacy else page.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _stream(self):
        """SSE. HTTP/1.1 이므로 chunked 로 보내야 프록시가 길이를 판단할 수 있다."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # ★ nginx 가 이 응답만 버퍼링하지 않게 지시 (설정 없이도 동작)
        self.send_header("X-Accel-Buffering", "no")
        chunked = self.protocol_version == "HTTP/1.1"
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            while not STOP.is_set():
                payload = json.dumps(
                    telemetry_snapshot(), ensure_ascii=False, allow_nan=False
                )
                body = f"data: {payload}\n\n".encode()
                if chunked:
                    self.wfile.write(f"{len(body):X}\r\n".encode() + body + b"\r\n")
                else:
                    self.wfile.write(body)
                self.wfile.flush()
                time.sleep(SSE_RATE)
        except OSError:
            pass
        finally:
            self.close_connection = True


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>UNIMOTORS</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html,body { width:100%; height:100%; background:#0a0a0a; color:#e8e8e8;
    font-family:-apple-system,"Segoe UI",Roboto,sans-serif; overflow:hidden;
    -webkit-user-select:none; user-select:none; }
  #wrap { width:100vw; height:100vh; display:flex; flex-direction:column;
    align-items:center; justify-content:center; padding:3vmin; }
  #top { display:flex; align-items:baseline; gap:3vmin; color:#888; }
  #clock { font-size:6vmin; font-variant-numeric:tabular-nums; letter-spacing:.05em; }
  #fix { font-size:3.2vmin; } #fix.ok { color:#3ddc84; } #fix.no { color:#e0a030; }
  #mid { display:flex; align-items:center; justify-content:center; gap:4vmin; margin:1vmin 0; }
  #speed-box { display:flex; flex-direction:column; align-items:center; }
  #speed { font-size:32vmin; font-weight:700; line-height:.9;
    font-variant-numeric:tabular-nums; color:#fff; text-shadow:0 0 6vmin rgba(80,160,255,.25); }
  #unit { font-size:4vmin; color:#7a7a7a; letter-spacing:.2em; margin-top:1vmin; }
  /* G-G 다이어그램 */
  #gg { width:26vmin; height:26vmin; border-radius:50%; position:relative;
    border:0.4vmin solid #222; background:radial-gradient(circle,#141414,#0a0a0a); flex:none; }
  #gg .cross-h,#gg .cross-v { position:absolute; background:#1e1e1e; }
  #gg .cross-h { left:0; right:0; top:50%; height:1px; }
  #gg .cross-v { top:0; bottom:0; left:50%; width:1px; }
  #gg .dot { position:absolute; width:2.6vmin; height:2.6vmin; border-radius:50%;
    background:#50a0ff; left:50%; top:50%; transform:translate(-50%,-50%);
    box-shadow:0 0 2vmin rgba(80,160,255,.6); transition:left .1s,top .1s; }
  #gg .lbl { position:absolute; font-size:1.8vmin; color:#555; }
  #gval { font-size:2vmin; color:#888; text-align:center; margin-top:.5vmin;
    font-variant-numeric:tabular-nums; }
  #heading { display:flex; align-items:baseline; gap:2vmin; font-variant-numeric:tabular-nums; }
  #compass { font-size:8vmin; font-weight:600; color:#50a0ff; min-width:3ch; text-align:center; }
  #deg { font-size:4.5vmin; color:#999; }
  #batt { width:100%; max-width:900px; margin-top:2vmin; border-top:.3vmin solid #1c1c1c;
    padding-top:1.6vmin; display:flex; justify-content:space-around; align-items:flex-end;
    font-variant-numeric:tabular-nums; gap:1vmin; }
  .cell { display:flex; flex-direction:column; align-items:center; gap:.5vmin; }
  .cell .v { font-size:5vmin; font-weight:600; color:#fff; line-height:1; }
  .cell .l { font-size:2vmin; color:#6a6a6a; letter-spacing:.08em; }
  .cell .v.regen { color:#3ddc84; } .cell .v.drain { color:#ffb054; }
  #batt.off { opacity:.25; }
  #alarm { position:fixed; top:0; left:0; right:0; text-align:center; font-size:3.2vmin;
    font-weight:700; padding:1.2vmin; display:none; letter-spacing:.05em; }
  #alarm.warn { display:block; background:#4a3500; color:#ffcf5a; }
  #alarm.crit { display:block; background:#5a0000; color:#ff6b6b;
    animation:blink .8s steps(2,start) infinite; }
  @keyframes blink { 50% { opacity:.4; } }
  #sats { position:fixed; bottom:2vmin; right:3vmin; font-size:2.4vmin; color:#555; }
  #cells { position:fixed; bottom:2vmin; left:3vmin; font-size:2vmin; color:#555;
    font-variant-numeric:tabular-nums; }
</style></head>
<body>
<div id="alarm"></div>
<div id="wrap">
  <div id="top"><div id="clock">--:--:--</div><div id="fix" class="no">NO FIX</div></div>
  <div id="mid">
    <div id="speed-box"><div id="speed">--</div><div id="unit">KM / H</div></div>
    <div>
      <div id="gg">
        <div class="cross-h"></div><div class="cross-v"></div>
        <div class="dot" id="gdot"></div>
      </div>
      <div id="gval">G --</div>
    </div>
  </div>
  <div id="heading"><div id="compass">--</div><div id="deg">--&deg;</div></div>
  <div id="batt" class="off">
    <div class="cell"><div class="v" id="b-soc">--</div><div class="l">SOC %</div></div>
    <div class="cell"><div class="v" id="b-volt">--</div><div class="l">VOLT</div></div>
    <div class="cell"><div class="v" id="b-cur">--</div><div class="l">AMP</div></div>
    <div class="cell"><div class="v" id="b-temp">--</div><div class="l">TEMP &deg;C</div></div>
    <div class="cell"><div class="v" id="b-remain">--</div><div class="l">REMAIN Ah</div></div>
    <div class="cell"><div class="v" id="b-range">--</div><div class="l">RANGE km</div></div>
  </div>
  <div id="cells">CELL --</div>
  <div id="sats">SAT --</div>
</div>
<script>
  const $=(id)=>document.getElementById(id);
  const $speed=$("speed"),$clock=$("clock"),$fix=$("fix"),$compass=$("compass"),
        $deg=$("deg"),$sats=$("sats"),$batt=$("batt"),$alarm=$("alarm"),$cells=$("cells"),
        $gdot=$("gdot"),$gval=$("gval");
  const $bSoc=$("b-soc"),$bVolt=$("b-volt"),$bCur=$("b-cur"),$bTemp=$("b-temp"),
        $bRemain=$("b-remain"),$bRange=$("b-range");
  let targetSpeed=0, shownSpeed=0, hasFix=false;
  const fmt=(x,d)=>(x==null||isNaN(x))?"--":Number(x).toFixed(d);
  const GG_MAX=1.5;   // 다이어그램 가장자리에 해당하는 G

  function onData(s){
    hasFix=s.fix; targetSpeed=s.fix?s.speed:0;
    if(s.utc){const[h,m,se]=s.utc.split(":").map(Number);let kh=(h+9)%24;
      $clock.textContent=[kh,m,se].map(x=>String(x).padStart(2,"0")).join(":");}
    else{const d=new Date();$clock.textContent=[d.getHours(),d.getMinutes(),d.getSeconds()].map(x=>String(x).padStart(2,"0")).join(":");}
    if(s.fix){$fix.textContent="GPS";$fix.className="ok";}else{$fix.textContent="NO FIX";$fix.className="no";}
    $compass.textContent=s.compass||"--";
    $deg.innerHTML=(s.heading!=null?Math.round(s.heading):"--")+"&deg;";
    $sats.textContent="SAT "+(s.sats||"--");
    // G-G 다이어그램: g_lat=횡(좌우), g_lon=종(가감속, 위=가속)
    const gl=s.g_lat||0, go=s.g_lon||0;
    const x=Math.max(-1,Math.min(1,gl/GG_MAX)), y=Math.max(-1,Math.min(1,go/GG_MAX));
    $gdot.style.left=(50+x*42)+"%"; $gdot.style.top=(50-y*42)+"%";
    const gmag=Math.sqrt(gl*gl+go*go);
    $gval.textContent="G "+fmt(gmag,2);
    // 배터리
    const b=s.bms;
    if(b&&s.bms_ok){
      $batt.classList.remove("off");
      $bSoc.textContent=fmt(b.soc,0); $bVolt.textContent=fmt(b.voltage,1);
      $bTemp.textContent=(b.temp_max!=null)?b.temp_max:"--";
      $bRemain.textContent=fmt(b.remain_ah,1);
      $bRange.textContent=(b.range_km!=null)?fmt(b.range_km,0):"--";
      $bCur.textContent=fmt(b.current,1);
      $bCur.className="v "+(b.regen?"regen":(b.current>0.2?"drain":""));
      let c="CELL";
      if(b.cell_v_max!=null&&b.cell_v_min!=null)
        c+=" "+b.cell_v_min+"~"+b.cell_v_max+"mV Δ"+(b.cell_v_diff!=null?b.cell_v_diff:"-");
      if(b.any_balancing)c+=" BAL";
      $cells.textContent=c;
      const f=b.faults;
      if(f&&f.dangers&&f.dangers.length){$alarm.className="crit";$alarm.textContent="⚠ "+f.dangers.join(" · ");}
      else if(f&&f.warnings&&f.warnings.length){$alarm.className="warn";$alarm.textContent="⚠ "+f.warnings.join(" · ");}
      else{$alarm.className="";$alarm.textContent="";}
    } else {
      $batt.classList.add("off");$alarm.className="";$alarm.textContent="";$cells.textContent="CELL --";
    }
  }
  function animate(){
    const diff=targetSpeed-shownSpeed;
    shownSpeed = Math.abs(diff)<0.1 ? targetSpeed : shownSpeed+diff*0.18;
    $speed.textContent=hasFix?Math.round(shownSpeed):"--";
    requestAnimationFrame(animate);
  }
  requestAnimationFrame(animate);
  function connect(){const es=new EventSource("/stream");
    es.onmessage=(e)=>{try{onData(JSON.parse(e.data));}catch(_){}};
    es.onerror=()=>{es.close();setTimeout(connect,2000);};}
  connect();
</script>
</body></html>
"""


def replay_input(path, rate):
    records, rejected = parse_csv(Path(path).read_text(encoding="utf-8-sig"))
    if rejected:
        raise ValueError("replay CSV has rejected rows")
    for record in records:
        if STOP.is_set():
            break
        RUNTIME.update_gps(dict(record, fix=bool(record.get("fix"))))
        RUNTIME.update_bms(
            {
                k: record[k]
                for k in ("voltage", "current", "soc", "temp_max", "temp_min")
                if record.get(k) is not None
            }
        )
        STOP.wait(rate)
    RUNTIME.event("replay_complete", {"records": len(records)})


def main():
    global RUNTIME, REPLAY, SESSION_ID, LOG_DIR, VEHICLE_ID
    parser = argparse.ArgumentParser(description="UNIMOTORS v7 local cluster")
    parser.add_argument("--replay", help="CSV replay; upload disabled")
    parser.add_argument("--replay-interval", type=float, default=0.2)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()
    REPLAY = bool(args.replay)
    state_dir = os.environ.get("UNIMOTORS_STATE_DIR", os.path.join(LOG_DIR, "state"))
    if REPLAY:
        LOG_DIR = os.path.join(LOG_DIR, "replay")
        state_dir = os.path.join(state_dir, "replay")
        VEHICLE_ID = "replay-" + VEHICLE_ID
    RUNTIME = RaceRuntime(
        state_dir,
        vehicle=VEHICLE_ID,
        battery_epoch=os.environ.get("UNIMOTORS_BATTERY_EPOCH"),
        resume_window=float(os.environ.get("UNIMOTORS_RESUME_SECONDS", "300")),
        persist_interval=float(os.environ.get("UNIMOTORS_PERSIST_SECONDS", "1")),
    )
    SESSION_ID = RUNTIME.segment
    RUNTIME.replay = REPLAY
    print(
        f"UNIMOTORS {VERSION}: http://{args.bind}:{PORT}; replay={REPLAY}", flush=True
    )
    workers = []

    def start(target, args=()):
        worker = threading.Thread(target=target, args=args, daemon=True)
        worker.start()
        workers.append(worker)

    start(csv_logger)
    if REPLAY:
        start(replay_input, (args.replay, args.replay_interval))
    else:
        start(raw_logger)
        start(gps_reader)
        start(display_updater)
        start(bms_poller)
        start(uploader)
    srv = ThreadingHTTPServer((args.bind, PORT), Handler)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        srv.server_close()
        for worker in workers:
            worker.join(timeout=5)
        RUNTIME.close()


if __name__ == "__main__":
    main()
