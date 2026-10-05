"""Durable canonical records, full backfill, team commands and race reports."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    from telemetry_protocol import FIELDS, TEXT_FIELDS, normalize_sample, finite
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
    from telemetry_protocol import FIELDS, TEXT_FIELDS, normalize_sample, finite


class ResearchStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records_v7(id TEXT PRIMARY KEY, vehicle TEXT,
                    race TEXT, segment TEXT, payload TEXT, received REAL);
                CREATE INDEX IF NOT EXISTS records_v7_race ON records_v7(vehicle,race);
                CREATE TABLE IF NOT EXISTS events_v7(id TEXT PRIMARY KEY, vehicle TEXT,
                    race TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS race_links(vehicle TEXT, alias TEXT, parent TEXT,
                    PRIMARY KEY(vehicle,alias));
                CREATE TABLE IF NOT EXISTS team_commands(id TEXT PRIMARY KEY, vehicle TEXT,
                    payload TEXT, status TEXT);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "cancel_acked" not in {
                r[1] for r in db.execute("PRAGMA table_info(team_commands)")
            }:
                db.execute(
                    "ALTER TABLE team_commands ADD COLUMN cancel_acked INTEGER DEFAULT 0"
                )
            for table in ("summary", "track"):
                columns = {r[1] for r in db.execute("PRAGMA table_info(" + table + ")")}
                if not columns:
                    continue
                for key in FIELDS:
                    if key not in columns:
                        db.execute(
                            "ALTER TABLE "
                            + table
                            + " ADD COLUMN "
                            + key
                            + (" TEXT" if key in TEXT_FIELDS else " REAL")
                        )
                self._migrate_identity(db, table)

    def _migrate_identity(self, db, table):
        info = list(db.execute("PRAGMA table_info(" + table + ")"))
        if [r[1] for r in info if r[5]] == ["sample_id"]:
            return
        temporary = table + "_identity_v7"
        columns = [r[1] for r in info]
        definitions = []
        for row in info:
            name = '"' + row[1].replace('"', '""') + '"'
            definitions.append(
                name
                + (
                    " TEXT PRIMARY KEY NOT NULL"
                    if row[1] == "sample_id"
                    else " " + (row[2] or "TEXT")
                )
            )
        db.execute("CREATE TABLE " + temporary + "(" + ",".join(definitions) + ")")
        source = db.execute("SELECT * FROM " + table)
        for row in source:
            old = dict(zip(columns, row))
            if not old.get("sample_id"):
                old["sample_id"] = normalize_sample(
                    {
                        "vehicle": old.get("vehicle") or "unknown",
                        "session": old.get("session"),
                        "ts": old.get("ts") or "unknown",
                    }
                )["sample_id"]
            db.execute(
                "INSERT INTO "
                + temporary
                + "("
                + ",".join(columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")",
                [old[k] for k in columns],
            )
        db.execute("DROP TABLE " + table)
        db.execute("ALTER TABLE " + temporary + " RENAME TO " + table)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_"
            + table
            + "_v_ts ON "
            + table
            + "(vehicle,ts)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_"
            + table
            + "_session ON "
            + table
            + "(session)"
        )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, records, events=()):
        if not isinstance(records, list) or len(records) > 500:
            raise ValueError("batch must contain at most 500 records")
        accepted, rejected, normalized, event_ids = [], [], [], []
        inserted = 0
        with self.connect() as db:
            for raw in records:
                try:
                    record = normalize_sample(raw)
                    encoded = json.dumps(
                        record, ensure_ascii=False, sort_keys=True, allow_nan=False
                    )
                    old = db.execute(
                        "SELECT payload FROM records_v7 WHERE id=?",
                        (record["sample_id"],),
                    ).fetchone()
                    if old and old[0] != encoded:
                        raise ValueError("sample id reused with different data")
                    cursor = db.execute(
                        "INSERT OR IGNORE INTO records_v7 VALUES(?,?,?,?,?,?)",
                        (
                            record["sample_id"],
                            record["vehicle"],
                            record["race_session_id"] or record["session"],
                            record["segment_id"] or record["session"],
                            encoded,
                            time.time(),
                        ),
                    )
                    inserted += cursor.rowcount
                    columns = ",".join(FIELDS)
                    placeholders = ",".join("?" for _ in FIELDS)
                    values = [record.get(k) for k in FIELDS]
                    for table in ("summary", "track"):
                        db.execute(
                            "INSERT OR IGNORE INTO "
                            + table
                            + "("
                            + columns
                            + ") VALUES("
                            + placeholders
                            + ")",
                            values,
                        )
                    accepted.append(record["sample_id"])
                    normalized.append(record)
                except (ValueError, TypeError) as exc:
                    rejected.append(
                        {
                            "sample_id": raw.get("sample_id")
                            if isinstance(raw, dict)
                            else None,
                            "error": str(exc),
                        }
                    )
            if not isinstance(events, list) and events != ():
                raise ValueError("events must be an array")
            if len(events) > 200:
                raise ValueError("too many events")
            for event in events:
                if not isinstance(event, dict) or not all(
                    event.get(k) for k in ("id", "vehicle", "race_session_id", "kind")
                ):
                    raise ValueError("invalid event")
                encoded = json.dumps(
                    event, ensure_ascii=False, sort_keys=True, allow_nan=False
                )
                if len(encoded) > 32768:
                    raise ValueError("event too large")
                old = db.execute(
                    "SELECT payload FROM events_v7 WHERE id=?", (event["id"],)
                ).fetchone()
                if old and json.loads(old[0]) != event:
                    raise ValueError("event id reused with different data")
                db.execute(
                    "INSERT OR IGNORE INTO events_v7 VALUES(?,?,?,?)",
                    (event["id"], event["vehicle"], event["race_session_id"], encoded),
                )
                if event["kind"] == "resume":
                    detail = event.get("detail", {})
                    aliases = detail.get("provisional_ids") or [
                        detail.get("provisional")
                    ]
                    parent = event["race_session_id"]
                    if not isinstance(aliases, list) or len(aliases) > 100:
                        raise ValueError("invalid race aliases")
                    for alias in aliases:
                        if alias and alias != parent:
                            previous = db.execute(
                                "SELECT parent FROM race_links WHERE vehicle=? AND alias=?",
                                (event["vehicle"], alias),
                            ).fetchone()
                            if previous and previous[0] != parent:
                                raise ValueError(
                                    "race alias already has another parent"
                                )
                            if parent in self.aliases(db, event["vehicle"], alias):
                                raise ValueError("race alias cycle")
                            db.execute(
                                "INSERT OR REPLACE INTO race_links VALUES(?,?,?)",
                                (event["vehicle"], alias, parent),
                            )
                event_ids.append(event["id"])
        # Return only after the transaction has committed.
        return {
            "accepted": accepted,
            "rejected": rejected,
            "event_ids": event_ids,
            "inserted": inserted,
            "records": normalized,
        }

    def create_command(self, body, now=None):
        now = time.time() if now is None else now
        vehicle, race, message = (
            body.get(k) for k in ("vehicle", "race_session_id", "message")
        )
        ttl = finite(body.get("ttl_seconds", 30))
        if (
            not all(isinstance(x, str) and 1 <= len(x) <= 160 for x in (vehicle, race))
            or not isinstance(message, str)
            or not 1 <= len(message) <= 120
            or ttl is None
            or not 1 <= ttl <= 300
        ):
            raise ValueError("vehicle, race, message and TTL 1..300 are required")
        command = dict(
            id=uuid.uuid4().hex,
            vehicle=vehicle,
            race_session_id=race,
            message=message,
            issued_at=now,
            expires_at=now + ttl,
            source="TEAM",
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO team_commands(id,vehicle,payload,status) VALUES(?,?,?,?)",
                (
                    command["id"],
                    vehicle,
                    json.dumps(command, ensure_ascii=False),
                    "queued",
                ),
            )
        return command

    def commands(self, vehicle, receipts=()):
        with self.connect() as db:
            if not isinstance(receipts, (tuple, list)) or len(receipts) > 100:
                raise ValueError("invalid command receipts")
            for receipt in receipts:
                if not isinstance(receipt, dict):
                    raise ValueError("receipt must be object")
                if receipt.get("status") in {"received", "seen", "cancelled"}:
                    if receipt["status"] == "cancelled":
                        db.execute(
                            "UPDATE team_commands SET cancel_acked=1 WHERE id=? AND vehicle=?",
                            (receipt.get("id"), vehicle),
                        )
                    else:
                        db.execute(
                            "UPDATE team_commands SET status=? WHERE id=? AND vehicle=? AND status!='cancelled'",
                            (receipt["status"], receipt.get("id"), vehicle),
                        )
            rows = db.execute(
                "SELECT payload,status,cancel_acked FROM team_commands WHERE vehicle=? ORDER BY rowid DESC LIMIT 50",
                (vehicle,),
            ).fetchall()
        now = time.time()
        return [
            dict(json.loads(p), status=s, cancelled=s == "cancelled")
            for p, s, acked in rows
            if (s == "cancelled" and not acked)
            or (s not in {"seen", "cancelled"} and json.loads(p)["expires_at"] >= now)
        ]

    def cancel_command(self, identifier):
        with self.connect() as db:
            db.execute(
                "UPDATE team_commands SET status='cancelled' WHERE id=?", (identifier,)
            )

    def command_history(self, vehicle):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload,status FROM team_commands WHERE vehicle=? ORDER BY rowid DESC LIMIT 100",
                (vehicle,),
            ).fetchall()
        return [
            dict(
                json.loads(payload),
                status=status,
                expired=json.loads(payload)["expires_at"] < time.time(),
            )
            for payload, status in rows
        ]

    def aliases(self, db, vehicle, race):
        aliases = {race}
        links = db.execute(
            "SELECT alias,parent FROM race_links WHERE vehicle=?", (vehicle,)
        ).fetchall()
        for _ in range(len(links) + 1):
            previous = len(aliases)
            aliases.update(alias for alias, parent in links if parent in aliases)
            if len(aliases) == previous:
                break
        return sorted(aliases)

    def report(self, vehicle, race):
        with self.connect() as db:
            aliases = self.aliases(db, vehicle, race)
            placeholders = ",".join("?" for _ in aliases)
            records = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM records_v7 WHERE vehicle=? AND race IN ("
                    + placeholders
                    + ") ORDER BY rowid",
                    [vehicle] + aliases,
                )
            ]
            events = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM events_v7 WHERE vehicle=? AND race IN ("
                    + placeholders
                    + ") ORDER BY rowid",
                    [vehicle] + aliases,
                )
            ]
        segments = {}
        parents = {}
        for r in records:
            mono = finite(r.get("t_mono"))
            if mono is not None:
                segments.setdefault(r["segment_id"] or r["session"], []).append(mono)
            try:
                meta = json.loads(r.get("meta") or "{}")
                parents[r["segment_id"] or r["session"]] = meta.get("segment_parent")
            except (ValueError, TypeError):
                pass
        order = {key: index for index, key in enumerate(segments)}

        def depth(key, seen=None):
            seen = set() if seen is None else seen
            if key in seen or len(seen) > 100:
                return 0
            seen.add(key)
            parent = parents.get(key)
            return 1 + depth(parent, seen) if parent in parents else 0

        records.sort(
            key=lambda r: (
                depth(r["segment_id"] or r["session"]),
                order.get(r["segment_id"] or r["session"], 0),
                r.get("sequence") or r.get("t_mono") or 0,
            )
        )
        measured_seconds = sum(max(v) - min(v) for v in segments.values() if v)
        return dict(
            vehicle=vehicle,
            race_session_id=race,
            aliases=aliases,
            points=len(records),
            measured_segment_seconds=measured_seconds,
            measured_increment_wh=sum(
                r.get("energy_increment_wh") or 0 for r in records
            ),
            latest_used_wh=records[-1].get("used_wh") if records else None,
            sample_count=len(records),
            events=events,
            sectors=[e["detail"] for e in events if e["kind"] == "sector"],
            timeline=records[:: max(1, (len(records) + 1999) // 2000)],
            note="TEAM estimate; power-off gaps are not measured; official timing and meter prevail",
        )

    def races(self, vehicle):
        with self.connect() as db:
            rows = db.execute(
                "SELECT race,COUNT(*) FROM records_v7 WHERE vehicle=? GROUP BY race ORDER BY MAX(rowid) DESC",
                (vehicle,),
            ).fetchall()
            links = dict(
                db.execute(
                    "SELECT alias,parent FROM race_links WHERE vehicle=?", (vehicle,)
                )
            )
        totals = {}
        for race, count in rows:
            seen = set()
            while race in links and race not in seen:
                seen.add(race)
                race = links[race]
            totals[race] = totals.get(race, 0) + count
        return [{"race_session_id": r, "points": n} for r, n in totals.items()]
