#!/usr/bin/env python3
"""UNIMOTORS 텔레메트리 서버 v7 (멀티 차량, aiohttp + durable ACK).

v7: stable sample IDs, full-field CSV, migration, TEAM orders and race reports.
See docs/19-cluster-research.md; the following v2 notes describe inherited features.

v1 대비 변경점:
  - 멀티 차량: latest/recent/last_seen 을 vehicle 별 dict 로 관리
  - /live 브로드캐스트에 vehicle + 온라인 차량목록 포함
    -> 대시보드가 1대면 자동표시, 2대+면 선택박스
  - phone-* 차량(임시 위치추적)은 DB 저장 안 함(개인정보), 브로드캐스트만
  - 누적 API: /api/vehicles, /api/sessions?vehicle=, /api/cumulative?vehicle=
    (전체 궤적 + 통계: 총거리/최고속도/평속/총시간/주행횟수/최대G)
  - 차량 표시이름 매핑 (VEHICLE_NAMES)

역할:
  - WS /ingest  : 차량/폰이 스냅샷 push (토큰 인증). vehicle 필드로 구분.
  - WS /live    : 브라우저 실시간 수신 (온라인 차량 목록 + 선택 차량 데이터)
  - POST /upload: 주행후 10Hz raw telem CSV -> track (세션별)
  - GET /api/*  : 조회 (아래 라우트 참고)
  - GET /       : 대시보드 HTML

의존성: aiohttp  (pip3 install aiohttp)
"""

import asyncio
import hmac
import sys
from pathlib import Path
import json
import math
import os
import sqlite3
import time
from collections import deque
from datetime import datetime, timezone, timedelta

from aiohttp import web, WSMsgType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
from telemetry_protocol import parse_csv
from research_store import ResearchStore

RESEARCH_KEY = web.AppKey("research", ResearchStore)
WATCHER_KEY = web.AppKey("watcher", asyncio.Task)

# ============================================================
#  설정
# ============================================================
HOST = "0.0.0.0"
PORT = int(os.environ.get("UNIMOTORS_SERVER_PORT", "8090"))
DB_PATH = os.environ.get(
    "UNIMOTORS_SERVER_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry.db"),
)

AUTH_TOKEN = os.environ.get("UNIMOTORS_TOKEN", "change-me")
OFFLINE_SEC = 600
RECENT_MAX = 1200  # 차량당 유지할 최근 스냅샷 수
ALT_TH = 3.0  # 누적 상승고도 계산 시 무시할 GPS 고도 노이즈(m)

# 차량 표시 이름 (없으면 vehicle id 그대로 표시)
VEHICLE_NAMES = {
    "car1": "레이싱카",
    "starex": "스타렉스",
    # 개인차/기타 자유 추가
}


# phone-* 로 시작하는 vehicle 은 DB 저장 안 함 (임시 위치추적, 개인정보)
def is_ephemeral(vehicle):
    return bool(vehicle) and vehicle.startswith("phone")


KST = timezone(timedelta(hours=9))

FIELDS = [
    "ts",
    "vehicle",
    "session",
    "t_mono",
    "lat",
    "lon",
    "alt",
    "speed",
    "heading",
    "sats",
    "fix",
    "hdop",
    "g_lon",
    "g_lat",
    "voltage",
    "current",
    "soc",
    "power_w",
    "regen",
    "remain_ah",
    "range_km",
    "state",
    "used_ah",
    "used_wh",
    "temp_max",
    "temp_min",
    "cell_v_max",
    "cell_v_min",
    "cell_v_diff",
    "balancing",
    "alarm_level",
    "alarms",
]

# ============================================================
#  메모리 상태 (차량별)
# ============================================================
latest = {}  # {vehicle: snapshot}
recent = {}  # {vehicle: deque}
last_seen = {}  # {vehicle: epoch}
live_clients = set()


def vehicle_name(v):
    return VEHICLE_NAMES.get(v, v)


def online_vehicles():
    now = time.time()
    return [v for v, t in last_seen.items() if now - t < OFFLINE_SEC]


def vehicle_list_payload():
    """온라인 차량 목록 (id + 표시이름)."""
    return [{"id": v, "name": vehicle_name(v)} for v in online_vehicles()]


