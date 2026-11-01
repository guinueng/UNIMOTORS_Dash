"""Game-world traces and exact track/layout map matching; never geographic GPS.

The point index is derived from canonical v7 samples and committed with them.
Maps may be a licensed reference line, a learned full lap, or a calibrated image.
"""
import base64
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

VIEWER = Path(__file__).with_name("game_track_viewer.html")


def name(value):
    return " ".join(str(value or "").split())[:128]


def track_key(track, variation):
    if not name(track):
        return None
    normalized = [unicodedata.normalize("NFKC", name(s)).casefold() for s in (track, variation)]
    return "pcars1-" + hashlib.sha256(json.dumps(normalized, ensure_ascii=False).encode()).hexdigest()[:24]


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_map(value):
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("지도 형식 version=1이 필요합니다")
    track, variation = name(value.get("track")), name(value.get("variation"))
    if not track:
        raise ValueError("서킷 이름이 필요합니다")
    paths = value.get("paths", [])
    if not isinstance(paths, list) or len(paths) > 100:
        raise ValueError("invalid map paths")
    checked, count = [], 0
    for path in paths:
        if not isinstance(path, list) or len(path) < 2:
            raise ValueError("each map path needs at least two [x,z] points")
        points = []
        for p in path:
            if not isinstance(p, (list, tuple)) or len(p) != 2 or not all(finite(v) and abs(v) <= 1e6 for v in p):
                raise ValueError("invalid game-world map coordinate")
            points.append(list(p))
        checked.append(points)
        count += len(points)
    if count > 20000:
        raise ValueError("map has too many points")
    image = value.get("image")
    if image is not None:
        if not isinstance(image, dict):
            raise ValueError("invalid map image")
        url, bounds = image.get("data_url", ""), image.get("bounds")
        if not isinstance(url, str) or len(url) > 1_400_000:
            raise ValueError("image too large (maximum 1 MB)")
        match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", url)
        if not match:
            raise ValueError("map image must be embedded PNG/JPEG; external URLs/SVG are not accepted")
        try:
            raw = base64.b64decode(match[2], validate=True)
        except ValueError as exc:
            raise ValueError("invalid image encoding") from exc
        if len(raw) > 1_048_576 or not raw.startswith(b"\x89PNG\r\n\x1a\n" if match[1] == "png" else b"\xff\xd8\xff"):
            raise ValueError("invalid PNG/JPEG image")
        if not isinstance(bounds, list) or len(bounds) != 4 or not all(finite(v) and abs(v) <= 1e6 for v in bounds) or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise ValueError("image bounds must be [minX,minZ,maxX,maxZ] in game coordinates")
        image = dict(data_url=url, bounds=bounds)
    if not checked and image is None:
        raise ValueError("a path or calibrated background image is required")
    return dict(version=1, key=track_key(track, variation), track=track, variation=variation,
                label=name(value.get("label") or track + " " + variation),
                kind=name(value.get("kind") or "custom"),
                source=str(value.get("source") or "사용자 등록 도면")[:1000],
                paths=checked, image=image)


@lru_cache(maxsize=1)
def builtins():
    path = Path(__file__).with_name("pcars_track_maps.json")
    return [validate_map(m) for m in json.loads(path.read_text(encoding="utf8"))]


def ensure_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS game_track_points(
            id TEXT PRIMARY KEY, vehicle TEXT, race TEXT, segment TEXT, frame INTEGER,
            track_key TEXT, track TEXT, variation TEXT, ts TEXT, t REAL,
            x REAL,y REAL,z REAL,speed REAL,lap INTEGER,completed INTEGER,
            lap_distance REAL,track_length REAL,invalid INTEGER,part INTEGER);
        CREATE INDEX IF NOT EXISTS game_points_race ON game_track_points(vehicle,race);
        CREATE INDEX IF NOT EXISTS game_points_map ON game_track_points(track_key);
        CREATE UNIQUE INDEX IF NOT EXISTS game_points_frame
            ON game_track_points(vehicle,race,segment,frame);
        CREATE TABLE IF NOT EXISTS game_track_maps(key TEXT PRIMARY KEY,payload TEXT);
    """)


def ingest_points(db, records):
    """Use the caller's transaction, so a committed ACK includes the coordinate index."""
    for r in records:
        try:
            meta = r.get("meta") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            source = meta.get("input", {})
            f, c = source.get("frame", {}), source.get("context", {})
            if source.get("kind") != "game" or source.get("transport") != "pcars1_udp_v1" or not f.get("active"):
                continue
            coords = [f.get("world_" + axis) for axis in "xyz"]
            if not all(finite(v) and -32768 <= v <= 32767 for v in coords):
                continue
            # Bound secondary fields too: meta is accepted as JSON text by the v7 protocol.
            frame, part = source.get("frame_index"), f.get("path_part", 0)
            if not all(isinstance(v, int) and 0 <= v <= 2**53 for v in (frame, part)) or not finite(r.get("t_mono")):
                continue
            numbers = [f.get(k) for k in ("speed_kmh", "current_lap", "laps_completed", "lap_distance", "track_length")]
            if any(v is not None and (not finite(v) or abs(v) > 1e7) for v in numbers):
                continue
            track, variation = name(c.get("track")), name(c.get("variation"))
            db.execute("INSERT OR IGNORE INTO game_track_points VALUES(" + ",".join("?" for _ in range(20)) + ")",
                       (r["sample_id"], r["vehicle"], r["race_session_id"], r["segment_id"], frame,
                        track_key(track, variation), track, variation, r.get("ts"), r["t_mono"],
                        *coords, *numbers[:3], numbers[3], numbers[4], int(bool(f.get("lap_invalid"))), part))
        except (ValueError, TypeError, KeyError, AttributeError):
            # Malformed optional game metadata must not reject an otherwise valid sensor sample.
            continue


