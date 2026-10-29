"""Physics-based trip energy model.

Energy at the battery for a round trip is built from first principles and then
scaled by a single per-model calibration constant so that, at reference
conditions (50 % payload, 3 m/km ascent, 15 C), it reproduces the consumption
derived from the published rated range. Sensitivity to distance, cargo mass,
terrain and temperature therefore comes from the physics, while the absolute
level matches the real vehicle.

    E_prop  = (Crr m g + 1/2 rho CdA v^2) d / eta
    E_stop  = n_stops * 1/2 m v_s^2 * (1/eta - eta r)
    E_climb = m g h_up * (1/eta - eta r)
    E_aux   = (P_aux + P_hvac(T)) * d / v_avg
    E_trip  = cal * f_cold(T) * (E_prop + E_stop + E_climb) + E_aux
"""
from __future__ import annotations

from functools import lru_cache

from .specs import TRUCK_MODELS, TruckModel

G = 9.81
RHO_AIR = 1.2
J_PER_KWH = 3.6e6
REF = {"payload_frac": 0.5, "elev_m_km": 3.0, "temp_c": 15.0, "dist_km": 100.0}


def _hvac_kw(m: TruckModel, temp_c: float) -> float:
    heat = m.hvac_kw_per_c * max(0.0, 18.0 - temp_c)
    cool = 0.6 * m.hvac_kw_per_c * max(0.0, temp_c - 24.0)
    return heat + cool


def _terms(m: TruckModel, dist_km: float, payload_kg: float, elev_m_km: float,
           temp_c: float) -> dict[str, float]:
    mass = m.curb_kg + payload_kg
    v = m.avg_speed_kmh / 3.6
    d_m = dist_km * 1000.0
    roll = m.crr * mass * G * d_m
    aero = 0.5 * RHO_AIR * m.cda_m2 * (1.05 * v) ** 2 * d_m
    net = 1.0 / m.eta_drive - m.eta_drive * m.regen
    stop = 0.5 * mass * (m.stop_speed_kmh / 3.6) ** 2 * net * m.stops_per_km * dist_km
    climb = mass * G * elev_m_km * dist_km * net
    cold = 1.0 + 0.004 * max(0.0, 15.0 - temp_c)
    aux = (m.aux_kw + _hvac_kw(m, temp_c)) * dist_km / m.avg_speed_kmh
    return {
        "rolling": roll / J_PER_KWH * cold, "aero": aero / J_PER_KWH * cold,
        "stop_go": stop / J_PER_KWH * cold, "climb": climb / J_PER_KWH * cold,
        "aux": aux,
    }


@lru_cache(maxsize=None)
def calibration(model_key: str) -> float:
    """Scale on the mechanical terms so reference conditions hit rated consumption."""
    m = TRUCK_MODELS[model_key]
    t = _terms(m, REF["dist_km"], REF["payload_frac"] * m.max_payload_kg,
               REF["elev_m_km"], REF["temp_c"])
    mech = t["rolling"] + t["aero"] + t["stop_go"] + t["climb"]
    return (m.consumption_kwh_km * REF["dist_km"] - t["aux"]) / mech


def trip_energy(model_key: str, dist_km: float, payload_kg: float,
                elev_m_km: float, temp_c: float) -> dict[str, float]:
    """Battery energy (kWh) for the trip, with a per-term breakdown."""
    m = TRUCK_MODELS[model_key]
    t = _terms(m, dist_km, payload_kg, elev_m_km, temp_c)
    cal = calibration(model_key)
    out = {k: (v * cal if k != "aux" else v) for k, v in t.items()}
    out["total"] = sum(out.values())
    out["kwh_per_km"] = out["total"] / dist_km
    return out