# ============================================================
#  SQLite
# ============================================================
def _db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def db_init():
    conn = _db_connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS summary (
            ts TEXT, vehicle TEXT, session TEXT, t_mono REAL,
            lat REAL, lon REAL, alt REAL, speed REAL, heading REAL,
            sats INTEGER, fix INTEGER, hdop REAL, g_lon REAL, g_lat REAL,
            voltage REAL, current REAL, soc REAL, power_w REAL, regen INTEGER,
            remain_ah REAL, range_km REAL, state TEXT,
            used_ah REAL, used_wh REAL,
            temp_max REAL, temp_min REAL,
            cell_v_max INTEGER, cell_v_min INTEGER, cell_v_diff INTEGER,
            balancing INTEGER, alarm_level INTEGER, alarms TEXT,
            PRIMARY KEY (vehicle, ts)
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_summary_v_ts ON summary(vehicle, ts);")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS track (
            session TEXT, vehicle TEXT, ts TEXT, t_mono REAL,
            lat REAL, lon REAL, alt REAL,
            speed REAL, heading REAL, g_lon REAL, g_lat REAL,
            soc REAL, voltage REAL, current REAL, used_ah REAL,
            PRIMARY KEY (session, ts)
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_track_v ON track(vehicle);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_track_session ON track(session);")
    # 설정 저장 (출발선 좌표 등). 팀원 브라우저 간 공유되도록 서버에 둔다.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")

    # --- 기존 DB 마이그레이션 (컬럼 추가; 이미 있으면 무시) ---
    migrations = [
        ("summary", "session TEXT"),
        ("summary", "t_mono REAL"),
        ("summary", "alt REAL"),
        ("summary", "hdop REAL"),
        ("summary", "used_ah REAL"),
        ("summary", "used_wh REAL"),
        ("track", "t_mono REAL"),
        ("track", "alt REAL"),
        ("track", "soc REAL"),
        ("track", "voltage REAL"),
        ("track", "current REAL"),
        ("track", "used_ah REAL"),
    ]
    for table, coldef in migrations:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            print(f"[DB] {table} + {coldef.split()[0]}", flush=True)
        except sqlite3.OperationalError:
            pass  # 이미 존재
    cur.execute("CREATE INDEX IF NOT EXISTS idx_summary_session ON summary(session);")
    conn.commit()
    conn.close()


def db_insert_summary(snap):
    conn = _db_connect()
    try:
        cols = ",".join(FIELDS)
        ph = ",".join("?" * len(FIELDS))
        conn.execute(
            f"INSERT OR IGNORE INTO summary ({cols}) VALUES ({ph})",
            [snap.get(k) for k in FIELDS],
        )
        conn.commit()
    finally:
        conn.close()


def db_insert_track_bulk(session, vehicle, rows):
    conn = _db_connect()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO track "
            "(session,vehicle,ts,t_mono,lat,lon,alt,speed,heading,g_lon,g_lat,"
            " soc,voltage,current,used_ah) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    session,
                    vehicle,
                    r.get("ts"),
                    r.get("t_mono"),
                    r.get("lat"),
                    r.get("lon"),
                    r.get("alt"),
                    r.get("speed"),
                    r.get("heading"),
                    r.get("g_lon"),
                    r.get("g_lat"),
                    r.get("soc"),
                    r.get("voltage"),
                    r.get("current"),
                    r.get("used_ah"),
                )
                for r in rows
            ],
        )
        conn.commit()
        return conn.total_changes
    finally:
        conn.close()


