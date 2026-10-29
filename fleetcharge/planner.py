"""Charge-power planner (LP).

Chooses grid-side charge power p[v,t] for every truck so that each reaches its
trip-derived energy target before departure at minimum cost:

    energy (dynamic price, net of solar) + demand charge (daily peak kW)
    + battery-health proxy (convex C-rate tiers, high-SOC dwell)
    + heavy penalty on any missed target.

``windows`` optionally restricts each truck to a charging window [lo, hi) of
slots; this is how the bay scheduler (`scheduler.py`) hands the LP the time
each truck physically holds a bay. Without windows every truck has its own
charger (the unconstrained bound).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix

from .scenario import DT_H, T_SLOTS, Scenario

PLAN_MARGIN_SOC = 0.02    # cushion (fraction of usable capacity) vs taper/solar drift


@dataclass
class Plan:
    now: int
    status: str
    power: dict[str, np.ndarray]        # vid -> kW (grid side) from slot `start[vid]`
    start: dict[str, int]
    grid_kw: np.ndarray
    shortfall_kwh: dict[str, float]
    cost: dict[str, float]
    peak_kw: float
    solve_s: float = 0.0
    windows: dict[str, tuple[int, int]] | None = None   # bay occupancy [start, end)
    stage1_s: float = 0.0
    fallback: bool = False

    def setpoint(self, vid: str, slot: int) -> float:
        p = self.power.get(vid)
        if p is None:
            return 0.0
        i = slot - self.start[vid]
        return float(p[i]) if 0 <= i < len(p) else 0.0


class _Builder:
    def __init__(self) -> None:
        self.n = 0
        self.lb: list[float] = []
        self.ub: list[float] = []
        self.c: list[float] = []

    def var(self, lb: float, ub: float, cost: float) -> int:
        self.lb.append(lb)
        self.ub.append(ub)
        self.c.append(cost)
        self.n += 1
        return self.n - 1


class _Rows:
    def __init__(self) -> None:
        self.r: list[int] = []
        self.c: list[int] = []
        self.v: list[float] = []
        self.rhs: list[float] = []

    def add(self, terms: list[tuple[int, float]], rhs: float) -> None:
        row = len(self.rhs)
        for col, val in terms:
            self.r.append(row)
            self.c.append(col)
            self.v.append(val)
        self.rhs.append(rhs)

    def matrix(self, ncols: int):
        if not self.rhs:
            return None, None
        m = coo_matrix((self.v, (self.r, self.c)), shape=(len(self.rhs), ncols)).tocsr()
        return m, np.array(self.rhs)


def tier_widths(pmax: float, cap: float, health) -> list[float]:
    widths, prev = [], 0.0
    for i, (ub_c, _) in enumerate(health.c_rate_tiers):
        top = ub_c * cap
        if i == len(health.c_rate_tiers) - 1:
            top = max(top, pmax)
        top = min(top, pmax)
        widths.append(max(0.0, top - prev))
        prev = max(prev, top)
    return widths


def plan_charging(s: Scenario, now: int = 0,
                  energy_kwh: dict[str, float] | None = None,
                  peak_so_far: float = 0.0,
                  solar: np.ndarray | None = None,
                  windows: dict[str, tuple[int, int]] | None = None,
                  exclude: set[str] | None = None) -> Plan:
    """Solve from slot `now`. `energy_kwh` is the *reported* battery energy of trucks
    already on site; others start at their reported arrival SOC."""
    t0 = time.perf_counter()
    energy_kwh = energy_kwh or {}
    exclude = exclude or set()
    solar = s.solar_fc if solar is None else solar
    site, health = s.site, s.health
    eta, tiers = site.charger_efficiency, health.c_rate_tiers
    b, A_ub, A_eq = _Builder(), _Rows(), _Rows()

    horizon = range(now, T_SLOTS)
    g = {t: b.var(0.0, site.grid_limit_kw, s.price[t] * DT_H) for t in horizon}
    ppk = b.var(peak_so_far, site.grid_limit_kw, site.demand_charge_per_kw_day)
    site_terms: dict[int, list[tuple[int, float]]] = {t: [(g[t], -1.0)] for t in horizon}

    layout: dict[str, tuple[int, list[list[int]], int, int]] = {}

    for v in s.vehicles:
        if v.vid in exclude:
            continue
        a, d = max(v.arrive_slot, now), v.ready_by
        n = d - a
        if n <= 0:
            continue
        cap, pmax = v.cap_plan_kwh, v.max_kw_plan
        e0 = energy_kwh.get(v.vid, v.soc_arrive_rep * cap) if v.arrive_slot <= now \
            else v.soc_arrive_rep * cap
        target_e = min(v.target_kwh + PLAN_MARGIN_SOC * cap, cap)
        ceiling = max(health.soc_ceiling * cap, target_e)
        widths = tier_widths(pmax, cap, health)
        if not v.online:
            widths = [0.0] * len(widths)
        lo, hi = (windows[v.vid] if windows and v.vid in windows else
                  ((0, 0) if windows is not None else (a, d)))

        e_idx = [b.var(e0, e0, 0.0)] + [b.var(0.0, ceiling, 0.0) for _ in range(n)]
        pt = [[b.var(0.0, widths[k] if lo <= a + i < hi else 0.0, tiers[k][1] * DT_H)
               for i in range(n)] for k in range(len(tiers))]
        h_idx = [b.var(0.0, np.inf, health.high_soc_cost_kwh_hour * DT_H) for _ in range(n)]
        sv = b.var(0.0, np.inf, s.policy.shortfall_penalty * v.priority)
        layout[v.vid] = (a, pt, n, sv)

        c_slope = (1 - health.taper_floor) / (1 - health.taper_start_soc)
        for i in range(n):
            ptot = [(pt[k][i], 1.0) for k in range(len(tiers))]
            A_eq.add([(e_idx[i + 1], 1.0), (e_idx[i], -1.0)] +
                     [(var, -eta * DT_H) for var, _ in ptot], 0.0)
            A_ub.add(ptot + [(e_idx[i], pmax * c_slope / cap)],
                     pmax * (health.taper_floor + c_slope))
            A_ub.add([(e_idx[i + 1], 1.0), (h_idx[i], -1.0)], health.high_soc_threshold * cap)
            site_terms[a + i].extend(ptot)
        A_ub.add([(e_idx[n], -1.0), (sv, -1.0)], -target_e)

    for t in horizon:
        A_ub.add(site_terms[t], float(solar[t]))
        A_ub.add([(g[t], 1.0), (ppk, -1.0)], 0.0)

    n_vars = b.n
    Aub, bub = A_ub.matrix(n_vars)
    Aeq, beq = A_eq.matrix(n_vars)
    res = linprog(np.array(b.c), A_ub=Aub, b_ub=bub, A_eq=Aeq, b_eq=beq,
                  bounds=list(zip(b.lb, b.ub)), method="highs")
    if res.status != 0:
        return Plan(now, f"failed:{res.message}", {}, {}, np.zeros(T_SLOTS), {}, {}, 0.0,
                    time.perf_counter() - t0)

    x = res.x
    power, start, short = {}, {}, {}
    for vid, (a, pt, n, sv) in layout.items():
        power[vid] = sum(x[np.array(pt[k])] for k in range(len(tiers)))
        start[vid] = a
        short[vid] = float(x[sv])
    grid = np.zeros(T_SLOTS)
    for t in horizon:
        grid[t] = x[g[t]]
    energy_cost = float(sum(s.price[t] * grid[t] * DT_H for t in horizon))
    cost = {"energy": energy_cost, "demand": float(site.demand_charge_per_kw_day * x[ppk]),
            "objective": float(res.fun)}
    return Plan(now, "optimal", power, start, grid, short, cost, float(x[ppk]),
                time.perf_counter() - t0, windows=windows)
