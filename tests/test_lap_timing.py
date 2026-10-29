"""Driver-screen lap clock (RaceRuntime.snapshot()["lap_timing"])."""
import pytest

from test_polarity import make_runtime
from test_runtime import Clock

LINE = {"lap_line": [[35, 128.999], [35, 129.001]], "lap_min_seconds": 1, "lap_departure_m": 1}


# Small rectangle around the line (line spans lon 128.999..129.001 at lat 35); the return
# leg runs at lon 129.0015, outside the line, so only the northbound leg crosses it.
LOOP = [(34.9998, 129.0), (35.0002, 129.0), (35.0002, 129.0015), (34.9998, 129.0015), (34.9998, 129.0)]


def drive_lap(r, c, steps_per_leg):
    """One lap, one GPS fix per second (runtime treats >1.5 s gaps as interrupted)."""
    for (a_lat, a_lon), (b_lat, b_lon) in zip(LOOP, LOOP[1:]):
        for i in range(1, steps_per_leg + 1):
            f = i / steps_per_leg
            r.update_gps({"fix": True, "lat": a_lat + (b_lat - a_lat) * f,
                          "lon": a_lon + (b_lon - a_lon) * f, "speed": 30})
            c.advance(1)


def start(r, c):
    r.update_gps({"fix": True, "lat": LOOP[0][0], "lon": LOOP[0][1], "speed": 30})
    c.advance(1)


def test_no_lap_line_means_no_lap_clock(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c)
    try:
        lt = r.snapshot()["lap_timing"]
        assert lt["line"] is False and lt["current"] is None
        assert lt["last"] is None and lt["best"] is None and lt["number"] == 1
    finally:
        r.close()


def test_lap_clock_counts_current_last_best(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c)
    try:
        r.configure(LINE)
        r.set_phase("RACE")
        start(r, c)
        drive_lap(r, c, 6)                   # crosses once: lap 1 starts, not yet timed
        lt = r.snapshot()["lap_timing"]
        assert lt["line"] and lt["number"] == 1 and lt["last"] is None
        assert lt["current"] is not None and lt["current"] > 0

        drive_lap(r, c, 6)                   # lap 1 completes
        drive_lap(r, c, 5)                   # lap 2 faster
        drive_lap(r, c, 7)                   # lap 3 slower
        lt = r.snapshot()["lap_timing"]
        laps = [x["seconds"] for x in r.run["laps"]]
        assert len(laps) == 3 and r.run["lap_count"] == 3
        assert lt["number"] == 4
        assert len(set(laps)) > 1            # pace changed between laps
        assert lt["best"] == pytest.approx(min(laps))
        assert lt["last"] == pytest.approx(laps[-1])
        assert lt["last_delta"] == pytest.approx(laps[-1] - min(laps))
    finally:
        r.close()


def test_interrupted_and_formation_laps_are_not_timed(tmp_path):
    c = Clock()
    r = make_runtime(tmp_path, c)
    try:
        r.configure(LINE)
        r.set_phase("FORMATION")
        start(r, c)
        drive_lap(r, c, 6)
        drive_lap(r, c, 6)                   # formation lap: excluded from last/best
        assert r.snapshot()["lap_timing"]["best"] is None
        r.set_phase("RACE")
        r.interrupted_lap = True             # e.g. signal loss mid-lap
        drive_lap(r, c, 6)
        lt = r.snapshot()["lap_timing"]
        assert lt["best"] is None and lt["last"] is None
        c.advance(3)                         # GPS silent > TTL: current lap no longer timed
        r.update_gps({"fix": True, "lat": 34.9998, "lon": 129.0, "speed": 30})
        assert r.snapshot()["lap_timing"]["current"] is None
    finally:
        r.close()