def db_query_history(date_str, vehicle=None):
    conn = _db_connect()
    try:
        if vehicle:
            cur = conn.execute(
                "SELECT * FROM summary WHERE ts LIKE ? AND vehicle=? ORDER BY ts",
                (date_str + "%", vehicle),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM summary WHERE ts LIKE ? ORDER BY ts", (date_str + "%",)
            )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def db_query_track(session):
    conn = _db_connect()
    try:
        cur = conn.execute(
            "SELECT ts,t_mono,lat,lon,alt,speed,heading,g_lon,g_lat,soc,voltage,current,used_ah "
            "FROM track WHERE session=? ORDER BY ts",
            (session,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def db_query_session(session, vehicle=None):
    """summary 테이블에서 세션 단위 조회 (배터리 포함 2Hz 데이터)."""
    conn = _db_connect()
    try:
        if vehicle:
            cur = conn.execute(
                "SELECT * FROM summary WHERE session=? AND vehicle=? ORDER BY ts",
                (session, vehicle),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM summary WHERE session=? ORDER BY ts", (session,)
            )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def db_query_vehicles():
    """기록이 있는 모든 차량 (summary + track). 온라인 여부 포함."""
    conn = _db_connect()
    try:
        vs = set()
        for r in conn.execute("SELECT DISTINCT vehicle FROM summary").fetchall():
            if r[0]:
                vs.add(r[0])
        for r in conn.execute("SELECT DISTINCT vehicle FROM track").fetchall():
            if r[0]:
                vs.add(r[0])
        on = set(online_vehicles())
        return [
            {"id": v, "name": vehicle_name(v), "online": v in on} for v in sorted(vs)
        ]
    finally:
        conn.close()


def db_query_sessions(vehicle):
    """차량의 주행(세션) 목록 — 세션별 요약 정보 포함.
    summary.session 기준(2Hz, 배터리 포함) + track 세션(10Hz 정밀) 표시."""
    conn = _db_connect()
    try:
        # summary 세션별 집계
        # 주행시간은 t_mono(monotonic 경과초) 우선 — 시계 점프에 영향받지 않음.
        # ★ used_ah/used_wh 는 MAX 가 아니라 '마지막 값'을 써야 한다.
        #   방전은 증가하지만 충전은 감소하므로 MAX 를 쓰면 충전 세션이 ~0 으로 나온다.
        rows = conn.execute(
            """
            SELECT s.session,
                   MIN(s.ts) AS t0, MAX(s.ts) AS t1, COUNT(*) AS n,
                   MAX(s.speed) AS vmax, AVG(s.speed) AS vavg,
                   MIN(s.soc) AS soc_min, MAX(s.soc) AS soc_max,
                   MIN(s.t_mono) AS m0, MAX(s.t_mono) AS m1,
                   MAX(s.temp_max) AS tmax,
                   (SELECT used_ah FROM summary x WHERE x.vehicle=s.vehicle
                     AND x.session=s.session AND x.used_ah IS NOT NULL
                     ORDER BY x.ts DESC LIMIT 1) AS used_ah,
                   (SELECT used_wh FROM summary x WHERE x.vehicle=s.vehicle
                     AND x.session=s.session AND x.used_wh IS NOT NULL
                     ORDER BY x.ts DESC LIMIT 1) AS used_wh,
                   (SELECT soc FROM summary x WHERE x.vehicle=s.vehicle
                     AND x.session=s.session AND x.soc IS NOT NULL
                     ORDER BY x.ts ASC LIMIT 1) AS soc_first,
                   (SELECT soc FROM summary x WHERE x.vehicle=s.vehicle
                     AND x.session=s.session AND x.soc IS NOT NULL
                     ORDER BY x.ts DESC LIMIT 1) AS soc_last
            FROM summary s WHERE s.vehicle=? AND s.session IS NOT NULL
            GROUP BY s.session ORDER BY t0 DESC
        """,
            (vehicle,),
        ).fetchall()
        # 10Hz 정밀 궤적이 있는 세션 집합
        has_track = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT session FROM track WHERE vehicle=?", (vehicle,)
            ).fetchall()
        }
        sessions = []
        for (
            s,
            t0,
            t1,
            n,
            vmax,
            vavg,
            smin,
            smax,
            m0,
            m1,
            tmax,
            uah,
            uwh,
            soc0,
            soc1,
        ) in rows:
            dur = None
            if m0 is not None and m1 is not None and m1 > m0:
                dur = round(m1 - m0, 1)  # 시계와 무관한 실제 경과
            # 세션 유형: 거의 움직이지 않았으면 충전/정차로 본다
            moved = (vmax or 0) >= 3.0
            charging = (not moved) and uah is not None and uah < -0.05
            sessions.append(
                {
                    "session": s,
                    "start": t0,
                    "end": t1,
                    "points": n,
                    "duration_sec": dur,
                    "kind": "charge" if charging else ("drive" if moved else "idle"),
                    "max_speed": round(vmax, 1) if vmax is not None else None,
                    "avg_speed": round(vavg, 1) if vavg is not None else None,
                    "used_ah": round(uah, 2) if uah is not None else None,
                    "used_wh": round(uwh, 1) if uwh is not None else None,
                    "soc_from": round(soc0, 1) if soc0 is not None else None,
                    "soc_to": round(soc1, 1) if soc1 is not None else None,
                    "temp_max": round(tmax, 1) if tmax is not None else None,
                    "soc_drop": (
                        round(smax - smin, 1)
                        if smin is not None and smax is not None
                        else None
                    ),
                    "has_track": s in has_track,
                }
            )
        # session 컬럼이 없던 구버전 데이터용: 날짜 목록도 함께
        days = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT substr(ts,1,10) d FROM summary "
                "WHERE vehicle=? AND session IS NULL ORDER BY d DESC",
                (vehicle,),
            ).fetchall()
        ]
        # track 만 있고 summary 에 없는 세션(구버전 backfill)
        orphan = [
            s
            for s in sorted(has_track, reverse=True)
            if s not in {x["session"] for x in sessions}
        ]
        return {
            "vehicle": vehicle,
            "name": vehicle_name(vehicle),
            "sessions": sessions,
            "track_only": orphan,
            "days": days,
        }
    finally:
        conn.close()


