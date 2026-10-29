#!/usr/bin/env python3
"""UNIMOTORS 계기판 데모 시뮬레이터 (demo_sim.py v1).

실제 차량 대신 '가상 차량'을 실제 도로 폐루프 위에서 달리게 하고,
그 값을 기존 gps_server.py 파이프라인 그대로 흘려보낸다.

  - LCD 계기판   : gps_server 의 계기판 HTML + SSE (localhost:8080) 그대로 사용
                   + 랩 정보 오버레이(LAP / 현재 랩타임 / LAST / BEST / DEMO 배지)
  - 서버 전송    : gps_server.uploader 그대로 (ws://10.10.0.9:8090/ingest, 32필드 스키마)
  - 로컬 CSV     : gps_server.csv_logger 그대로 (demo_logs/)

모든 값은 하나의 물리 모델에서 나온다 (서로 어긋나지 않게):
  위치 s(m) → 위경도·방향·곡률·고도/경사
  속도 v    → 곡률 한계(횡G) + 가속/제동 한계 + 모터 출력 한계로 만든 속도 프로파일 추종
  G         → 종G = 실제 가속도 / 횡G = v²·곡률 (우회전 +)
  배터리    → 구름저항+공기저항+경사+가속 → 모터 출력 → 효율 → 팩 전력
              → OCV(SOC) − I·R 로 전압/전류 → 쿨롱카운팅 SOC·잔여Ah·사용Ah/Wh
              → I²R 발열로 온도, 전류에 따라 셀 편차
  랩        → 출발선(경로 시작점) 통과 시 랩 완료. 랩마다 페이스가 조금씩 느려짐(fade)

배터리 사양: 삼성 SDI INR21700-50S (NMC) 14S16P, 80Ah, 공칭 50.4V / 만충 58.8V.
고도는 실측이 아닌 합성값(완만한 경사)이다.

실행:
  python3 demo_sim.py                    # 실시간 데모 (LCD + 서버 전송)
  python3 demo_sim.py --dry-run 5        # 5랩을 즉시 계산해 랩별 요약만 출력 (검증용)
  python3 demo_sim.py --no-upload        # 서버 전송 없이 LCD 만
  python3 demo_sim.py --osrm "35.5734,129.1916;35.5763,129.1929;..."   # 다른 코스 생성
  python3 demo_sim.py --route my_route.json                             # 저장된 코스 사용

같은 폴더에 gps_server.py 가 있어야 한다 (실시간 모드). 서버 토큰은 환경변수
UNIMOTORS_TOKEN (또는 /etc/default/unimotors) 로 넣는다.
"""
import argparse
import json
import math
import os
import random
import sys
import threading
import time
import types
import urllib.request
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))
G = 9.80665

# ============================================================
#  기본 코스 — UNIST 캠퍼스 순환 (OSM 도로, OSRM 경로 기반, 약 2.6 km)
#  출발선 = 유니스트길 ↔ 북쪽 진입로 교차점, 북쪽으로 출발.
#  고래 다리 → 사연길(동) → 곡연1길 → 사연길(남·서) → 유니스트길(서) → 출발선
#  형식: [lat, lon]
# ============================================================
DEFAULT_ROUTE = [
    [35.573542, 129.191621], [35.573637, 129.191619], [35.57373, 129.191608],
    [35.573766, 129.191636], [35.573827, 129.191599], [35.573904, 129.191569],
    [35.57403, 129.191509], [35.574107, 129.191464], [35.574414, 129.191306],
    [35.57459, 129.191218], [35.574723, 129.19114], [35.574958, 129.190965],
    [35.575221, 129.190768], [35.575419, 129.190596], [35.575536, 129.19051],
    [35.575614, 129.190668], [35.575758, 129.190915], [35.575869, 129.191092],
    [35.576047, 129.191447], [35.57613, 129.191625], [35.576253, 129.191866],
    [35.576243, 129.191973], [35.576238, 129.192089], [35.576238, 129.192412],
    [35.576266, 129.192748], [35.576285, 129.192892], [35.576288, 129.192905],
    [35.576472, 129.193623], [35.576555, 129.19386], [35.576707, 129.194308],
    [35.576834, 129.194711], [35.576888, 129.194916], [35.576932, 129.195294],
    [35.576923, 129.19586], [35.57692, 129.196001], [35.576912, 129.196314],
    [35.576904, 129.196605], [35.5769, 129.196799], [35.576885, 129.197506],
    [35.576862, 129.197578], [35.576819, 129.197772], [35.576695, 129.197906],
    [35.576463, 129.198532], [35.576166, 129.19924], [35.575631, 129.200377],
    [35.575171, 129.200927], [35.574574, 129.201555], [35.574168, 129.201946],
    [35.574106, 129.202069], [35.574029, 129.202089], [35.573948, 129.202106],
    [35.573875, 129.20213], [35.573281, 129.202332], [35.573161, 129.202372],
    [35.573088, 129.202391], [35.57269, 129.201286], [35.572571, 129.200941],
    [35.572539, 129.200819], [35.572522, 129.200761], [35.572486, 129.200593],
    [35.572483, 129.200469], [35.572519, 129.200222], [35.572518, 129.20016],
    [35.572503, 129.200101], [35.572476, 129.200049], [35.572438, 129.200008],
    [35.572343, 129.199881], [35.572296, 129.199755], [35.572262, 129.199555],
    [35.572257, 129.199524], [35.572225, 129.199276], [35.572083, 129.197914],
    [35.572063, 129.197752], [35.572051, 129.197625], [35.572046, 129.197574],
    [35.572008, 129.197205], [35.571975, 129.196851], [35.571966, 129.196633],
    [35.571971, 129.196297], [35.571978, 129.196146], [35.572006, 129.195894],
    [35.572044, 129.195692], [35.572089, 129.195506], [35.572134, 129.195315],
    [35.572154, 129.19524], [35.572211, 129.195129], [35.572233, 129.195101],
    [35.572249, 129.195068], [35.572255, 129.195048], [35.57226, 129.195032],
    [35.572264, 129.194994], [35.572261, 129.194955], [35.57228, 129.194842],
    [35.572322, 129.194742], [35.572348, 129.194685], [35.572423, 129.194524],
    [35.572607, 129.194097], [35.572698, 129.193868], [35.57277, 129.193669],
    [35.572842, 129.193436], [35.572887, 129.193285], [35.572976, 129.19293],
    [35.573077, 129.192514], [35.573132, 129.192304], [35.573191, 129.192157],
    [35.573256, 129.192032], [35.573368, 129.191897], [35.573483, 129.191749],
    [35.573537, 129.191673],
]

