"""Yard digital twin: bay pools, a waiting lot, plug-in / unplug delays, noisy
SOC/SOH telemetry, edge agents, and four dispatch policies.

Truck life cycle:  away -> waiting -> moving_in (waiting lot -> bay, connect)
                   -> charging -> unplugging (disconnect, move to gate) -> ready -> dispatched

Policies
  fcfs   first come, first served, flat-out charging
  edf    earliest departure first, flat-out charging
  smart  bay scheduler (MILP windows) + LP charge power, re-planned on events
  ideal  every truck has its own charger and no handling delays (upper bound)

Physics runs on *true* SOC/SOH and 1-minute steps; the controller only sees the
noisy values trucks report back (`Telemetry`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .planner import Plan, plan_charging
from .scenario import (DT_H, START_HOUR, T_SLOTS, TELEMETRY, Scenario, Vehicle, clone_scenario,
                       slot_to_clock)
from .scheduler import plan_yard

SUBSTEPS = 15
SUB_H = DT_H / SUBSTEPS
POLICIES = ("fcfs", "edf", "smart", "ideal")
LABELS = {"fcfs": "First come, first served", "edf": "Earliest departure first",
          "smart": "Smart scheduler", "ideal": "Ideal (a charger per truck)"}

# state codes used in logs and by the UI
AWAY, WAITING, MOVING_IN, CHARGING, UNPLUGGING, READY, GONE = range(7)
STATE_NAMES = ["away", "waiting", "moving_in", "charging", "unplugging", "ready", "gone"]


class TelemetryBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, topic: str, slot: int, minute: int = 0, **payload) -> None:
        self.events.append({"topic": topic, "slot": slot, "time": slot_to_clock(slot, minute),
                            **payload})


class TruckAgent:
    """Edge agent: owns the true battery state and enforces local limits."""

    def __init__(self, v: Vehicle, s: Scenario, rng: np.random.Generator) -> None:
        self.v, self.s, self.rng = v, s, rng
        self.cap = v.cap_true_kwh
        self.pmax = v.max_kw_true
        self.energy = v.soc_arrive * self.cap
        self.temp_c = 25.0
        self.state = AWAY
        self.bay = -1
        self.timer = 0.0
        self.docked_at: float | None = None
        self.energy_from_grid = 0.0

    @property
    def soc(self) -> float:
        return self.energy / self.cap

    def report_soc(self) -> float:
        """What the truck tells the yard controller: noisy, clipped SOC."""
        return float(np.clip(self.soc + self.rng.normal(0, TELEMETRY.soc_noise), 0.0, 1.0))

    def taper_limit(self) -> float:
        h = self.s.health
        if self.soc <= h.taper_start_soc:
            return self.pmax
        c = (1 - h.taper_floor) / (1 - h.taper_start_soc)
        return self.pmax * max(h.taper_floor, h.taper_floor + c * (1 - self.soc))

    def delay_minutes(self, which: str) -> float:
        lo, hi = self.v.model.plug_min if which == "plug" else self.v.model.unplug_min
        return float(self.rng.uniform(lo, hi))

    def plan_deficit_kw(self, plan: Plan | None, slot: int, sub: int) -> float:
        """Extra power if reality drifted behind the plan; zero while on track."""
        if plan is None:
            return 0.0
        eta = self.s.site.charger_efficiency
        series = plan.power.get(self.v.vid)
        if series is None:
            return 0.0
        i0 = slot - plan.start.get(self.v.vid, slot)
        planned = eta * float(series[max(i0, 0):].sum()) * DT_H
        if 0 <= i0 < len(series):
            planned -= eta * float(series[i0]) * sub * SUB_H
        deficit = self.v.target_kwh + 0.004 * self.cap - self.energy - planned
        if deficit <= 0.01:
            return 0.0
        remaining_h = max((self.v.ready_by - slot) * DT_H - sub * SUB_H, SUB_H)
        return deficit / (eta * remaining_h)

    def step(self, requested_kw: float, scale: float) -> float:
        """One minute of charging; returns grid-side power drawn."""
        if self.state != CHARGING or not self.v.online:
            self.temp_c += (25.0 - self.temp_c) * 0.05
            return 0.0
        p = min(requested_kw, self.taper_limit()) * scale
        if self.temp_c > 45.0:
            p *= 0.5
        eta = self.s.site.charger_efficiency
        room = (max(self.s.health.soc_ceiling * self.cap, self.v.target_kwh) - self.energy) / (eta * SUB_H)
        p = max(0.0, min(p, room))
        self.energy += eta * p * SUB_H
        self.energy_from_grid += p * SUB_H
        crate = eta * p / self.cap
        self.temp_c += (25.0 + 55.0 * crate - self.temp_c) * (SUB_H / (20 / 60))
        return p


@dataclass
class SimResult:
    policy: str
    metrics: dict
    vehicles: pd.DataFrame
    slots: pd.DataFrame
    states: np.ndarray              # [truck, slot] state code at end of slot
    soc: np.ndarray                 # [truck, slot] true SOC (nan while away)
    power: np.ndarray               # [truck, slot] mean grid kW
    bay: np.ndarray                 # [truck, slot] bay index or -1
    qpos: np.ndarray                # [truck, slot] queue position (0 = not queued)
    events: list[dict]
    plans: int = 0
    plan_time_s: float = 0.0
    vids: list[str] = field(default_factory=list)


def _tier_cost(s: Scenario, v: Vehicle, p_grid_kw: float) -> float:
    cap, remaining, prev, cost = v.cap_true_kwh, p_grid_kw, 0.0, 0.0
    tiers = s.health.c_rate_tiers
    for i, (ub_c, rate) in enumerate(tiers):
        top = ub_c * cap if i < len(tiers) - 1 else float("inf")
        width = max(0.0, top - prev)
        used = min(remaining, width)
        cost += used * rate
        remaining -= used
        prev = max(prev, top)
        if remaining <= 0:
            break
    return cost


def run(scn: Scenario, policy: str, replan_slots: int = 4, fault_rate: float = 0.0,
        seed: int = 0, on_event=None, time_limit: float = 5.0) -> SimResult:
    assert policy in POLICIES
    s = clone_scenario(scn)
    rng = np.random.default_rng(seed + 1000)
    bus = TelemetryBus()
    ideal = policy == "ideal"
    pool_n = {k: (999 if ideal else n) for k, n in s.yard.bays}
    agents = {v.vid: TruckAgent(v, s, rng) for v in s.vehicles}
    order = [v.vid for v in s.vehicles]
    n = len(order)
    ST = np.zeros((n, T_SLOTS), int)
    SOC = np.full((n, T_SLOTS), np.nan)
    PW = np.zeros((n, T_SLOTS))
    BY = np.full((n, T_SLOTS), -1, int)
    QP = np.zeros((n, T_SLOTS), int)
    grid_slot, load_slot, solar_slot = np.zeros(T_SLOTS), np.zeros(T_SLOTS), np.zeros(T_SLOTS)
    energy_cost = health_cost = hold_cost = 0.0
    crate_w = kwh_w = fast_kwh = hi_dwell_h = derate_min = peak_crate = 0.0
    bay_slots_used = 0
    outcomes: dict[str, dict] = {}
    waits: dict[str, float] = {}
    plan: Plan | None = None
    last_plan_slot = -99
    plan_runs, plan_time = 0, 0.0
    peak_so_far = 0.0
    dirty = True
    fault_until: dict[str, int] = {}
    bay_owner: dict[str, list[str | None]] = {k: [None] * n_b for k, n_b in pool_n.items()}

    def emit(topic: str, slot: int, minute: int = 0, **kw) -> None:
        bus.publish(topic, slot, minute, **kw)
        if on_event:
            on_event(bus.events[-1])

    def release_bay(ag: TruckAgent) -> None:
        if ag.bay >= 0:
            bay_owner[ag.v.model_key][ag.bay] = None
            ag.bay = -1

    def dock(ag: TruckAgent, slot: int, minute: int) -> bool:
        owners = bay_owner[ag.v.model_key]
        for b, o in enumerate(owners):
            if o is None:
                owners[b] = ag.v.vid
                ag.bay = b
                ag.state = MOVING_IN
                ag.timer = 0.0 if ideal else ag.delay_minutes("plug")
                ag.docked_at = slot + minute / 15
                waits[ag.v.vid] = (ag.docked_at - ag.v.arrive_slot) * DT_H
                emit("docking", slot, minute, vid=ag.v.vid, bay=b + 1, plug_min=round(ag.timer, 1),
                     waited_h=round(waits[ag.v.vid], 2))
                return True
        return False

    def queue_order(key: str) -> list[TruckAgent]:
        w = [ag for ag in agents.values() if ag.state == WAITING and ag.v.model_key == key]
        if policy == "fcfs":
            return sorted(w, key=lambda a: (a.v.arrive_slot, a.v.vid))
        if policy == "edf":
            return sorted(w, key=lambda a: (a.v.depart_slot, a.v.vid))
        if plan is not None and plan.windows is not None:
            return sorted(w, key=lambda a: (plan.windows.get(a.v.vid, (999, 0))[0], a.v.depart_slot))
        return sorted(w, key=lambda a: (a.v.depart_slot, a.v.vid))

    def do_replan(slot: int) -> None:
        nonlocal plan, last_plan_slot, plan_runs, plan_time, dirty
        energy = {vid: ag.report_soc() * ag.v.cap_plan_kwh for vid, ag in agents.items()
                  if ag.state in (WAITING, MOVING_IN, CHARGING, UNPLUGGING)}
        in_bay = {vid for vid, ag in agents.items() if ag.state in (MOVING_IN, CHARGING, UNPLUGGING)}
        exclude = {vid for vid, ag in agents.items() if ag.state in (READY, GONE)}
        if policy == "ideal":
            # a charger per truck, but keep the same plug/unplug cushion the yard policies have
            win = {v.vid: (max(v.arrive_slot, slot), v.ready_by - 1) for v in s.vehicles}
            plan = plan_charging(s, slot, energy, peak_so_far, windows=win, exclude=exclude)
        else:
            plan = plan_yard(s, slot, energy, in_bay, exclude, peak_so_far, time_limit=time_limit)
        plan_runs += 1
        plan_time += plan.solve_s + plan.stage1_s
        last_plan_slot, dirty = slot, False
        late = {k: round(x, 1) for k, x in plan.shortfall_kwh.items() if x > 1.0}
        if late:
            emit("plan_shortfall", slot, vehicles=late)

    for slot in range(T_SLOTS):
        # ---- arrivals, departures, faults -----------------------------------
        for v in s.vehicles:
            ag = agents[v.vid]
            if slot == v.arrive_slot:
                ag.state = WAITING
                emit("arrival", slot, vid=v.vid, model=v.model.name, soc=round(ag.soc, 3),
                     reported_soh=round(v.soh_rep, 3), need_kwh=round(v.target_kwh, 1),
                     depart=slot_to_clock(v.depart_slot))
                dirty = True
                if ag.energy >= v.target_kwh:
                    ag.state = READY
                    emit("ready", slot, vid=v.vid, note="arrived with enough charge")
            if slot == v.depart_slot and ag.state not in (AWAY, GONE):
                ok = ag.state == READY
                outcomes[v.vid] = {"ok": ok, "final_soc": ag.soc, "energy": ag.energy,
                                   "state_at_departure": STATE_NAMES[ag.state],
                                   "shortfall_kwh": max(0.0, v.target_kwh - ag.energy)}
                emit("departure", slot, vid=v.vid, soc=round(ag.soc, 3), on_time=bool(ok),
                     state=STATE_NAMES[ag.state])
                release_bay(ag)
                ag.state = GONE
                dirty = True
            if v.vid in fault_until and slot >= fault_until[v.vid]:
                v.online = True
                del fault_until[v.vid]
                emit("charger_restored", slot, vid=v.vid)
                dirty = True
            if (fault_rate > 0 and ag.state == CHARGING and v.online
                    and v.depart_slot - slot > 12 and rng.random() < fault_rate):
                v.online = False
                fault_until[v.vid] = slot + 8
                emit("charger_fault", slot, vid=v.vid, until=slot_to_clock(slot + 8))
                dirty = True

        # ---- 1-minute loop --------------------------------------------------
        slot_load = slot_grid = slot_solar = 0.0
        slot_kw = np.zeros(n)
        for sub in range(SUBSTEPS):
            if policy in ("smart", "ideal") and (dirty and slot - last_plan_slot >= 0
                                                 and (sub == 0 or last_plan_slot != slot)
                                                 or slot - last_plan_slot >= replan_slots):
                do_replan(slot)
            # docking
            if policy == "ideal":
                for ag in agents.values():
                    if ag.state == WAITING:
                        dock(ag, slot, sub)
            else:
                for key in pool_n:
                    free = [b for b, o in enumerate(bay_owner[key]) if o is None]
                    if not free:
                        continue
                    for ag in queue_order(key):
                        if not free:
                            break
                        if policy == "smart":
                            w = plan.windows.get(ag.v.vid) if plan and plan.windows else None
                            if w is None or w[0] > slot:
                                continue
                        if dock(ag, slot, sub):
                            free.pop(0)
            # requests
            req: dict[str, float] = {}
            for v in s.vehicles:
                ag = agents[v.vid]
                if ag.state == MOVING_IN:
                    ag.timer -= 1
                    if ag.timer <= 0:
                        ag.state = CHARGING
                        emit("plugged_in", slot, sub, vid=v.vid, bay=ag.bay + 1)
                    req[v.vid] = 0.0
                elif ag.state == UNPLUGGING:
                    ag.timer -= 1
                    if ag.timer <= 0:
                        release_bay(ag)
                        ag.state = READY
                        emit("ready", slot, sub, vid=v.vid, soc=round(ag.soc, 3))
                        dirty = True
                    req[v.vid] = 0.0
                elif ag.state == CHARGING:
                    if ag.energy >= v.target_kwh - 1e-6:
                        ag.state = UNPLUGGING
                        ag.timer = 0.0 if ideal else ag.delay_minutes("unplug")
                        req[v.vid] = 0.0
                        if ag.timer <= 0:
                            release_bay(ag)
                            ag.state = READY
                            emit("ready", slot, sub, vid=v.vid, soc=round(ag.soc, 3))
                            dirty = True
                    elif policy in ("fcfs", "edf"):
                        req[v.vid] = ag.pmax
                    else:
                        planned = plan.setpoint(v.vid, slot) if plan else 0.0
                        req[v.vid] = min(planned + ag.plan_deficit_kw(plan, slot, sub), ag.pmax)
                else:
                    req[v.vid] = 0.0
            solar_now = float(s.solar_actual[slot])
            total_req = sum(min(r, agents[k].taper_limit()) for k, r in req.items())
            scale = min(1.0, (s.site.grid_limit_kw + solar_now) / total_req) if total_req > 0 else 1.0
            load = 0.0
            for i, vid in enumerate(order):
                ag = agents[vid]
                p = ag.step(req[vid], scale)
                slot_kw[i] += p / SUBSTEPS
                load += p
                if p > 0:
                    health_cost += _tier_cost(s, ag.v, p) * SUB_H
                    crate = s.site.charger_efficiency * p / ag.cap
                    crate_w += crate * p * SUB_H
                    kwh_w += p * SUB_H
                    peak_crate = max(peak_crate, crate)
                    if crate > s.health.c_rate_tiers[0][0]:
                        fast_kwh += p * SUB_H
                    if ag.temp_c > 45.0:
                        derate_min += 1
                hi = max(0.0, ag.energy - s.health.high_soc_threshold * ag.cap)
                if ag.state in (WAITING, MOVING_IN, CHARGING, UNPLUGGING, READY):
                    hold_cost += s.health.high_soc_cost_kwh_hour * hi * SUB_H
                    if hi > 0:
                        hi_dwell_h += SUB_H
            imp = max(0.0, load - solar_now)
            energy_cost += s.price[slot] * imp * SUB_H
            slot_load += load / SUBSTEPS
            slot_grid += imp / SUBSTEPS
            slot_solar += solar_now / SUBSTEPS
        grid_slot[slot], load_slot[slot], solar_slot[slot] = slot_grid, slot_load, slot_solar
        peak_so_far = max(peak_so_far, slot_grid)
        PW[:, slot] = slot_kw
        occupied = sum(1 for ag in agents.values() if ag.bay >= 0)
        bay_slots_used += occupied
        qpos_map: dict[str, int] = {}
        for key in pool_n:
            for r, ag in enumerate(queue_order(key)):
                qpos_map[ag.v.vid] = r + 1
        for i, vid in enumerate(order):
            ag = agents[vid]
            ST[i, slot] = ag.state
            if ag.state != AWAY:
                SOC[i, slot] = ag.soc
            BY[i, slot] = ag.bay
            QP[i, slot] = qpos_map.get(vid, 0)

    for v in s.vehicles:
        ag = agents[v.vid]
        if v.vid not in outcomes:
            outcomes[v.vid] = {"ok": ag.energy >= v.target_kwh, "final_soc": ag.soc,
                               "energy": ag.energy, "state_at_departure": STATE_NAMES[ag.state],
                               "shortfall_kwh": max(0.0, v.target_kwh - ag.energy)}

    demand_cost = s.site.demand_charge_per_kw_day * float(grid_slot.max())
    delivered = float(load_slot.sum() * DT_H)
    solar_total = float(solar_slot.sum() * DT_H)
    solar_used = float(np.minimum(load_slot, solar_slot).sum() * DT_H)
    total_bays = sum(pool_n.values()) if not ideal else 0
    veh_df = pd.DataFrame([{
        "vid": v.vid, "model": v.model.name, "arrive": slot_to_clock(v.arrive_slot),
        "depart": slot_to_clock(v.depart_slot), "route_km": round(v.route_km),
        "payload_kg": round(v.payload_kg), "soh": round(v.soh_true, 3),
        "need_kwh": round(v.target_kwh, 1), "final_kwh": round(outcomes[v.vid]["energy"], 1),
        "wait_h": round(waits.get(v.vid, 0.0), 2), "ready_on_time": bool(outcomes[v.vid]["ok"]),
        "shortfall_kwh": round(outcomes[v.vid]["shortfall_kwh"], 1),
        "overcharge_kwh": round(max(0.0, outcomes[v.vid]["energy"] - v.target_kwh), 1),
    } for v in s.vehicles])
    slot_df = pd.DataFrame({"slot": np.arange(T_SLOTS), "clock": [slot_to_clock(i) for i in range(T_SLOTS)],
                            "price": s.price, "solar_kw": solar_slot, "load_kw": load_slot,
                            "grid_kw": grid_slot})
    wl = list(waits.values()) or [0.0]
    metrics = {
        "policy": policy, "energy_cost": energy_cost, "demand_cost": demand_cost,
        "health_cost": health_cost + hold_cost,
        "total_cost": energy_cost + demand_cost + health_cost + hold_cost,
        "peak_kw": float(grid_slot.max()), "missed": int((~veh_df["ready_on_time"]).sum()),
        "on_time_rate": float(veh_df["ready_on_time"].mean()),
        "shortfall_kwh": float(veh_df["shortfall_kwh"].sum()),
        "overcharge_kwh": float(veh_df["overcharge_kwh"].sum()),
        "mean_wait_h": float(np.mean(wl)), "max_wait_h": float(np.max(wl)),
        "bay_utilisation": (bay_slots_used / (total_bays * T_SLOTS)) if total_bays else 0.0,
        "solar_self_consumption": solar_used / solar_total if solar_total else 0.0,
        "energy_delivered_kwh": delivered,
        "avg_cost_per_kwh": (energy_cost + demand_cost) / delivered if delivered else 0.0,
        "plans": plan_runs, "plan_time_s": plan_time,
        "avg_c_rate": crate_w / kwh_w if kwh_w else 0.0, "peak_c_rate": peak_crate,
        "fast_charge_share": fast_kwh / kwh_w if kwh_w else 0.0,
        "hi_soc_dwell_h": hi_dwell_h, "thermal_derate_min": derate_min,
    }
    return SimResult(policy, metrics, veh_df, slot_df, ST, SOC, PW, BY, QP, bus.events,
                     plan_runs, plan_time, order)


def run_job(job: tuple) -> tuple[str, SimResult]:
    """Picklable worker entry point so the policies can run in separate processes."""
    scn, policy, fault_rate, seed = job
    return policy, run(scn, policy, fault_rate=fault_rate, seed=seed)


def compare(scn: Scenario, policies=POLICIES, **kw) -> dict[str, SimResult]:
    return {p: run(scn, p, **kw) for p in policies}