def db_get_setting(key):
    conn = _db_connect()
    try:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def db_set_setting(key, value):
    conn = _db_connect()
    try:
        conn.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def db_count_target(vehicle, session=None):
    """삭제 대상 행 수를 미리 세어 반환 (삭제 전 확인용)."""
    conn = _db_connect()
    try:
        if session:
            s = conn.execute(
                "SELECT COUNT(*) FROM summary WHERE vehicle=? AND session=?",
                (vehicle, session),
            ).fetchone()[0]
            t = conn.execute(
                "SELECT COUNT(*) FROM track WHERE vehicle=? AND session=?",
                (vehicle, session),
            ).fetchone()[0]
        else:
            s = conn.execute(
                "SELECT COUNT(*) FROM summary WHERE vehicle=?", (vehicle,)
            ).fetchone()[0]
            t = conn.execute(
                "SELECT COUNT(*) FROM track WHERE vehicle=?", (vehicle,)
            ).fetchone()[0]
        r = conn.execute(
            "SELECT COUNT(*) FROM records_v7 WHERE vehicle=?"
            + (" AND segment=?" if session else ""),
            (vehicle, session) if session else (vehicle,),
        ).fetchone()[0]
        return {
            "vehicle": vehicle,
            "session": session,
            "summary": s,
            "track": t,
            "research": r,
            "total": s + t + r,
        }
    finally:
        conn.close()


def db_delete_data(vehicle, session=None):
    """주행 데이터 삭제. session 이 없으면 그 차량 전체.
    되돌릴 수 없으므로 호출 측(웹 UI)에서 이중 확인을 반드시 거친다."""
    conn = _db_connect()
    try:
        if session:
            c1 = conn.execute(
                "DELETE FROM summary WHERE vehicle=? AND session=?", (vehicle, session)
            ).rowcount
            c2 = conn.execute(
                "DELETE FROM track WHERE vehicle=? AND session=?", (vehicle, session)
            ).rowcount
        else:
            c1 = conn.execute(
                "DELETE FROM summary WHERE vehicle=?", (vehicle,)
            ).rowcount
            c2 = conn.execute("DELETE FROM track WHERE vehicle=?", (vehicle,)).rowcount
        c3 = conn.execute(
            "DELETE FROM records_v7 WHERE vehicle=?"
            + (" AND segment=?" if session else ""),
            (vehicle, session) if session else (vehicle,),
        ).rowcount
        if session:
            identifiers = [
                identifier
                for identifier, payload in conn.execute(
                    "SELECT id,payload FROM events_v7 WHERE vehicle=?", (vehicle,)
                )
                if json.loads(payload).get("segment_id") == session
            ]
            conn.executemany(
                "DELETE FROM events_v7 WHERE id=?", [(x,) for x in identifiers]
            )
            c4 = len(identifiers)
        else:
            c4 = conn.execute(
                "DELETE FROM events_v7 WHERE vehicle=?", (vehicle,)
            ).rowcount
            conn.execute("DELETE FROM race_links WHERE vehicle=?", (vehicle,))
            conn.execute("DELETE FROM team_commands WHERE vehicle=?", (vehicle,))
        conn.commit()
        conn.execute("VACUUM")  # 파일 크기 회수
        return {
            "deleted_summary": c1,
            "deleted_track": c2,
            "deleted_research": c3,
            "deleted_events": c4,
            "total": c1 + c2 + c3 + c4,
        }
    finally:
        conn.close()