# ============================================================
#  차량 / 배터리 / 주행 파라미터 (여기 숫자만 바꾸면 성격이 바뀜)
# ============================================================
VEH = {
    "mass": 230.0,          # kg (차체 + 운전자)
    "crr": 0.015,           # 구름저항 계수
    "cda": 0.45,            # 공기저항 Cd·A (m²)
    "rho": 1.2,             # 공기 밀도
    "p_max": 8000.0,        # W  모터 기계출력 상한
    "a_trac": 2.5,          # m/s² 구동력 한계 (저속 토크/접지)
    "p_regen_max": 3000.0,  # W  회생 기계출력 상한 (나머지는 마찰 브레이크)
    "regen_min_kmh": 8.0,   # 이 속도 아래로는 회생 거의 없음
    "eta_drive": 0.85,      # 배터리→바퀴 효율
    "eta_regen": 0.65,      # 바퀴→배터리 효율
    "p_aux": 45.0,          # W  상시 부하 (Pi, LCD, 컨트롤러 대기)
}
PACK = {
    "series": 14,
    "capacity_ah": 80.0,
    "r_int": 0.021,         # Ω 팩 내부저항 (셀 DCIR × 14/16 + 버스바/MOS)
    # NMC 셀 OCV 곡선 (SOC %, V/셀)
    "ocv": [(0, 3.00), (5, 3.28), (10, 3.38), (20, 3.46), (30, 3.55), (40, 3.62),
            (50, 3.68), (60, 3.75), (70, 3.84), (80, 3.93), (90, 4.04), (100, 4.17)],
    "c_th": 15000.0,        # J/K 팩 열용량 (셀 16kg × ~0.9kJ/kgK)
    "r_th": 0.35,           # K/W 팩→외기 열저항 (주행풍 냉각 포함)
}
DRIVE = {
    "v_top_kmh": 55.0,      # 직선 최고속도 (캠퍼스 코스)
    "a_lat": 4.5,           # m/s² 코너 횡가속 한계 (~0.46G)
    "a_acc": 2.5,           # m/s² 가속 한계
    "a_brk": 4.0,           # m/s² 제동 한계 (~0.41G)
    "tau": 0.4,             # s 속도 추종 시정수 (운전자 반응)
    "pace_jitter": 0.012,   # 랩마다 페이스 랜덤 편차 (표준편차, 비율)
}
DS = 1.0                    # m 경로 재샘플 간격
HEAD_SMOOTH_M = 12          # m 방향 평활 창 (교차로 직각 코너 → 반경 ~7m)


