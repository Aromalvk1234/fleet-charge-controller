"""Synthetic depot-day generator: prices, solar, and a 50-truck fleet with
heterogeneous arrival/departure times and trip requirements."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

from .energy import trip_energy
from .specs import (FLEET_MIX, TRUCK_MODELS, DispatchPolicy, HealthParams, Site,
                    Telemetry, TruckModel, YardConfig)

DT_H = 0.25                 # slot length in hours
T_SLOTS = 96                # 24 h horizon
DISPATCH_BUFFER_SLOTS = 1   # truck must be ready this many slots before departure
MAX_PLANNED_TARGET = 0.95   # dispatch cap on required departure SOC
START_HOUR = 12.0           # horizon starts 12:00 on day 1, ends 12:00 on day 2


def slot_to_clock(slot: float, minute: float = 0.0) -> str:
    total = int(round(((START_HOUR + slot * DT_H) % 24) * 60 + minute)) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def clock_to_slot(hhmm: str, next_day_ok: bool = True) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    hours = h + m / 60
    rel = hours - START_HOUR
    if rel < 0:
        rel += 24
    return int(round(rel / DT_H))


def required_energy(model_key: str, route_km: float, payload_kg: float, elev_m_km: float,
                    temp_c: float, cap_plan_kwh: float, policy: DispatchPolicy) -> dict[str, float]:
    """Energy the truck must leave with: physics trip energy, plus margin, plus reserve."""
    trip = trip_energy(model_key, route_km, payload_kg, elev_m_km, temp_c)
    margin = trip["total"] * policy.energy_margin
    reserve = policy.reserve_soc * cap_plan_kwh
    return {**trip, "margin": margin, "reserve": reserve,
            "required": trip["total"] + margin + reserve}


@dataclass
class Vehicle:
    vid: str
    model_key: str
    arrive_slot: int
    depart_slot: int
    soc_arrive: float            # true SOC on arrival (fraction of true usable capacity)
    route_km: float              # round trip
    payload_kg: float = 0.0
    elev_m_km: float = 3.0       # cumulative ascent per km
    soh_true: float = 1.0
    soh_rep: float = 1.0         # what the truck reports to the controller
    soc_arrive_rep: float = 0.0
    priority: float = 1.0
    target_kwh: float = 0.0      # energy required at departure (set via refresh_target)
    online: bool = True          # charger availability (fault injection)

    @property
    def model(self) -> TruckModel:
        return TRUCK_MODELS[self.model_key]

    @property
    def ready_by(self) -> int:
        return self.depart_slot - DISPATCH_BUFFER_SLOTS

    @property
    def cap_true_kwh(self) -> float:
        return self.model.battery_kwh * self.soh_true

    @property
    def cap_plan_kwh(self) -> float:
        return self.model.battery_kwh * self.soh_rep

    @property
    def max_kw_true(self) -> float:
        return self.model.max_charge_kw * (1 - TELEMETRY.soh_power_derate * (1 - self.soh_true))

    @property
    def max_kw_plan(self) -> float:
        return self.model.max_charge_kw * (1 - TELEMETRY.soh_power_derate * (1 - self.soh_rep))

    @property
    def target(self) -> float:
        """Required departure SOC as the controller sees it (fraction of reported usable)."""
        return self.target_kwh / self.cap_plan_kwh

    def requirement(self, temp_c: float, policy: DispatchPolicy) -> dict[str, float]:
        return required_energy(self.model_key, self.route_km, self.payload_kg, self.elev_m_km,
                               temp_c, self.cap_plan_kwh, policy)

    def refresh_target(self, temp_c: float, policy: DispatchPolicy) -> None:
        self.target_kwh = self.requirement(temp_c, policy)["required"]


TELEMETRY = Telemetry()


@dataclass
class Scenario:
    seed: int
    price: np.ndarray            # $/kWh per slot (day-ahead, treated as known)
    solar_fc: np.ndarray         # kW forecast
    solar_actual: np.ndarray     # kW realised
    vehicles: list[Vehicle]
    temp_c: float = 15.0
    site: Site = field(default_factory=Site)
    health: HealthParams = field(default_factory=HealthParams)
    policy: DispatchPolicy = field(default_factory=DispatchPolicy)
    yard: YardConfig = field(default_factory=YardConfig)

    def vehicle(self, vid: str) -> Vehicle:
        return next(v for v in self.vehicles if v.vid == vid)

    def apply_temperature(self, temp_c: float) -> None:
        self.temp_c = temp_c
        for v in self.vehicles:
            v.refresh_target(temp_c, self.policy)


def make_price(rng: np.random.Generator, spike: bool = True) -> np.ndarray:
    hours = (START_HOUR + np.arange(T_SLOTS) * DT_H) % 24
    base = np.select(
        [hours < 6, hours < 16, hours < 21],
        [0.09, 0.14, 0.32],
        default=0.14,
    )
    hourly_noise = np.exp(rng.normal(0, 0.12, T_SLOTS // 4 + 1))
    noise = np.repeat(hourly_noise, 4)[:T_SLOTS]
    price = base * noise
    if spike:  # a real-time price spike in the evening peak
        s = int(rng.integers(18, 34))
        price[s:s + 4] *= 1.8
    return price


def make_solar(rng: np.random.Generator, site: Site):
    hours = (START_HOUR + np.arange(T_SLOTS) * DT_H) % 24
    clear = np.clip(np.sin(np.pi * (hours - 6.5) / 13.0), 0, None) ** 1.2
    clear *= site.solar_peak_kw
    day1, day2 = rng.uniform(0.55, 1.0, 2)
    cloud = np.where(np.arange(T_SLOTS) * DT_H + START_HOUR < 24, day1, day2)
    fc = clear * cloud
    ar = np.zeros(T_SLOTS)
    for i in range(1, T_SLOTS):
        ar[i] = 0.9 * ar[i - 1] + rng.normal(0, 0.08)
    actual = np.clip(fc * (1 + ar), 0, clear * 1.1)
    return fc, actual


def make_fleet(rng: np.random.Generator, policy: DispatchPolicy,
               temp_c: float = 15.0) -> list[Vehicle]:
    vehicles: list[Vehicle] = []
    n = 0
    for key, count in FLEET_MIX.items():
        m = TRUCK_MODELS[key]
        for _ in range(count):
            n += 1
            short_turn = key == "e_transit" and rng.random() < 0.18
            if short_turn:
                a = int(rng.integers(0, 8))                    # 12:00-14:00
                d = a + int(rng.integers(16, 26))              # 4-6.5 h dwell
                route = float(rng.uniform(40, 80))
            else:
                a = int(rng.triangular(2, 22, 42))             # 12:30-22:30, peak ~17:30
                d = int(rng.integers(66, 96))                  # 04:30-12:00 next day
                route = float(rng.uniform(*m.route_km))
            d = max(d, a + 20)
            d = min(d, T_SLOTS - 1)
            soh = float(rng.uniform(*TELEMETRY.soh_range))
            soc = float(rng.uniform(0.15, 0.45))
            v = Vehicle(
                vid=f"T{n:02d}", model_key=key, arrive_slot=a, depart_slot=d,
                soc_arrive=soc, route_km=route,
                payload_kg=float(rng.uniform(0.15, 1.0) * m.max_payload_kg),
                elev_m_km=float(rng.uniform(*m.elevation_m_km)),
                soh_true=soh,
                soh_rep=float(np.clip(soh + rng.normal(0, TELEMETRY.soh_noise), 0.5, 1.0)),
                soc_arrive_rep=float(np.clip(soc + rng.normal(0, TELEMETRY.soc_noise), 0.0, 1.0)),
                priority=float(rng.choice([1.0, 2.0], p=[0.85, 0.15])),
            )
            v.refresh_target(temp_c, policy)
            while v.target > MAX_PLANNED_TARGET:   # dispatch would not assign a trip needing ~100% SOC
                v.route_km *= 0.96
                v.refresh_target(temp_c, policy)
            vehicles.append(v)
    return vehicles


def make_scenario(seed: int = 0, temp_c: float = 15.0,
                  site: Site | None = None, health: HealthParams | None = None,
                  policy: DispatchPolicy | None = None,
                  yard: YardConfig | None = None) -> Scenario:
    rng = np.random.default_rng(seed)
    site = site or Site()
    health = health or HealthParams()
    policy = policy or DispatchPolicy()
    price = make_price(rng)
    fc, actual = make_solar(rng, site)
    vehicles = make_fleet(rng, policy, temp_c)
    return Scenario(seed=seed, price=price, solar_fc=fc, solar_actual=actual,
                    vehicles=vehicles, temp_c=temp_c, site=site, health=health,
                    policy=policy, yard=yard or YardConfig())


def clone_scenario(s: Scenario) -> Scenario:
    return replace(s, vehicles=[replace(v) for v in s.vehicles])