def _haversine(a, b):
    """두 (lat,lon) 간 거리 (m)."""
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dla = la2 - la1
    dlo = lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def db_query_cumulative(vehicle):
    """차량 전체 누적: 궤적(다운샘플) + 통계."""
    conn = _db_connect()
    try:
        cur = conn.execute(
            "SELECT ts,lat,lon,speed,g_lon,g_lat,alt,used_ah,used_wh,session,t_mono,"
            "energy_increment_wh,charge_increment_ah,current_convention,energy_quality "
            "FROM summary WHERE vehicle=? ORDER BY ts",
            (vehicle,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    pts = []  # 지도용 (lat,lon) — 유효 좌표만
    speeds = []
    gmags = []
    total_dist = 0.0
    total_time = 0.0
    gain = 0.0  # 누적 상승고도(m)
    tot_ah = 0.0
    tot_wh = 0.0  # Unique measured increments; legacy fallback is labelled separately.
    days = set()
    sessions = set()
    prev_pt = None
    prev_t = None
    prev_alt = None
    prev_mono = None
    prev_sess = None
    sess_ah = {}
    sess_wh = {}
    known_wh, known_ah = False, False
    unverified_energy = False
    energy_partial = False
    for (
        ts,
        lat,
        lon,
        spd,
        glon,
        glat,
        alt,
        uah,
        uwh,
        sess,
        tmono,
        dwh,
        dah,
        convention,
        quality,
    ) in rows:
        if dwh is not None:
            tot_wh += dwh
            known_wh = True
        if dah is not None:
            tot_ah += dah
            known_ah = True
        if (dwh is not None or dah is not None) and (
            convention != "discharge_positive" or quality == "unverified_basis"
        ):
            unverified_energy = True
        if quality in {"partial", "waiting"}:
            energy_partial = True
        if ts:
            days.add(ts[:10])
        if sess:
            sessions.add(sess)
            # Legacy fallback only. Net consumption can decrease during regeneration.
            if uah is not None and dah is None:
                sess_ah[sess] = uah
            if uwh is not None and dwh is None:
                sess_wh[sess] = uwh
        if spd is not None:
            speeds.append(spd)
        if glon is not None and glat is not None:
            gmags.append(math.hypot(glon, glat))
        if lat is not None and lon is not None:
            cur_pt = (lat, lon)
            if prev_pt is not None:
                d = _haversine(prev_pt, cur_pt)
                if d < 1000:  # 튐 방지 (1초에 1km 이상은 무시)
                    total_dist += d
            pts.append([lat, lon])
            prev_pt = cur_pt
        # 고도 상승 누적: 기준고도 히스테리시스 방식.
        # (샘플간 delta 방식은 완만한 등판이 노이즈 문턱에 걸려 0이 되므로 쓰지 않음)
        # 기준보다 ALT_TH 이상 올라가면 그만큼 누적하고 기준을 올림.
        # 내려가면 기준만 낮춤 -> GPS 고도 노이즈(±수 m)는 무시되고 실제 등판만 잡힘.
        if alt is not None:
            if prev_alt is None:
                prev_alt = alt
            elif alt > prev_alt + ALT_TH:
                gain += alt - prev_alt
                prev_alt = alt
            elif alt < prev_alt - ALT_TH:
                prev_alt = alt
        # 주행 시간 누적: t_mono(시계 무관) 우선, 없으면 ts 로 폴백.
        # 세션이 바뀌면 연속성이 끊기므로 누적하지 않는다.
        if tmono is not None:
            if prev_mono is not None and sess == prev_sess:
                dt = tmono - prev_mono
                if 0 < dt < 10:
                    total_time += dt
            prev_mono = tmono
            prev_sess = sess
        else:
            try:
                t = datetime.fromisoformat(ts).timestamp() if ts else None
            except ValueError:
                t = None
            if t is not None and prev_t is not None:
                dt = t - prev_t
                if 0 < dt < 10:  # 10초 이상 벌어지면 다른 주행
                    total_time += dt
            prev_t = t

    legacy_wh, legacy_ah = sum(sess_wh.values()), sum(sess_ah.values())
    if not known_ah:
        tot_ah = legacy_ah
    if not known_wh:
        tot_wh = legacy_wh

    # 궤적 다운샘플 (너무 많으면 지도 부담)
    if len(pts) > 3000:
        step = len(pts) // 3000 + 1
        pts = pts[::step]

    dist_km = total_dist / 1000.0
    day_list = sorted(days)
    return {
        "vehicle": vehicle,
        "name": vehicle_name(vehicle),
        "track": pts,
        "stats": {
            "points": len(rows),
            "first": day_list[0] if day_list else None,
            "last": day_list[-1] if day_list else None,
            "distance_km": round(dist_km, 2),
            "max_speed": round(max(speeds), 1) if speeds else 0,
            "avg_speed": round(sum(speeds) / len(speeds), 1) if speeds else 0,
            "duration_min": round(total_time / 60.0, 1),
            "max_g": round(max(gmags), 2) if gmags else 0,
            "runs": len(sessions) if sessions else len(days),
            "elev_gain_m": round(gain, 0),
            "used_ah": round(tot_ah, 2) if known_ah or sess_ah else None,
            "used_wh": round(tot_wh, 1) if known_wh or sess_wh else None,
            "energy_basis": "unverified"
            if unverified_energy
            else "measured_increments"
            if known_wh and not sess_wh
            else "mixed_legacy_excluded"
            if known_wh
            else "legacy_unverified",
            "legacy_used_wh": legacy_wh if sess_wh else None,
            "legacy_used_ah": legacy_ah if sess_ah else None,
            "energy_partial": energy_partial,
            # 전비: km per Ah / Wh per km (배터리 데이터 있을 때만)
            "km_per_ah": round(dist_km / tot_ah, 2)
            if known_ah
            and not unverified_energy
            and not energy_partial
            and tot_ah > 0.01
            and dist_km
            else None,
            "wh_per_km": round(tot_wh / dist_km, 1)
            if known_wh
            and not unverified_energy
            and not energy_partial
            and tot_wh > 0.1
            and dist_km > 0.01
            else None,
        },
    }


# ============================================================
#  비동기 헬퍼
# ============================================================
async def run_db(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


async def broadcast(payload):
    if not live_clients:
        return
    msg = json.dumps(payload, ensure_ascii=False)
    dead = []
    for ws in live_clients:
        try:
            await ws.send_str(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        live_clients.discard(ws)


# ============================================================
#  WS /ingest — 차량/폰 -> 서버
# ============================================================
async def ws_ingest(request):
    if not hmac.compare_digest(request.headers.get("X-Auth-Token", ""), AUTH_TOKEN):
        return web.Response(status=401, text="unauthorized")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=2 * 1024 * 1024)
    await ws.prepare(request)
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            body = json.loads(msg.data)
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            batch = body.get("records", []) if body.get("type") == "batch" else [body]
            if not isinstance(batch, list) or len(batch) > 500:
                raise ValueError("invalid batch")
            persistent = [
                r
                for r in batch
                if isinstance(r, dict) and not is_ephemeral(r.get("vehicle", ""))
            ]
            result = await run_db(
                request.app[RESEARCH_KEY].ingest, persistent, body.get("events", [])
            )
            who = persistent[0].get("vehicle") if persistent else body.get("vehicle")
            commands = (
                await run_db(
                    request.app[RESEARCH_KEY].commands, who, body.get("receipts", [])
                )
                if who
                else []
            )
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                v = raw.get("vehicle")
                if not v:
                    continue
                if (
                    not is_ephemeral(v)
                    and raw.get("sample_id") not in result["accepted"]
                    and body.get("type") == "batch"
                ):
                    continue
                old = latest.get(v)
                # Backfill must not replace a more recent live snapshot.
                if (
                    old
                    and raw.get("segment_id") == old.get("segment_id")
                    and (raw.get("sequence") or 0) < (old.get("sequence") or 0)
                ):
                    continue
                last_seen[v] = time.time()
                latest[v] = raw
                recent.setdefault(v, deque(maxlen=RECENT_MAX)).append(raw)
                await broadcast(
                    {
                        "type": "live",
                        "vehicle": v,
                        "data": raw,
                        "vehicles": vehicle_list_payload(),
                    }
                )
                break
            if body.get("type") == "batch":
                await ws.send_json(
                    {
                        "type": "ack",
                        "accepted": result["accepted"],
                        "rejected": result["rejected"],
                        "event_ids": result["event_ids"],
                        "commands": commands,
                    }
                )
        except (ValueError, TypeError, KeyError) as exc:
            await ws.send_json({"type": "error", "error": str(exc)})
        except sqlite3.Error:
            await ws.send_json(
                {"type": "error", "error": "durable database write failed"}
            )
    return ws


async def ws_live(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    live_clients.add(ws)
    try:
        # 접속 즉시: 온라인 차량 목록 + 각 차량 최신/최근
        await ws.send_str(
            json.dumps(
                {
                    "type": "init",
                    "vehicles": vehicle_list_payload(),
                    "latest": {v: latest.get(v) for v in online_vehicles()},
                    "recent": {v: list(recent.get(v, [])) for v in online_vehicles()},
                },
                ensure_ascii=False,
            )
        )
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        live_clients.discard(ws)
    return ws


# ============================================================
#  POST /upload — 10Hz raw telem CSV
# ============================================================
async def http_upload(request):
    if not hmac.compare_digest(request.headers.get("X-Auth-Token", ""), AUTH_TOKEN):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        records, rejected = parse_csv(
            await request.text(),
            vehicle=request.query.get("vehicle"),
            session=request.query.get("session"),
        )
        accepted = []
        inserted = 0
        for start in range(0, len(records), 500):
            result = await run_db(
                request.app[RESEARCH_KEY].ingest, records[start : start + 500]
            )
            accepted.extend(result["accepted"])
            rejected.extend(result["rejected"])
            inserted += result["inserted"]
        return web.json_response(
            {
                "received": len(records),
                "accepted": accepted,
                "rejected": rejected,
                "inserted": inserted,
                "session": request.query.get("session"),
                "vehicle": request.query.get("vehicle"),
            }
        )
    except (ValueError, TypeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def api_vehicles(request):
    return web.json_response(
        {"vehicles": await run_db(db_query_vehicles), "online": online_vehicles()}
    )


async def api_sessions(request):
    v = request.query.get("vehicle", "")
    if not v:
        return web.json_response({"error": "vehicle required"}, status=400)
    return web.json_response(await run_db(db_query_sessions, v))


async def api_history(request):
    date_str = request.query.get("date", datetime.now(KST).strftime("%Y-%m-%d"))
    v = request.query.get("vehicle") or None
    rows = await run_db(db_query_history, date_str, v)
    return web.json_response(
        {"date": date_str, "vehicle": v, "count": len(rows), "rows": rows}
    )


async def api_track(request):
    session = request.query.get("session", "")
    if not session:
        return web.json_response({"error": "session required"}, status=400)
    rows = await run_db(db_query_track, session)
    return web.json_response({"session": session, "count": len(rows), "rows": rows})


async def api_session(request):
    """세션 단위 summary 조회 (배터리/고도 포함). 10Hz track 이 없어도 사용 가능."""
    session = request.query.get("session", "")
    vehicle = request.query.get("vehicle") or None
    if not session:
        return web.json_response({"error": "session required"}, status=400)
    rows = await run_db(db_query_session, session, vehicle)
    return web.json_response(
        {"session": session, "vehicle": vehicle, "count": len(rows), "rows": rows}
    )


async def api_cumulative(request):
    v = request.query.get("vehicle", "")
    if not v:
        return web.json_response({"error": "vehicle required"}, status=400)
    return web.json_response(await run_db(db_query_cumulative, v))


async def api_status(request):
    return web.json_response(
        {"online": online_vehicles(), "vehicles": vehicle_list_payload()}
    )


# ---------- 설정 (출발선 등) ----------
async def api_setting_get(request):
    key = request.query.get("key", "")
    if not key:
        return web.json_response({"error": "key required"}, status=400)
    return web.json_response({"key": key, "value": await run_db(db_get_setting, key)})


async def api_setting_set(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "json body required"}, status=400)
    key = body.get("key")
    value = body.get("value")
    if not key:
        return web.json_response({"error": "key required"}, status=400)
    await run_db(
        db_set_setting,
        key,
        json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value,
    )
    return web.json_response({"ok": True, "key": key})


# ---------- 삭제 (이중 확인) ----------
async def api_delete_preview(request):
    """삭제 전 대상 건수 조회. 웹 UI 1차 확인에 사용."""
    v = request.query.get("vehicle", "")
    sess = request.query.get("session") or None
    if not v:
        return web.json_response({"error": "vehicle required"}, status=400)
    return web.json_response(await run_db(db_count_target, v, sess))


async def api_delete(request):
    """실제 삭제. 서버에서도 이중 확인을 강제한다:
    - confirm 필드가 정확히 "DELETE" 여야 함
    - 세션 삭제 시 session 문자열 재입력(verify)이 일치해야 함
    - 차량 전체 삭제 시 verify 가 차량 ID 와 일치해야 함
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "json body required"}, status=400)
    v = body.get("vehicle")
    sess = body.get("session") or None
    confirm = body.get("confirm")
    verify = body.get("verify")
    if not v:
        return web.json_response({"error": "vehicle required"}, status=400)
    if confirm != "DELETE":
        return web.json_response({"error": 'confirm must be "DELETE"'}, status=400)
    expected = sess if sess else v
    if verify != expected:
        return web.json_response(
            {"error": "verify mismatch", "expected": expected}, status=400
        )
    result = await run_db(db_delete_data, v, sess)
    print(f"[DELETE] {v}/{sess or 'ALL'} -> {result}", flush=True)
    return web.json_response({"ok": True, **result})


# ============================================================
#  대시보드
# ============================================================
async def index(request):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    if os.path.exists(path):
        return web.FileResponse(path)
    return web.Response(text="dashboard.html not found", content_type="text/plain")


# ============================================================
#  백그라운드: 오프라인 감시 (차량별)
# ============================================================
async def offline_watcher(app):
    prev = None
    try:
        while True:
            await asyncio.sleep(5)
            cur = tuple(sorted(online_vehicles()))
            if cur != prev:
                await broadcast(
                    {"type": "vehicles", "vehicles": vehicle_list_payload()}
                )
                prev = cur
    except asyncio.CancelledError:
        pass


def authorized(request):
    return AUTH_TOKEN not in (
        "change-me",
        "unimotors-secret-change-me",
    ) and hmac.compare_digest(request.headers.get("X-Auth-Token", ""), AUTH_TOKEN)


async def research_index(request):
    return web.FileResponse(Path(__file__).with_name("research_dashboard.html"))


async def research_live(request):
    return web.json_response(
        {"latest": latest, "received_at": last_seen, "vehicles": vehicle_list_payload()}
    )


async def api_races(request):
    vehicle = request.query.get("vehicle", "car1")
    return web.json_response(
        {
            "vehicle": vehicle,
            "races": await run_db(request.app[RESEARCH_KEY].races, vehicle),
        }
    )


async def api_race_report(request):
    vehicle = request.query.get("vehicle", "car1")
    race = request.query.get("race_session_id")
    if not race:
        return web.json_response({"error": "race_session_id required"}, status=400)
    return web.json_response(
        await run_db(request.app[RESEARCH_KEY].report, vehicle, race)
    )


async def api_command_history(request):
    return web.json_response(
        {
            "commands": await run_db(
                request.app[RESEARCH_KEY].command_history,
                request.query.get("vehicle", "car1"),
            )
        }
    )


async def api_command(request):
    if not authorized(request):
        return web.json_response({"error": "configured token required"}, status=401)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        if body.get("cancel_id"):
            await run_db(request.app[RESEARCH_KEY].cancel_command, body["cancel_id"])
            return web.json_response({"ok": True})
        command = await run_db(request.app[RESEARCH_KEY].create_command, body)
        return web.json_response({"ok": True, "command": command})
    except (ValueError, TypeError, KeyError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def on_startup(app):
    await run_db(db_init)
    app[RESEARCH_KEY] = await run_db(ResearchStore, DB_PATH)
    app[WATCHER_KEY] = asyncio.create_task(offline_watcher(app))
    print(f"[SERVER] telemetry_server v7 on :{PORT}  DB={DB_PATH}", flush=True)


async def on_cleanup(app):
    app[WATCHER_KEY].cancel()
    for ws in list(live_clients):
        await ws.close()


def make_app():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes(
        [
            web.get("/ingest", ws_ingest),
            web.get("/live", ws_live),
            web.post("/upload", http_upload),
            web.get("/api/vehicles", api_vehicles),
            web.get("/api/sessions", api_sessions),
            web.get("/api/history", api_history),
            web.get("/api/track", api_track),
            web.get("/api/session", api_session),
            web.get("/api/cumulative", api_cumulative),
            web.get("/api/status", api_status),
            web.get("/api/setting", api_setting_get),
            web.post("/api/setting", api_setting_set),
            web.get("/api/delete/preview", api_delete_preview),
            web.post("/api/delete", api_delete),
            web.get("/", index),
            web.get("/research", research_index),
            web.get("/api/research/live", research_live),
            web.get("/api/races", api_races),
            web.get("/api/race-report", api_race_report),
            web.get("/api/commands", api_command_history),
            web.post("/api/commands", api_command),
        ]
    )
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=HOST, port=PORT)
