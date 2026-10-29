"""demo_sim v2: virtual sensors fed into RaceRuntime give the same laps/energy as the sim."""
import math
from types import SimpleNamespace

import pytest

import demo_sim as ds
from test_polarity import make_runtime
from test_runtime import Clock

LAT0, LON0 = 35.5735, 129.1916


def rect_route(w=120, h=80, step=5):
    ky = 111195.0
    kx = ky * math.cos(math.radians(LAT0))
    pts = [(0, x) for x in range(0, w, step)] + [(y, w) for y in range(0, h, step)]
    pts += [(h, x) for x in range(w, 0, -step)] + [(y, 0) for y in range(h, 0, -step)]
    return ds.Route([[LAT0 + y / ky, LON0 + x / kx] for y, x in pts])


def test_lap_line_is_perpendicular_and_centered():
    route = rect_route()
    line = ds.lap_line_at(route, ds.LINE_S, half_width=10)
    lat, lon, hdg, _, _, _ = route.at(ds.LINE_S)
    (a_lat, a_lon), (b_lat, b_lon) = line
    assert (a_lat + b_lat) / 2 == pytest.approx(lat, abs=1e-6)
    assert (a_lon + b_lon) / 2 == pytest.approx(lon, abs=1e-6)
    dn, de = (a_lat - b_lat) * route.ky, (a_lon - b_lon) * route.kx
    assert math.hypot(dn, de) == pytest.approx(20, rel=0.01)
    th = math.radians(hdg)
    assert abs(dn * math.cos(th) + de * math.sin(th)) < 0.5   # ⟂ to heading


def test_profile_budget_matches_ocv_integral():
    route = rect_route()
    prof = ds.demo_profile(SimpleNamespace(soc_floor=10, laps=30), route, 85)
    nominal = ds.PACK["capacity_ah"] * 0.75 * ds.PACK["series"] * 3.7
    assert prof["usable_remaining_wh"] == pytest.approx(nominal, rel=0.08)
    assert prof["event"] == "endurance" and prof["confirmed"] is True


def test_runtime_laps_and_energy_follow_the_virtual_car(tmp_path):
    route = rect_route()
    clock = Clock()
    rt = make_runtime(tmp_path, clock, sign=1)
    try:
        rt.configure(ds.demo_profile(SimpleNamespace(soc_floor=10, laps=30), route, 85))
        rt.set_phase("RACE")
        sim_laps = []
        car = ds.DemoCar(route, seed=1, on_lap=sim_laps.append)
        dt, last_bms, crossings = 0.1, -1.0, []
        while len(sim_laps) < 3:
            before = math.floor((car.dist - ds.LINE_S) / route.L)
            car.step(dt)
            clock.advance(dt)
            if math.floor((car.dist - ds.LINE_S) / route.L) > before:
                crossings.append(car.t)          # sim's own pass over the lap line
            rt.update_gps(dict(car.gps_view(), fix=True))
            if clock.mono - last_bms >= 0.33:
                b = car.bms_view()
                for key in ("power_w", "regen", "state"):
                    b.pop(key, None)
                rt.update_bms(b)
                last_bms = clock.mono
        laps = [x["seconds"] for x in rt.run["laps"] if x.get("complete")]
        assert len(laps) == 2 and rt.run["lap_count"] == 2
        expected = [b - a for a, b in zip(crossings, crossings[1:])]
        assert laps == pytest.approx(expected, abs=2 * dt)   # within GPS sample period
        snap = rt.snapshot()
        assert snap["lap_timing"]["best"] == pytest.approx(min(laps))
        # +1 polarity: runtime-integrated discharge energy equals the car's own integral
        assert snap["used_wh"] > 0
        assert snap["used_wh"] == pytest.approx(car.used_wh, rel=0.03)
    finally:
        rt.close()