def races(db, vehicle):
    rows = db.execute("SELECT race,track_key,track,variation,COUNT(*),MIN(ts),MAX(ts) "
                      "FROM game_track_points WHERE vehicle=? GROUP BY race,track_key ORDER BY MAX(rowid) DESC LIMIT 100", (vehicle,))
    return [dict(race_session_id=r[0], key=r[1], track=r[2], variation=r[3], count=r[4], started=r[5], ended=r[6]) for r in rows]


def save_map(db, value):
    m = validate_map(value)
    # User imports are always identified as such, regardless of claimed JSON kind.
    m["kind"] = "custom"
    db.execute("INSERT OR REPLACE INTO game_track_maps VALUES(?,?)", (m["key"], json.dumps(m, ensure_ascii=False)))
    return m


def learned_map(db, key):
    # Only continuous, valid laps observed near both ends of native lap distance
    # can become a saved background; a partial/out lap must not look complete.
    candidates = db.execute("SELECT vehicle,race,segment,part,lap FROM game_track_points "
                            "WHERE track_key=? AND lap IS NOT NULL GROUP BY vehicle,race,segment,part,lap "
                            "HAVING COUNT(*)>=20 AND MAX(invalid)=0 AND MIN(track_length)>100 "
                            "AND MIN(lap_distance)<=MAX(100,MIN(track_length)*.02) "
                            "AND MAX(lap_distance)>=MAX(track_length)*.95 "
                            "ORDER BY MAX(rowid) DESC LIMIT 10", (key,)).fetchall()
    for vehicle, race, segment, part, lap in candidates:
        done = db.execute("SELECT 1 FROM game_track_points WHERE vehicle=? AND race=? AND track_key=? "
                          "AND lap>? AND completed>=? LIMIT 1", (vehicle, race, key, lap, lap)).fetchone()
        rows = db.execute("SELECT x,z,t,track,variation FROM game_track_points WHERE vehicle=? AND race=? "
                          "AND segment=? AND part=? AND lap=? AND track_key=? ORDER BY rowid",
                          (vehicle, race, segment, part, lap, key)).fetchall()
        if not done or len(rows) < 20 or any(b[2] - a[2] > 2 or b[2] < a[2] or math.hypot(b[0]-a[0], b[1]-a[1]) > 200 for a, b in zip(rows, rows[1:])):
            continue
        step = max(1, math.ceil(len(rows) / 3000))
        points = [[r[0], r[1]] for r in rows[::step]]
        if points[-1] != list(rows[-1][:2]):
            points.append(list(rows[-1][:2]))
        m = validate_map(dict(version=1, track=rows[0][3], variation=rows[0][4], paths=[points],
                              kind="learned", source="이 수신기에서 기록한 연속 유효 완주 랩 · 공식 서킷 경계가 아님"))
        db.execute("INSERT OR IGNORE INTO game_track_maps VALUES(?,?)", (key, json.dumps(m, ensure_ascii=False)))
        return m
    return None


def find_map(db, key):
    row = db.execute("SELECT payload FROM game_track_maps WHERE key=?", (key,)).fetchone()
    if row:
        return json.loads(row[0])
    for m in builtins():
        if m["key"] == key:
            return m
    return learned_map(db, key) if key else None


def trace(db, vehicle, race, key=None, limit=12000):
    available = [r for r in races(db, vehicle) if r["race_session_id"] == race]
    if key is None and available:
        key = available[0]["key"]
    rows = db.execute("SELECT x,y,z,speed,lap,ts,t,segment,part,frame,invalid "
                      "FROM game_track_points WHERE vehicle=? AND race=? AND track_key IS ? ORDER BY rowid LIMIT 200000",
                      (vehicle, race, key)).fetchall()
    stored_total = db.execute("SELECT COUNT(*) FROM game_track_points WHERE vehicle=? AND race=? AND track_key IS ?", (vehicle, race, key)).fetchone()[0]
    # Allocate groups before thinning. Never draw a connector across a dropout,
    # teleport, lap boundary or a different process/position continuity part.
    points, previous, group = [], None, 0
    for r in rows:
        if previous and (r[7:9] != previous[7:9] or r[4] != previous[4] or r[6] - previous[6] > 2 or r[6] < previous[6]
                         or math.hypot(r[0]-previous[0], r[2]-previous[2]) > 200):
            group += 1
        points.append(dict(x=r[0], y=r[1], z=r[2], speed=r[3], lap=r[4], ts=r[5], t=r[6], group=group, frame=r[9], invalid=bool(r[10])))
        previous = r
    total = len(points)
    # Retain original group IDs while thinning, so missing groups never gain connectors.
    limit = max(100, min(20000, int(limit)))
    if total > limit:
        step = math.ceil(total / limit)
        points = points[::step] + ([points[-1]] if (total-1) % step else [])
    return dict(vehicle=vehicle, race_session_id=race, key=key,
                track=next((r for r in available if r["key"] == key), None),
                points=points, total=stored_total, thinned=stored_total > len(points),
                capped=stored_total > total,
                map=find_map(db, key), coordinate_system="pcars1_world_xz_int16_not_gps")
