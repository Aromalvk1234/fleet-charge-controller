"""Bay scheduler: decides which truck takes which pool bay and when, then hands
the LP the window each truck holds so it can pick the cheapest, gentlest power.

Stage 1 (per model pool, time-indexed MILP). Each truck j needing charge is a job
with release r_j (arrival), deadline D_j (departure) and duration

    p_j = 1 (plug-in) + k_j (fast-charge slots at the truck's own SOH-derated
          power incl. taper) + 1 (unplug and move to the gate)

Binary x[j,t] = job j starts at slot t. Pool capacity: at most n bays occupied in
every slot (trucks already in a bay are fixed jobs). Cost of starting at t:

    wait weight * priority * (t - r_j)                              (queueing)
  + energy proxy: price over the charging slots * fast-charge kWh   (cheap windows)
  + shortfall penalty * priority * energy missing if t + p_j > D_j  (lateness)

Stage 1b widens each window into any bay-slots left unused (both directions), so
the LP has room to slow down and shift charging into cheap or solar hours.
Stage 2 (LP, planner.py) optimises power inside the windows.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .planner import PLAN_MARGIN_SOC, Plan, plan_charging
from .scenario import DT_H, T_SLOTS, Scenario, Vehicle

WAIT_COST_PER_SLOT = 0.5      # $ per slot of queueing per unit priority
OVERHEAD_SLOTS = 2            # plug-in slot + unplug slot (no charging in either)


def target_energy(v: Vehicle) -> float:
    return min(v.target_kwh + PLAN_MARGIN_SOC * v.cap_plan_kwh, v.cap_plan_kwh)


def fast_slots(s: Scenario, v: Vehicle, e0: float) -> int:
    """Slots needed to reach the target charging flat out (SOH-derated, with taper)."""
    h, eta = s.health, s.site.charger_efficiency
    cap, pmax = v.cap_plan_kwh, v.max_kw_plan
    target = target_energy(v)
    c = (1 - h.taper_floor) / (1 - h.taper_start_soc)
    e, k = e0, 0
    while e < target - 1e-6 and k < T_SLOTS:
        soc = e / cap
        p = pmax if soc <= h.taper_start_soc else pmax * max(h.taper_floor, h.taper_floor + c * (1 - soc))
        e += eta * p * DT_H
        k += 1
    return k


def _energy_at(v: Vehicle, energy_kwh: dict[str, float], now: int) -> float:
    if v.arrive_slot <= now and v.vid in energy_kwh:
        return energy_kwh[v.vid]
    return v.soc_arrive_rep * v.cap_plan_kwh


@dataclass
class Job:
    v: Vehicle
    release: int
    deadline: int
    k: int                 # fast-charge slots
    dur: int               # total bay slots
    kwh: float             # energy to deliver (grid side)
    p_kw: float
    start: int | None = None


def assign_windows(s: Scenario, now: int, energy_kwh: dict[str, float],
                   in_bay: set[str], exclude: set[str],
                   time_limit: float = 5.0) -> tuple[dict[str, tuple[int, int]], float]:
    """Stage 1 + 1b. Returns ({vid: (start, end)} bay-occupancy windows, solve seconds)."""
    t0 = time.perf_counter()
    windows: dict[str, tuple[int, int]] = {}
    eta = s.site.charger_efficiency
    for key, cap_bays in s.yard.bays:
        fixed = np.zeros(T_SLOTS, int)          # occupancy by trucks already in a bay
        jobs: list[Job] = []
        for v in s.vehicles:
            if v.model_key != key or v.vid in exclude or v.ready_by <= now:
                continue
            e0 = _energy_at(v, energy_kwh, now)
            target = target_energy(v)
            if v.vid in in_bay:
                k = fast_slots(s, v, e0)
                end = min(v.ready_by, now + k + 1)       # remaining charge + unplug
                fixed[now:end] += 1
                windows[v.vid] = (now, end)
                continue
            if e0 >= target - 1e-6:
                continue                                    # already full: needs no bay
            k = fast_slots(s, v, e0)
            jobs.append(Job(v, max(v.arrive_slot, now), v.ready_by, k, k + OVERHEAD_SLOTS,
                            (target - e0) / eta, v.max_kw_plan))
        _solve_pool(s, cap_bays, jobs, fixed, time_limit)
        placed = [j for j in jobs if j.start is not None]
        for j in placed:
            windows[j.v.vid] = (j.start, min(j.start + j.dur, j.deadline))
        _widen(windows, placed, in_bay, s, key, cap_bays, fixed, now)
    return windows, time.perf_counter() - t0


def _solve_pool(s: Scenario, cap_bays: int, jobs: list[Job], fixed: np.ndarray,
                time_limit: float) -> None:
    # jobs with no feasible start slot cannot be served: drop them (they stay unplaced)
    jobs_in = [j for j in jobs if max(j.release, j.deadline - OVERHEAD_SLOTS - 1) >= j.release
               and j.deadline - OVERHEAD_SLOTS - 1 >= j.release]
    if not jobs_in:
        return
    var_job: list[tuple[int, int]] = []     # (job index, start slot)
    cost: list[float] = []
    for ji, j in enumerate(jobs_in):
        last = j.deadline - OVERHEAD_SLOTS - 1                   # latest start with >=1 charge slot
        for t in range(j.release, min(last, T_SLOTS - 1) + 1):
            late_slots = max(0, t + j.dur - j.deadline)
            short_kwh = min(late_slots, j.k) * j.p_kw * DT_H * s.site.charger_efficiency
            chg = slice(t + 1, min(t + 1 + j.k, T_SLOTS))
            fast_kwh = max(j.k * j.p_kw * DT_H, 1e-9)
            energy_proxy = float(np.sum(s.price[chg])) * j.p_kw * DT_H * (j.kwh / fast_kwh)
            cost.append(WAIT_COST_PER_SLOT * j.v.priority * (t - j.release) + energy_proxy
                        + s.policy.shortfall_penalty * j.v.priority * short_kwh)
            var_job.append((ji, t))
    n = len(var_job)
    rows, cols, vals, lo, hi = [], [], [], [], []
    r = 0
    for ji in range(len(jobs_in)):                    # each job starts exactly once
        for vi, (jj, _) in enumerate(var_job):
            if jj == ji:
                rows.append(r); cols.append(vi); vals.append(1.0)
        lo.append(1.0); hi.append(1.0); r += 1
    by_slot: dict[int, list[int]] = {}
    for vi, (ji, t) in enumerate(var_job):
        j = jobs_in[ji]
        for tau in range(t, min(t + j.dur, j.deadline, T_SLOTS)):
            by_slot.setdefault(tau, []).append(vi)
    for tau, vis in by_slot.items():
        for vi in vis:
            rows.append(r); cols.append(vi); vals.append(1.0)
        lo.append(-np.inf); hi.append(float(cap_bays - fixed[tau])); r += 1
    A = coo_matrix((vals, (rows, cols)), shape=(r, n)).tocsr()
    res = milp(c=np.array(cost), constraints=LinearConstraint(A, np.array(lo), np.array(hi)),
               integrality=np.ones(n), bounds=Bounds(0, 1),
               options={"time_limit": time_limit, "mip_rel_gap": 0.01, "disp": False})
    if res.x is None:      # infeasible under hard capacity (or timeout): greedy EDF fallback
        occ = fixed.copy()
        for j in sorted(jobs_in, key=lambda j: j.deadline):
            for t in range(j.release, T_SLOTS - j.dur + 1):
                if all(occ[tau] < cap_bays for tau in range(t, min(t + j.dur, T_SLOTS))):
                    j.start = t
                    occ[t:t + j.dur] += 1
                    break
        return
    for vi, (ji, t) in enumerate(var_job):
        if res.x[vi] > 0.5:
            jobs_in[ji].start = t


def _widen(windows: dict[str, tuple[int, int]], placed: list[Job], in_bay: set[str],
           s: Scenario, key: str, cap_bays: int, fixed: np.ndarray, now: int) -> None:
    """Stage 1b: grow windows into unused bay-slots (EDF order) to give the LP slack."""
    occ = fixed.copy()
    for j in placed:
        lo, hi = windows[j.v.vid]
        occ[lo:hi] += 1
    members = [j.v.vid for j in placed] + [vid for vid in in_bay if vid in windows
                                           and s.vehicle(vid).model_key == key]
    for vid in sorted(members, key=lambda vid: s.vehicle(vid).ready_by):
        v = s.vehicle(vid)
        lo, hi = windows[vid]
        floor = now if vid in in_bay else max(v.arrive_slot, now)
        grew = True
        while grew:
            grew = False
            if hi < v.ready_by and occ[hi] < cap_bays:
                occ[hi] += 1
                hi += 1
                grew = True
            if vid not in in_bay and lo > floor and occ[lo - 1] < cap_bays:
                lo -= 1
                occ[lo] += 1
                grew = True
        windows[vid] = (lo, hi)


def plan_yard(s: Scenario, now: int, energy_kwh: dict[str, float], in_bay: set[str],
              exclude: set[str], peak_so_far: float = 0.0,
              solar: np.ndarray | None = None, time_limit: float = 5.0) -> Plan:
    """Full scheduler: bay windows, then the LP over charge power inside them."""
    windows, t1 = assign_windows(s, now, energy_kwh, in_bay, exclude, time_limit)
    lp_windows: dict[str, tuple[int, int]] = {}
    for vid, (lo, hi) in windows.items():
        # first window slot is the plug-in slot (not for trucks already docked); last is unplug
        lp_windows[vid] = (lo if vid in in_bay else lo + 1, hi - 1)
    plan = plan_charging(s, now, energy_kwh, peak_so_far, solar, windows=lp_windows, exclude=exclude)
    plan.windows = windows
    plan.stage1_s = t1
    return plan