# ============================================================
#  유틸
# ============================================================
def interp_table(tab, x):
    if x <= tab[0][0]:
        return tab[0][1]
    for (x0, y0), (x1, y1) in zip(tab, tab[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return tab[-1][1]


def wrap_pi(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def fmt_lap(sec):
    if sec is None:
        return "--:--.-"
    m = int(sec // 60)
    return f"{m}:{sec - m * 60:04.1f}"


# ============================================================
#  코스 (경로 → 1m 간격 재샘플, 방향/곡률/고도/경사)
# ============================================================
def remove_spurs(pts):
    """OSRM 경로의 '갔다가 그대로 되돌아오는' 막다른 구간 제거 (좌표가 정확히 반복됨)."""
    out = []
    for p in pts:
        if out and p == out[-1]:
            continue
        if len(out) >= 2 and p == out[-2]:
            out.pop()
            continue
        out.append(p)
    return out


def clean_ring(pts):
    pts = [(round(p[0], 6), round(p[1], 6)) for p in pts]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    start = pts[0]
    for _ in range(2):                         # 시작점 부근 막다른 구간도 잡도록 회전 후 재처리
        pts = remove_spurs(pts)
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        h = len(pts) // 2
        pts = pts[h:] + pts[:h]
    # 원래 시작점(또는 가장 가까운 점)이 맨 앞에 오도록 되돌림
    i = min(range(len(pts)),
            key=lambda k: (pts[k][0] - start[0]) ** 2 + (pts[k][1] - start[1]) ** 2)
    pts = pts[i:] + pts[:i]
    return [list(p) for p in pts]


def fetch_osrm_loop(waypoints):
    """웨이포인트(lat,lon 리스트)를 도로를 따라 잇는 폐루프를 OSRM 공개 서버에서 받아온다."""
    wps = list(waypoints) + [waypoints[0]]
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in wps)
    url = (f"https://router.project-osrm.org/route/v1/driving/{coords}"
           f"?overview=full&geometries=geojson")
    req = urllib.request.Request(url, headers={"User-Agent": "unimotors-demo/1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode())
    if j.get("code") != "Ok":
        raise RuntimeError(f"OSRM 응답 오류: {j.get('code')} {j.get('message')}")
    line = j["routes"][0]["geometry"]["coordinates"]
    return clean_ring([[lat, lon] for lon, lat in line])


def load_route_file(path):
    with open(path, encoding="utf-8") as f:
        j = json.load(f)
    if isinstance(j, dict):                    # GeoJSON (LineString / Feature / FeatureCollection)
        if j.get("type") == "FeatureCollection":
            j = j["features"][0]
        if j.get("type") == "Feature":
            j = j["geometry"]
        return clean_ring([[lat, lon] for lon, lat in j["coordinates"]])
    return clean_ring(j)                       # [[lat, lon], ...]


class Route:
    def __init__(self, latlon):
        self.lat0 = sum(p[0] for p in latlon) / len(latlon)
        self.lon0 = sum(p[1] for p in latlon) / len(latlon)
        self.kx = math.radians(1) * 6371000.0 * math.cos(math.radians(self.lat0))
        self.ky = math.radians(1) * 6371000.0
        xy = [((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky) for lat, lon in latlon]
        xy.append(xy[0])                       # 폐루프

        # --- 등간격 재샘플 ---
        seg = [math.dist(xy[i], xy[i + 1]) for i in range(len(xy) - 1)]
        L = sum(seg)
        N = max(10, int(round(L / DS)))
        ds = L / N
        xs, ys = [], []
        i = 0; acc = 0.0
        for k in range(N):
            target = k * ds
            while i < len(seg) - 1 and acc + seg[i] < target:
                acc += seg[i]; i += 1
            f = (target - acc) / seg[i] if seg[i] > 0 else 0.0
            xs.append(xy[i][0] + (xy[i + 1][0] - xy[i][0]) * f)
            ys.append(xy[i][1] + (xy[i + 1][1] - xy[i][1]) * f)
        self.N, self.L, self.ds, self.xs, self.ys = N, L, ds, xs, ys

        # --- 방향 (진북 기준 시계방향, rad) — 단위벡터 이동평균으로 평활 ---
        raw = [math.atan2(xs[(k + 1) % N] - xs[k], ys[(k + 1) % N] - ys[k]) for k in range(N)]
        w = max(1, int(HEAD_SMOOTH_M / ds / 2))
        self.psi = []
        for k in range(N):
            sx = sum(math.sin(raw[(k + j) % N]) for j in range(-w, w + 1))
            cx = sum(math.cos(raw[(k + j) % N]) for j in range(-w, w + 1))
            self.psi.append(math.atan2(sx, cx))
        # --- 곡률 (rad/m, 우회전 +) ---
        kap = [wrap_pi(self.psi[(k + 1) % N] - self.psi[k]) / ds for k in range(N)]
        self.kappa = [sum(kap[(k + j) % N] for j in range(-2, 3)) / 5 for k in range(N)]

        # --- 고도 (합성): 북쪽이 조금 높고 완만한 굴곡. 랩마다 동일 → 일관성 유지 ---
        ymin, ymax = min(ys), max(ys)
        alt = [42.0 + 12.0 * (ys[k] - ymin) / max(1.0, ymax - ymin)
               + 1.5 * math.sin(2 * math.pi * k / N * 2) for k in range(N)]
        wa = max(1, int(15 / ds))
        self.alt = [sum(alt[(k + j) % N] for j in range(-wa, wa + 1)) / (2 * wa + 1)
                    for k in range(N)]
        self.grade = [(self.alt[(k + 1) % N] - self.alt[k]) / ds for k in range(N)]

    def at(self, s):
        """경로상 거리 s(m) → (lat, lon, heading_deg, kappa, alt, grade)."""
        s %= self.L
        x = s / self.ds
        k = int(x) % self.N; k1 = (k + 1) % self.N; f = x - int(x)
        px = self.xs[k] + (self.xs[k1] - self.xs[k]) * f
        py = self.ys[k] + (self.ys[k1] - self.ys[k]) * f
        psi = self.psi[k] + wrap_pi(self.psi[k1] - self.psi[k]) * f
        lat = self.lat0 + py / self.ky
        lon = self.lon0 + px / self.kx
        return (lat, lon, math.degrees(psi) % 360.0,
                self.kappa[k] + (self.kappa[k1] - self.kappa[k]) * f,
                self.alt[k] + (self.alt[k1] - self.alt[k]) * f,
                self.grade[k])


# ============================================================
#  속도 프로파일 (곡률 한계 → 전진(가속·출력) → 후진(제동), 폐루프)
# ============================================================
def resist_force(v, grade):
    return (VEH["crr"] * VEH["mass"] * G
            + 0.5 * VEH["rho"] * VEH["cda"] * v * v
            + VEH["mass"] * G * grade)


def build_profile(route):
    N, ds = route.N, route.ds
    vtop = DRIVE["v_top_kmh"] / 3.6
    vlim = [min(vtop, math.sqrt(DRIVE["a_lat"] / max(abs(k), 1e-6))) for k in route.kappa]
    M = 3 * N
    v = [vlim[i % N] for i in range(M)]
    for i in range(M - 1):                     # 가속 한계 (구동력 + 출력)
        vi = max(v[i], 0.5)
        a_pow = VEH["p_max"] / (VEH["mass"] * vi) - resist_force(vi, route.grade[i % N]) / VEH["mass"]
        a = max(0.05, min(DRIVE["a_acc"], VEH["a_trac"], a_pow))
        v[i + 1] = min(v[i + 1], math.sqrt(v[i] ** 2 + 2 * a * ds))
    for i in range(M - 1, 0, -1):              # 제동 한계 (코너 앞 감속)
        v[i - 1] = min(v[i - 1], math.sqrt(v[i] ** 2 + 2 * DRIVE["a_brk"] * ds))
    return v[N:2 * N]


# ============================================================
#  가상 차량
# ============================================================
class DemoCar:
    def __init__(self, route, soc_start=85.0, ambient=22.0, fade=0.004, fade_max=0.08,
                 soc_floor=10.0, gps_noise=0.0, seed=None, on_lap=None, on_swap=None):
        self.r = route
        self.prof = build_profile(route)
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.soc_start = soc_start; self.soc_floor = soc_floor
        self.ambient = ambient; self.fade = fade; self.fade_max = fade_max
        self.gps_noise = gps_noise
        self.on_lap = on_lap; self.on_swap = on_swap
        self.t = 0.0                           # 시뮬레이션 경과초
        self.s = 0.0; self.v = 0.0; self.a = 0.0
        self.dist = 0.0
        self.temp = ambient + 3.0
        self.sats = 15; self.hdop = 0.7
        self.reset_battery(soc_start)
        self.reset_laps()

    # --- 배터리/랩 초기화 ---
    def reset_battery(self, soc):
        self.q_ah = PACK["capacity_ah"] * soc / 100.0
        self.used_ah = 0.0; self.used_wh = 0.0
        self.km_per_ah = None; self.learn_d0 = self.dist; self.learn_q0 = 0.0
        self.I = 0.0; self.V = self.ocv()
        self.p_batt = 0.0

    def reset_laps(self):
        self.lap_no = 1; self.lap_t0 = self.t; self.last_lap = None; self.best_lap = None
        self.lap_wh0 = self.used_wh; self.lap_vmax = 0.0; self.lap_gmax = 0.0
        self.lap_d0 = self.dist
        self.pace = self.new_pace()

    def new_pace(self):
        slow = min(self.fade_max, self.fade * (self.lap_no - 1))
        return (1.0 - slow) * (1.0 + self.rng.gauss(0, DRIVE["pace_jitter"]))

    @property
    def soc(self):
        return max(0.0, min(100.0, self.q_ah / PACK["capacity_ah"] * 100.0))

    def ocv(self):
        return PACK["series"] * interp_table(PACK["ocv"], self.soc)

    def derate(self):
        d = 1.0
        if self.soc < 25:
            d = min(d, 0.5 + 0.5 * max(0.0, self.soc - 5) / 20.0)
        if self.temp > 50:
            d = min(d, max(0.3, 1.0 - (self.temp - 50) * 0.07))
        return d

    def prof_at(self, s):
        x = (s % self.r.L) / self.r.ds
        k = int(x) % self.r.N; k1 = (k + 1) % self.r.N; f = x - int(x)
        return self.prof[k] + (self.prof[k1] - self.prof[k]) * f

    # --- 한 스텝 ---
    def step(self, dt, hold=False):
        m = VEH["mass"]
        _, _, _, _, _, grade = self.r.at(self.s)
        if hold:
            vt = 0.0
        else:
            vt = min(self.prof_at(self.s + self.v * DRIVE["tau"]) * self.pace,
                     DRIVE["v_top_kmh"] / 3.6)
        a_des = max(-DRIVE["a_brk"], min(DRIVE["a_acc"], (vt - self.v) / DRIVE["tau"]))
        if hold and self.v < 0.05:
            a_des = 0.0
        f_res = resist_force(self.v, grade) if self.v > 0.01 else 0.0
        F = m * a_des + f_res
        F = min(F, m * VEH["a_trac"])
        p_lim = VEH["p_max"] * self.derate()
        if F > 0 and F * self.v > p_lim:
            F = p_lim / max(self.v, 0.1)
        a = (F - f_res) / m
        if self.v <= 0.0 and a < 0:
            a = 0.0
        p_mech = F * self.v

        # 회생/구동 → 배터리 전력
        if p_mech >= 0:
            p_elec = p_mech / VEH["eta_drive"]
        else:
            fade_v = min(1.0, max(0.0, (self.v * 3.6 - VEH["regen_min_kmh"] * 0.5)
                                  / VEH["regen_min_kmh"]))
            p_regen = max(p_mech, -VEH["p_regen_max"]) * fade_v
            p_elec = p_regen * VEH["eta_regen"]
        p_batt = p_elec + VEH["p_aux"]

        ocv = self.ocv(); R = PACK["r_int"]
        disc = max(0.0, ocv * ocv - 4 * R * p_batt)
        I = (ocv - math.sqrt(disc)) / (2 * R)
        V = ocv - I * R
        dq = I * dt / 3600.0

        # 운동
        v_new = max(0.0, self.v + a * dt)
        ds = (self.v + v_new) / 2 * dt
        with self.lock:
            self.q_ah -= dq
            self.used_ah += dq
            self.used_wh += V * I * dt / 3600.0
            self.temp += (I * I * R - (self.temp - self.ambient) / PACK["r_th"]) * dt / PACK["c_th"]
            self.I, self.V, self.p_batt, self.a = I, V, p_batt, a
            self.v = v_new; self.s += ds; self.dist += ds; self.t += dt
            if hold and self.v < 0.05:
                self.lap_t0 = self.t           # 출발 전 대기는 랩타임에 넣지 않음
            # 전비 학습 (거리 / 소비Ah) — 300m 이상 달린 뒤부터
            dd = (self.dist - self.learn_d0) / 1000.0
            dq_l = self.used_ah - self.learn_q0
            if dd > 0.3 and dq_l > 0.01:
                kpa = dd / dq_l
                self.km_per_ah = kpa if self.km_per_ah is None else 0.98 * self.km_per_ah + 0.02 * kpa
                self.learn_d0 = self.dist; self.learn_q0 = self.used_ah
            # 랩 통계
            kmh = self.v * 3.6
            self.lap_vmax = max(self.lap_vmax, kmh)
            _, _, _, kap, _, _ = self.r.at(self.s)
            g_lat = self.v * self.v * kap / G
            self.lap_gmax = max(self.lap_gmax, math.hypot(self.a / G, g_lat))
            # GPS 품질 랜덤워크
            if self.rng.random() < dt * 0.2:
                self.sats = max(11, min(19, self.sats + self.rng.choice((-1, 1))))
            self.hdop = max(0.5, min(1.0, self.hdop + self.rng.gauss(0, 0.01 * math.sqrt(dt))))

        # 출발선 통과 → 랩 완료
        if self.s >= self.r.L:
            with self.lock:
                self.s -= self.r.L
                lap_time = self.t - self.lap_t0
                info = {"lap": self.lap_no, "time": lap_time,
                        "dist_km": (self.dist - self.lap_d0) / 1000.0,
                        "wh": self.used_wh - self.lap_wh0,
                        "vmax": self.lap_vmax, "gmax": self.lap_gmax,
                        "soc": self.soc, "temp": self.temp}
                self.last_lap = lap_time
                self.best_lap = lap_time if self.best_lap is None else min(self.best_lap, lap_time)
                self.lap_no += 1; self.lap_t0 = self.t
                self.lap_wh0 = self.used_wh; self.lap_d0 = self.dist
                self.lap_vmax = 0.0; self.lap_gmax = 0.0
                self.pace = self.new_pace()
                swap = self.soc <= self.soc_floor
            if self.on_lap:
                self.on_lap(info)
            if swap:                           # 배터리 교체(피트) → 새 세션
                with self.lock:
                    self.reset_battery(self.soc_start)
                    self.reset_laps()
                if self.on_swap:
                    self.on_swap()

    # --- 계기판/서버로 나갈 값 ---
    def gps_view(self):
        with self.lock:
            lat, lon, hdg, kap, alt, _ = self.r.at(self.s)
            v = self.v; a = self.a
            if self.gps_noise > 0:
                lat += self.rng.gauss(0, self.gps_noise) / self.r.ky
                lon += self.rng.gauss(0, self.gps_noise) / self.r.kx
            return {
                "speed": round(v * 3.6, 1), "heading": round(hdg, 1),
                "lat": round(lat, 6), "lon": round(lon, 6), "alt": round(alt, 1),
                "g_lon": round(a / G, 2), "g_lat": round(v * v * kap / G, 2),
                "sats": self.sats, "hdop": round(self.hdop, 1),
            }

    def bms_view(self):
        with self.lock:
            V = round(self.V, 1); I = round(self.I, 1)
            soc = round(self.soc, 1); remain = round(self.q_ah, 2)
            cell_avg = self.V / PACK["series"] * 1000.0
            diff = 3 + 0.04 * abs(self.I) + (0.2 * (30 - soc) if soc < 30 else 0.0)
            diff = int(round(diff + self.rng.uniform(-0.6, 0.6)))
            cmax = int(round(cell_avg + diff * 0.6)); cmin = cmax - diff
            warnings = ["SOC 낮음 주의"] if soc < 20 else []
            if self.temp >= 45:
                warnings.append("방전 고온 주의")
            st = "방전" if I > 0.5 else ("충전" if I < -0.5 else "정지")
            b = {
                "voltage": V, "current": I, "soc": soc,
                "power_w": round(V * I, 1), "regen": I < 0,
                "state": st, "charge_mos": True, "discharge_mos": True, "bms_life": 0,
                "remain_mah": int(self.q_ah * 1000), "remain_ah": remain,
                "cell_v_max": cmax, "cell_v_max_no": 7, "cell_v_min": cmin, "cell_v_min_no": 12,
                "cell_v_diff": cmax - cmin,
                "temp_max": int(round(self.temp + 1.2)), "temp_max_no": 1,
                "temp_min": int(round(self.temp - 0.3)), "temp_min_no": 2,
                "cell_count": PACK["series"], "temp_sensor_count": 2,
                "charger_connected": False, "load_connected": True,
                "balancing_cells": [], "any_balancing": False,
                "faults": {"fault_code": 0, "warnings": warnings, "dangers": [],
                           "ok": not warnings},
            }
            if self.km_per_ah:
                b["range_km"] = round(self.q_ah * self.km_per_ah, 1)
            return b

    def lap_view(self):
        with self.lock:
            return {"lap": self.lap_no, "lap_time": round(self.t - self.lap_t0, 1),
                    "last_lap": round(self.last_lap, 1) if self.last_lap else None,
                    "best_lap": round(self.best_lap, 1) if self.best_lap else None}


# ============================================================
#  LCD 계기판 오버레이 (랩 정보 + DEMO 배지)
# ============================================================
LAP_BLOCK = """
<style>
  #dlap { position:fixed; top:2vmin; left:3vmin; font-variant-numeric:tabular-nums; }
  #dlap .n { font-size:3vmin; color:#888; letter-spacing:.1em; }
  #dlap .t { font-size:7vmin; font-weight:600; color:#fff; line-height:1; }
  #dlast { position:fixed; top:2vmin; right:3vmin; text-align:right;
    font-variant-numeric:tabular-nums; font-size:2.8vmin; color:#888; line-height:1.5; }
  #dlast b { color:#ddd; font-weight:600; } #dlast .best b { color:#c38bff; }
  #dbadge { position:fixed; bottom:2vmin; left:50%; transform:translateX(-50%);
    font-size:2vmin; color:#50a0ff; border:1px solid #234; border-radius:1vmin;
    padding:.3vmin 1.2vmin; letter-spacing:.2em; }
</style>
<div id="dlap"><div class="n" id="dlapn">LAP --</div><div class="t" id="dlapt">--:--.-</div></div>
<div id="dlast"><div>LAST <b id="dlastv">--:--.-</b></div>
  <div class="best">BEST <b id="dbestv">--:--.-</b></div></div>
<div id="dbadge">DEMO · SIMULATED</div>
<script>
  function _lf(x){ if(x==null) return "--:--.-"; const m=Math.floor(x/60);
    return m+":"+(x-m*60).toFixed(1).padStart(4,"0"); }
  function onLap(s){
    if(s.lap==null) return;
    document.getElementById("dlapn").textContent="LAP "+s.lap;
    document.getElementById("dlapt").textContent=_lf(s.lap_time);
    document.getElementById("dlastv").textContent=_lf(s.last_lap);
    document.getElementById("dbestv").textContent=_lf(s.best_lap);
  }
</script>
"""


def patch_dashboard_html(html):
    target = "onData(JSON.parse(e.data));"
    if target in html:
        html = html.replace(target,
                            "const _d=JSON.parse(e.data);onData(_d);if(window.onLap)onLap(_d);", 1)
        return html.replace("</body>", LAP_BLOCK + "</body>", 1)
    # 계기판 HTML 구조가 바뀐 경우: 별도 SSE 로 랩 정보만 받는다
    extra = LAP_BLOCK.replace("</script>", """
  (function(){const es=new EventSource("/stream");
    es.onmessage=e=>{try{onLap(JSON.parse(e.data));}catch(_){}};})();
</script>""")
    return html.replace("</body>", extra + "</body>", 1)


# ============================================================
#  실행 모드
# ============================================================
def make_route(args):
    if args.osrm:
        wps = [tuple(map(float, p.split(","))) for p in args.osrm.split(";") if p.strip()]
        print(f"[ROUTE] OSRM 으로 {len(wps)}개 웨이포인트 폐루프 생성 중…", flush=True)
        pts = fetch_osrm_loop(wps)
        with open(args.save_route, "w", encoding="utf-8") as f:
            json.dump(pts, f)
        print(f"[ROUTE] {len(pts)}점 저장 → {args.save_route}", flush=True)
        return Route(pts)
    if args.route:
        return Route(load_route_file(args.route))
    return Route(clean_ring(DEFAULT_ROUTE))


def dry_run(args):
    route = make_route(args)
    laps = []
    car = DemoCar(route, soc_start=args.soc, ambient=args.ambient, fade=args.fade,
                  soc_floor=args.soc_floor, seed=args.seed, on_lap=laps.append)
    print(f"코스 {route.L:.0f} m · 직선 최고 {DRIVE['v_top_kmh']:.0f} km/h · "
          f"시작 SOC {args.soc:.0f}%")
    dt = 0.05; t_end = 3600 * 12
    hold_until = args.start_wait
    d_check = 0.0
    while len(laps) < args.dry_run and car.t < t_end:
        v0 = car.v
        car.step(dt, hold=car.t < hold_until)
        d_check += (v0 + car.v) / 2 * dt
    print(f"{'랩':>3} {'시간':>8} {'거리km':>7} {'평속':>6} {'최고':>6} {'maxG':>5} "
          f"{'Wh':>6} {'Wh/km':>6} {'SOC%':>6} {'온도':>5}")
    for L in laps:
        avg = L["dist_km"] / (L["time"] / 3600.0)
        print(f"{L['lap']:>3} {fmt_lap(L['time']):>8} {L['dist_km']:7.3f} {avg:6.1f} "
              f"{L['vmax']:6.1f} {L['gmax']:5.2f} {L['wh']:6.1f} {L['wh'] / L['dist_km']:6.1f} "
              f"{L['soc']:6.1f} {L['temp']:5.1f}")
    print(f"검증: 적분거리 {d_check:.1f} m = 차량거리 {car.dist:.1f} m · "
          f"사용 {car.used_ah:.2f} Ah = SOC 감소분 "
          f"{(args.soc - car.soc) / 100 * PACK['capacity_ah']:.2f} Ah")


def serve(args):
    # pyserial 이 없어도 gps_server 를 불러올 수 있게 (데모는 시리얼을 안 씀)
    try:
        import serial  # noqa: F401
    except ImportError:
        sys.modules["serial"] = types.ModuleType("serial")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gps_server as gs

    route = make_route(args)

    # --- gps_server 설정 덮어쓰기 (원본 파일은 수정하지 않음) ---
    gs.VEHICLE_ID = args.vehicle
    gs.UPLOAD_ENABLED = not args.no_upload
    if args.server:
        gs.SERVER_WS_URL = args.server
    tok = os.environ.get("UNIMOTORS_TOKEN")
    if tok:
        gs.UPLOAD_TOKEN = tok
    gs.LOG_DIR = os.path.expanduser(args.log_dir)
    gs.SSE_RATE = 0.1                # 3B+ 로컬 LCD → 10Hz
    gs.UPLOAD_RATE = 0.5             # 서버 2Hz (실차와 동일)
    gs.TELEM_LOG_RATE = 1.0
    gs.DASHBOARD_HTML = patch_dashboard_html(gs.DASHBOARD_HTML)

    def new_session(tag):
        gs.SESSION_ID = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
        with gs.usage_lock:
            gs.usage.update({"used_ah": 0.0, "used_wh": 0.0, "last_t": None})
        print(f"[DEMO] {tag} → 새 세션 {gs.SESSION_ID}", flush=True)

    def on_lap(L):
        avg = L["dist_km"] / (L["time"] / 3600.0)
        print(f"[LAP] {L['lap']:>3}  {fmt_lap(L['time'])}  평속 {avg:4.1f} km/h  "
              f"{L['wh']:.0f} Wh ({L['wh'] / L['dist_km']:.0f} Wh/km)  "
              f"SOC {L['soc']:.1f}%  {L['temp']:.1f}°C", flush=True)

    car = DemoCar(route, soc_start=args.soc, ambient=args.ambient, fade=args.fade,
                  soc_floor=args.soc_floor, gps_noise=args.gps_noise, seed=args.seed,
                  on_lap=on_lap, on_swap=lambda: new_session("배터리 교체(SOC 하한)"))

    # 서버/CSV 로 나가는 used_ah/used_wh 를 시뮬레이터 정밀값으로 (샘플링 오차 제거)
    _orig_snapshot = gs.telemetry_snapshot

    def snapshot():
        d = _orig_snapshot()
        with car.lock:
            d["used_ah"] = round(car.used_ah, 3)
            d["used_wh"] = round(car.used_wh, 1)
        return d
    gs.telemetry_snapshot = snapshot

    def sim_loop():
        t_prev = time.monotonic(); t0 = t_prev
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            el = min(0.5, now - t_prev); t_prev = now
            n = max(1, int(math.ceil(el / 0.05)))
            for _ in range(n):
                car.step(el / n, hold=(now - t0) < args.start_wait)
            g = car.gps_view(); b = car.bms_view(); lp = car.lap_view()
            with gs.state_lock:
                gs.state.update(g)
                gs.state.update(lp)
                gs.state["compass"] = gs.heading_to_compass(g["heading"])
                gs.state["utc"] = datetime.now(timezone.utc).strftime("%H:%M:%S")
                gs.state["fix"] = True
                gs.state["bms"] = b
                gs.state["bms_ok"] = True
                gs.state["demo"] = True

    print(f"UNIMOTORS DEMO v1: http://0.0.0.0:{gs.PORT}  vehicle={gs.VEHICLE_ID}")
    print(f"코스 {route.L:.0f} m · 시작 SOC {args.soc:.0f}% · 랩당 감속 {args.fade * 100:.1f}%")
    print(f"서버 전송: {'ON -> ' + gs.SERVER_WS_URL if gs.UPLOAD_ENABLED else 'OFF'}"
          f"{'' if gs.WS_AVAILABLE or not gs.UPLOAD_ENABLED else ' (websocket-client 미설치!)'}")
    print(f"로그: {gs.LOG_DIR}", flush=True)
    threading.Thread(target=sim_loop, daemon=True).start()
    threading.Thread(target=gs.csv_logger, daemon=True).start()
    threading.Thread(target=gs.uploader, daemon=True).start()
    srv = gs.ThreadingHTTPServer(("0.0.0.0", gs.PORT), gs.Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료"); srv.shutdown()


def main():
    ap = argparse.ArgumentParser(description="UNIMOTORS 계기판 데모 시뮬레이터")
    ap.add_argument("--vehicle", default=os.environ.get("DEMO_VEHICLE", "demo"),
                    help="서버에 표시될 차량 ID (기본 demo)")
    ap.add_argument("--no-upload", action="store_true", help="서버 전송 끄기 (LCD 만)")
    ap.add_argument("--server", default=None, help="서버 WS 주소 (기본: gps_server 설정)")
    ap.add_argument("--soc", type=float, default=85.0, help="시작 SOC %% (기본 85)")
    ap.add_argument("--soc-floor", type=float, default=10.0,
                    help="이 SOC 이하로 랩을 마치면 배터리 교체 후 새 세션 (기본 10)")
    ap.add_argument("--ambient", type=float, default=22.0, help="외기 온도 °C")
    ap.add_argument("--fade", type=float, default=0.004,
                    help="랩마다 페이스 저하 비율 (기본 0.4%%, 최대 8%%). 0 = 일정")
    ap.add_argument("--gps-noise", type=float, default=0.0, help="GPS 노이즈 표준편차 m")
    ap.add_argument("--start-wait", type=float, default=5.0, help="출발 전 정차 초")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--route", default=None, help="코스 파일 ([[lat,lon],…] 또는 GeoJSON)")
    ap.add_argument("--osrm", default=None,
                    help='웨이포인트로 코스 생성: "lat,lon;lat,lon;…" (인터넷 필요)')
    ap.add_argument("--save-route", default="demo_route.json")
    ap.add_argument("--log-dir", default="~/demo_logs")
    ap.add_argument("--dry-run", type=int, default=0, metavar="LAPS",
                    help="실시간 없이 LAPS 랩을 계산해 요약만 출력")
    args = ap.parse_args()
    if args.dry_run:
        dry_run(args)
    else:
        serve(args)


if __name__ == "__main__":
    main()
