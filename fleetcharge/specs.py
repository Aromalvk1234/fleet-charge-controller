"""Truck, charger, yard and site specifications.

Battery / range / charging figures come from public manufacturer and press
specs (checked Oct 2026). Mass, drag and drivetrain figures are engineering
estimates used by the physics energy model (`energy.py`); the model is then
calibrated so that, at reference conditions, it reproduces the consumption
derived from rated range (usable_kwh / rated_range_km). All of it is input to
a *simulation*; adjust in one place here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TruckModel:
    name: str
    segment: str
    battery_kwh: float            # usable when new (SOH = 1)
    consumption_kwh_km: float     # rated-range-derived, used for calibration
    vehicle_max_kw: float         # max accepted charge power
    depot_charger_kw: float       # depot charger rating for this pool
    route_km: tuple[float, float]
    # --- physics for the trip-energy model ---
    curb_kg: float                # tractor+trailer (or van) empty mass
    max_payload_kg: float
    crr: float                    # rolling resistance coefficient
    cda_m2: float                 # drag area
    eta_drive: float              # battery-to-wheel efficiency
    regen: float                  # fraction of braking/descent energy recovered
    aux_kw: float                 # base auxiliary load (compressor, lights, ECU)
    hvac_kw_per_c: float          # extra heating power per degree below 18 C
    avg_speed_kmh: float
    stops_per_km: float
    stop_speed_kmh: float
    elevation_m_km: tuple[float, float]  # typical cumulative ascent per km
    # --- yard operations (minutes) ---
    plug_min: tuple[float, float]   # waiting lot -> bay, positioning + connect
    unplug_min: tuple[float, float]  # disconnect + move to dispatch gate

    @property
    def max_charge_kw(self) -> float:
        return min(self.vehicle_max_kw, self.depot_charger_kw)


TRUCK_MODELS: dict[str, TruckModel] = {
    "e_transit": TruckModel(
        # 2025 E-Transit: 89 kWh pack, EPA 143-159 mi (230-256 km),
        # 19.2 kW AC (80 A), DC up to 176 kW.
        name="Ford E-Transit", segment="van", battery_kwh=89.0,
        consumption_kwh_km=0.45, vehicle_max_kw=176.0, depot_charger_kw=19.2,
        route_km=(60.0, 140.0),
        curb_kg=2800, max_payload_kg=1700, crr=0.012, cda_m2=1.7, eta_drive=0.78,
        regen=0.5, aux_kw=0.6, hvac_kw_per_c=0.25, avg_speed_kmh=55,
        stops_per_km=0.6, stop_speed_kmh=40, elevation_m_km=(1.0, 8.0),
        plug_min=(4.0, 8.0), unplug_min=(3.0, 6.0),
    ),
    "vnr_electric": TruckModel(
        # 6-battery config: 565 kWh installed, ~452 kWh usable, up to 275 mi
        # (443 km), CCS1 at 250 kW.
        name="Volvo VNR Electric", segment="class8_regional", battery_kwh=452.0,
        consumption_kwh_km=1.02, vehicle_max_kw=250.0, depot_charger_kw=150.0,
        route_km=(150.0, 320.0),
        curb_kg=15500, max_payload_kg=20000, crr=0.0065, cda_m2=5.6, eta_drive=0.82,
        regen=0.5, aux_kw=3.0, hvac_kw_per_c=0.5, avg_speed_kmh=60,
        stops_per_km=0.05, stop_speed_kmh=60, elevation_m_km=(1.0, 6.0),
        plug_min=(8.0, 14.0), unplug_min=(6.0, 10.0),
    ),
    "eactros_600": TruckModel(
        # 621 kWh installed LFP, >95 % usable (~590 kWh), 500 km rated range,
        # CCS2 up to 400 kW (MCS-ready).
        name="Mercedes-Benz eActros 600", segment="class8_long", battery_kwh=590.0,
        consumption_kwh_km=1.18, vehicle_max_kw=400.0, depot_charger_kw=180.0,
        route_km=(200.0, 380.0),
        curb_kg=16000, max_payload_kg=24000, crr=0.0062, cda_m2=5.0, eta_drive=0.84,
        regen=0.5, aux_kw=3.0, hvac_kw_per_c=0.5, avg_speed_kmh=70,
        stops_per_km=0.03, stop_speed_kmh=70, elevation_m_km=(1.0, 6.0),
        plug_min=(8.0, 14.0), unplug_min=(6.0, 10.0),
    ),
}

FLEET_MIX = {"e_transit": 28, "vnr_electric": 14, "eactros_600": 8}  # 50 trucks
BAYS = {"e_transit": 6, "vnr_electric": 3, "eactros_600": 2}          # 11 bays, per-model pools


@dataclass(frozen=True)
class Site:
    grid_limit_kw: float = 1500.0
    solar_peak_kw: float = 900.0
    charger_efficiency: float = 0.93      # grid kWh -> battery kWh
    demand_charge_per_kw_day: float = 0.55  # $/kW of daily peak (prorated monthly tariff)
    export_credit: float = 0.0            # surplus solar is simply curtailed


@dataclass(frozen=True)
class HealthParams:
    """Battery-health proxy. All costs are $ and piecewise-linear convex so
    the planner stays tractable.

    C-rate tiers: (upper_bound_in_C, $ per kWh charged at that tier).
    High-SOC dwell: $ per kWh-above-threshold per hour.
    """
    c_rate_tiers: tuple[tuple[float, float], ...] = (
        (0.15, 0.002), (0.35, 0.010), (1.00, 0.035),
    )
    high_soc_threshold: float = 0.80
    high_soc_cost_kwh_hour: float = 0.004
    soc_ceiling: float = 1.0
    taper_start_soc: float = 0.80
    taper_floor: float = 0.30  # fraction of max power at 100% SOC


@dataclass(frozen=True)
class DispatchPolicy:
    energy_margin: float = 0.15   # extra energy on top of the trip estimate
    reserve_soc: float = 0.10     # never arrive home below this SOC
    shortfall_penalty: float = 50.0  # $ per kWh missing at departure (per priority)


@dataclass(frozen=True)
class Telemetry:
    """What the trucks report back (simulated): noisy SOC every slot, SOH once."""
    soc_noise: float = 0.010      # 1-sigma absolute SOC error
    soh_noise: float = 0.010      # 1-sigma absolute SOH error
    soh_range: tuple[float, float] = (0.82, 1.00)  # fleet ageing spread (true SOH)
    soh_power_derate: float = 0.5  # max charge power x (1 - derate*(1-SOH))


@dataclass(frozen=True)
class YardConfig:
    bays: tuple[tuple[str, int], ...] = tuple(BAYS.items())
    bay_cost_per_slot: float = 0.35   # $ opportunity cost of holding a bay for 15 min

    def bays_for(self, key: str) -> int:
        return dict(self.bays)[key]
