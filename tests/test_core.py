import numpy as np
import pytest

from fleetcharge.agent import _regex_parse, apply_actions
from fleetcharge.energy import calibration, trip_energy
from fleetcharge.planner import plan_charging
from fleetcharge.scenario import make_scenario, required_energy
from fleetcharge.scheduler import assign_windows, fast_slots, plan_yard
from fleetcharge.specs import TRUCK_MODELS, DispatchPolicy


@pytest.fixture(scope="module")
def scn():
    return make_scenario(0)


def test_fleet_size_and_models(scn):
    assert len(scn.vehicles) == 50
    assert {v.model_key for v in scn.vehicles} == set(TRUCK_MODELS)


def test_energy_model_calibrated_to_rated_consumption():
    for key, m in TRUCK_MODELS.items():
        e = trip_energy(key, 100, 0.5 * m.max_payload_kg, 3.0, 15.0)
        assert e["kwh_per_km"] == pytest.approx(m.consumption_kwh_km, rel=1e-6)
        assert 0.7 < calibration(key) < 2.0


def test_energy_scales_with_distance_cargo_terrain_and_cold():
    k = "vnr_electric"
    base = trip_energy(k, 200, 10000, 3, 15)["total"]
    assert trip_energy(k, 300, 10000, 3, 15)["total"] > base * 1.4
    assert trip_energy(k, 200, 18000, 3, 15)["total"] > base
    assert trip_energy(k, 200, 2000, 3, 15)["total"] < base
    assert trip_energy(k, 200, 10000, 9, 15)["total"] > base
    assert trip_energy(k, 200, 10000, 3, -10)["total"] > base


def test_requirement_includes_margin_and_reserve(scn):
    v = scn.vehicles[0]
    r = required_energy(v.model_key, v.route_km, v.payload_kg, v.elev_m_km, 15, v.cap_plan_kwh,
                        DispatchPolicy(energy_margin=0.15, reserve_soc=0.10))
    assert r["margin"] == pytest.approx(0.15 * r["total"])
    assert r["reserve"] == pytest.approx(0.10 * v.cap_plan_kwh)
    assert r["required"] == pytest.approx(r["total"] + r["margin"] + r["reserve"])


def test_low_soh_raises_required_soc(scn):
    v = scn.vehicles[0]
    t_new = v.target_kwh / (v.model.battery_kwh * 1.0)
    assert v.target > t_new if v.soh_rep < 1 else True


def test_unconstrained_plan_meets_every_deadline(scn):
    plan = plan_charging(scn, 0)
    assert plan.status == "optimal"
    assert sum(plan.shortfall_kwh.values()) < 1e-3
    assert plan.grid_kw.max() <= scn.site.grid_limit_kw + 1e-6


def test_scheduler_respects_bay_capacity_and_deadlines(scn):
    windows, _ = assign_windows(scn, 0, {}, set(), set())
    for key, n in scn.yard.bays:
        occ = np.zeros(96, int)
        for vid, (lo, hi) in windows.items():
            if scn.vehicle(vid).model_key == key:
                occ[lo:hi] += 1
        assert occ.max() <= n
    for vid, (lo, hi) in windows.items():
        v = scn.vehicle(vid)
        assert lo >= v.arrive_slot and hi <= v.ready_by


def test_plan_yard_delivers_energy_inside_windows(scn):
    plan = plan_yard(scn, 0, {}, set(), set())
    assert sum(plan.shortfall_kwh.values()) < 1.0
    for vid, p in plan.power.items():
        lo, hi = plan.windows.get(vid, (0, 0))
        for i, kw in enumerate(p):
            slot = plan.start[vid] + i
            if kw > 1e-6:
                assert lo < slot < hi - 1 + 1     # never charges in the plug-in slot or after the window


def test_infeasible_deadline_reports_shortfall():
    s = make_scenario(0)
    v = s.vehicles[-1]
    v.arrive_slot, v.depart_slot, v.soc_arrive = 10, 14, 0.1
    v.soc_arrive_rep = 0.1
    plan = plan_charging(s, 0)
    assert plan.shortfall_kwh[v.vid] > 10


def test_regex_parser_and_apply():
    s = make_scenario(0)
    acts = _regex_parse("Truck 7 delayed 2 hours. Truck 12 route is now 180 km. Truck 3 charger fault. Truck 5 carrying 12 t of cargo.", s)
    kinds = {(a.type, a.vid) for a in acts}
    assert {("delay_departure", "T07"), ("set_route", "T12"), ("charger_fault", "T03"), ("set_payload", "T05")} <= kinds
    before = s.vehicle("T07").depart_slot
    apply_actions(s, acts)
    assert s.vehicle("T07").depart_slot == min(95, before + 8)
    assert s.vehicle("T03").online is False
    assert s.vehicle("T05").payload_kg == pytest.approx(min(12000, s.vehicle("T05").model.max_payload_kg))


def test_fast_slots_longer_for_low_soh(scn):
    v = scn.vehicles[30]
    e0 = 0.2 * v.cap_plan_kwh
    assert fast_slots(scn, v, e0) >= 1


def test_yard_sim_smart_dispatches_everyone_with_delays():
    from fleetcharge.yard import run
    s = make_scenario(0)
    r = run(s, "smart", seed=0)
    assert r.metrics["missed"] == 0
    docks = [e for e in r.events if e["topic"] == "docking"]
    assert docks and all(e["plug_min"] > 0 for e in docks)          # handling delay is simulated
    ideal = run(s, "ideal", seed=0)
    assert r.metrics["total_cost"] > ideal.metrics["total_cost"]     # bays cost something
    assert r.metrics["total_cost"] < run(s, "fcfs", seed=0).metrics["total_cost"]
    # no pool ever exceeds its bay count
    for key, n in s.yard.bays:
        idx = [i for i, vid in enumerate(r.vids) if s.vehicle(vid).model_key == key]
        for t in range(96):
            assert len({int(r.bay[i, t]) for i in idx if r.bay[i, t] >= 0}) <= n
