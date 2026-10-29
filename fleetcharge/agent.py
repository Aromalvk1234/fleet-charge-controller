"""Agent layer: Nemotron on Nebius Token Factory (OpenAI-compatible) + Tavily.

The LLM never decides charge power or bay assignment. It (1) turns free-text
dispatcher notes into validated structured actions, (2) reads live weather via
Tavily so the physics energy model uses the real temperature, and (3) explains
scheduler decisions using real numbers. Every call has a deterministic offline
fallback so the demo runs without keys.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import httpx
import numpy as np

from .scenario import DT_H, MAX_PLANNED_TARGET, T_SLOTS, Scenario, clock_to_slot, slot_to_clock


def _feasibility_note(v) -> str:
    if v.target > 1.0:
        return " WARNING: more than the battery can hold, so this trip needs a charging stop or less cargo"
    if v.target > MAX_PLANNED_TARGET:
        return " (above the 95% dispatch limit: very little room for error)"
    return ""

TOKEN_FACTORY_URL = os.getenv("TOKEN_FACTORY_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
FAST_MODEL = os.getenv("NEMOTRON_FAST_MODEL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
STRONG_MODEL = os.getenv("NEMOTRON_STRONG_MODEL", "nvidia/nemotron-3-super-120b-a12b")
TAVILY_URL = "https://api.tavily.com/search"

ACTIONS = {"set_route", "delay_departure", "set_departure", "charger_fault",
           "charger_restored", "set_payload"}
ACTION_SCHEMA = """Return ONLY JSON: {"actions":[{"type": "set_route"|"delay_departure"|"set_departure"|"charger_fault"|"charger_restored"|"set_payload",
"vid": "T07", "route_km": number|null, "minutes": number|null, "clock": "HH:MM"|null, "payload_kg": number|null}]}
Truck ids look like T01..T50. Cargo weights in tonnes must be converted to kg. Use null for unused fields."""


def llm_available() -> bool:
    return bool(os.getenv("NEBIUS_API_KEY"))


def chat(messages: list[dict], model: str = FAST_MODEL, max_tokens: int = 1500,
         temperature: float = 0.1) -> str:
    r = httpx.post(
        f"{TOKEN_FACTORY_URL}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['NEBIUS_API_KEY']}"},
        json={"model": model, "messages": messages, "max_tokens": max_tokens,
              "temperature": temperature},
        timeout=60,
    )
    r.raise_for_status()
    choice = r.json()["choices"][0]
    if choice.get("finish_reason") == "length":   # reasoning ate the budget -> don't return a fragment
        raise RuntimeError("model output truncated")
    text = choice["message"]["content"] or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError("no JSON in model output")
    return json.loads(m.group(0))


# --------------------------------------------------------------------------
# 1. dispatcher notes -> structured actions
# --------------------------------------------------------------------------
@dataclass
class Action:
    type: str
    vid: str
    route_km: float | None = None
    minutes: float | None = None
    clock: str | None = None
    payload_kg: float | None = None


def _validate(raw: list[dict], s: Scenario) -> list[Action]:
    valid_ids = {v.vid for v in s.vehicles}
    out = []
    for a in raw:
        vid = str(a.get("vid", "")).upper()
        if vid not in valid_ids or a.get("type") not in ACTIONS:
            continue
        out.append(Action(a["type"], vid, a.get("route_km"), a.get("minutes"), a.get("clock"),
                          a.get("payload_kg")))
    return out


def _regex_parse(text: str, s: Scenario) -> list[Action]:
    """Offline fallback covering the common phrasings."""
    acts: list[Action] = []
    for sentence in re.split(r"[.;\n]", text):
        ids = [f"T{int(x):02d}" for x in re.findall(r"(?:truck|van|T)\s*#?0?(\d{1,2})\b", sentence, re.I)]
        if not ids:
            continue
        low = sentence.lower()
        km = re.search(r"(\d+(?:\.\d+)?)\s*km", low)
        cargo = re.search(r"(\d+(?:\.\d+)?)\s*(kg|t|tonnes?|tons?)\b", low)
        delay = re.search(r"(?:delayed?|late|later)\D{0,12}(\d+(?:\.\d+)?)\s*(h|hour|hr|min)", low) \
            or re.search(r"(\d+(?:\.\d+)?)\s*(h|hour|hr|min)\w*\s+(?:late|delay)", low)
        clock = re.search(r"(?:at|by|until)\s+(\d{1,2}):(\d{2})", low)
        for vid in ids:
            if "fault" in low or "offline" in low or "broken" in low:
                acts.append(Action("charger_fault", vid))
            elif "restored" in low or "back online" in low:
                acts.append(Action("charger_restored", vid))
            elif delay:
                n = float(delay.group(1)) * (60 if delay.group(2).startswith("h") else 1)
                acts.append(Action("delay_departure", vid, minutes=n))
            elif clock and ("leave" in low or "depart" in low):
                acts.append(Action("set_departure", vid, clock=f"{int(clock.group(1)):02d}:{clock.group(2)}"))
            elif cargo and any(w in low for w in ("cargo", "load", "payload", "carrying", "weigh")):
                kg = float(cargo.group(1)) * (1 if cargo.group(2) == "kg" else 1000)
                acts.append(Action("set_payload", vid, payload_kg=kg))
            elif km:
                acts.append(Action("set_route", vid, route_km=float(km.group(1))))
    return _validate([a.__dict__ for a in acts], s)


def parse_instructions(text: str, s: Scenario) -> tuple[list[Action], str]:
    """Returns (actions, source) where source is the model name or 'offline-regex'."""
    if llm_available():
        try:
            raw = chat([
                {"role": "system", "content": "You convert fleet dispatcher notes into structured actions. " + ACTION_SCHEMA},
                {"role": "user", "content": text}], model=FAST_MODEL)
            acts = _validate(_extract_json(raw).get("actions", []), s)
            if acts:
                return acts, FAST_MODEL
        except Exception:
            pass
    return _regex_parse(text, s), "offline-regex"


def apply_actions(s: Scenario, actions: list[Action]) -> list[str]:
    """Mutates the scenario; returns human-readable log lines."""
    log = []
    for a in actions:
        v = s.vehicle(a.vid)
        if a.type == "set_route" and a.route_km:
            v.route_km = float(a.route_km)
            v.refresh_target(s.temp_c, s.policy)
            log.append(f"{a.vid}: route -> {a.route_km:.0f} km, needs {v.target_kwh:.0f} kWh ({v.target:.0%} SOC)" + _feasibility_note(v))
        elif a.type == "delay_departure" and a.minutes:
            v.depart_slot = min(T_SLOTS - 1, v.depart_slot + int(round(a.minutes / 60 / DT_H)))
            log.append(f"{a.vid}: departure delayed {a.minutes:.0f} min -> {slot_to_clock(v.depart_slot)}")
        elif a.type == "set_departure" and a.clock:
            v.depart_slot = min(T_SLOTS - 1, max(v.arrive_slot + 1, clock_to_slot(a.clock)))
            log.append(f"{a.vid}: departure set to {slot_to_clock(v.depart_slot)}")
        elif a.type == "set_payload" and a.payload_kg is not None:
            v.payload_kg = float(np.clip(a.payload_kg, 0, v.model.max_payload_kg))
            v.refresh_target(s.temp_c, s.policy)
            log.append(f"{a.vid}: cargo {v.payload_kg:.0f} kg, needs {v.target_kwh:.0f} kWh ({v.target:.0%} SOC)" + _feasibility_note(v))
        elif a.type == "charger_fault":
            v.online = False
            log.append(f"{a.vid}: charger FAULT")
        elif a.type == "charger_restored":
            v.online = True
            log.append(f"{a.vid}: charger restored")
    return log


# --------------------------------------------------------------------------
# 2. Tavily weather -> temperature for the physics model
# --------------------------------------------------------------------------
def tavily_search(query: str, max_results: int = 3) -> dict:
    r = httpx.post(TAVILY_URL, json={"query": query, "max_results": max_results,
                                     "include_answer": True, "search_depth": "basic"},
                   headers={"Authorization": f"Bearer {os.environ['TAVILY_API_KEY']}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def weather(city: str) -> dict:
    """Live temperature via Tavily; Nemotron extracts the number. Falls back to 15 C."""
    info = {"city": city, "temp_c": 15.0, "source": "default", "summary": ""}
    if not os.getenv("TAVILY_API_KEY"):
        return info
    try:
        res = tavily_search(f"{city} weather forecast tomorrow temperature celsius")
        blob = (res.get("answer") or "") + "\n" + "\n".join(x.get("content", "")[:400] for x in res.get("results", []))
        info["summary"] = (res.get("answer") or blob)[:300]
        temp = None
        if llm_available():
            out = chat([{"role": "system", "content": 'From the text, extract the expected temperature in Celsius for tomorrow (if no forecast is given, the current temperature). Convert from Fahrenheit if needed. If NO temperature is stated, return null, never guess. Return ONLY JSON {"temp_c": number|null}.'},
                        {"role": "user", "content": blob}])
            temp = _extract_json(out).get("temp_c")
        if temp is None or not -45 <= float(temp) <= 55:
            temp = None
            m = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*C\b", blob)
            temp = float(m.group(1)) if m else None
        if temp is not None:
            info.update(temp_c=float(temp), source="tavily" + ("+nemotron" if llm_available() else ""))
    except Exception as e:  # network/key problems must never break the controller
        info["summary"] = f"weather lookup failed: {e}"
    return info


# --------------------------------------------------------------------------
# 3. grounded explanations
# --------------------------------------------------------------------------
def explain(question: str, facts: dict) -> tuple[str, str]:
    """Answer using ONLY the supplied scheduler facts. Returns (answer, source)."""
    if llm_available():
        try:
            ans = chat([
                {"role": "system", "content": "You are the fleet charging scheduler's explainer. Answer in 3-5 sentences using ONLY the JSON facts. If a fact is missing, say so. Mention numbers. Explain the energy requirement (distance, cargo, terrain, temperature, margin, reserve), WHY the truck waited and WHEN it charged (price, solar, deadline, bay availability), and whether it charged gently to protect the battery. Required energy already INCLUDES margin and reserve."},
                {"role": "user", "content": f"Facts:\n{json.dumps(facts, default=str)}\n\nQuestion: {question}"}],
                model=STRONG_MODEL, max_tokens=2500)
            return ans, STRONG_MODEL
        except Exception:
            pass
    f = facts
    return (f"{f['vid']} ({f['model']}) must leave at {f['depart']} with {f['required_kwh']} kWh for a "
            f"{f['route_km']} km trip carrying {f['payload_kg']} kg. It arrived at {f['arrive']} with "
            f"{f['soc_arrive']} SOC, waited {f['waited_h']} h for a bay, then charged "
            f"{f['charged_kwh']} kWh (avg {f['avg_price_paid']} $/kWh vs {f['avg_price_window']} window avg) "
            f"and finished at {f['final_soc']}."), "offline-template"


def _charge_windows(p, price) -> list[dict]:
    """Contiguous charging runs: start-end clock time, avg kW, avg $/kWh."""
    runs, i, n = [], 0, len(p)
    while i < n:
        if p[i] > 0.5:
            j = i
            while j + 1 < n and p[j + 1] > 0.5:
                j += 1
            runs.append({"from": slot_to_clock(i), "to": slot_to_clock(j + 1),
                         "avg_kw": round(float(p[i:j + 1].mean()), 1),
                         "avg_price": round(float(price[i:j + 1].mean()), 3)})
            i = j + 1
        else:
            i += 1
    return runs[:6]


def build_facts(s: Scenario, res, vid: str) -> dict:
    """Ground-truth numbers about one truck from a finished yard simulation."""
    i = res.vids.index(vid)
    v = s.vehicle(vid)
    p = res.power[i]
    req = v.requirement(s.temp_c, s.policy)
    row = res.vehicles[res.vehicles.vid == vid].iloc[0]
    window = slice(v.arrive_slot, v.depart_slot)
    paid = float((p * s.price).sum() / p.sum()) if p.sum() > 0 else 0.0
    return {
        "vid": vid, "model": v.model.name, "pool_bays": s.yard.bays_for(v.model_key),
        "route_km": round(v.route_km), "payload_kg": round(v.payload_kg),
        "ascent_m_per_km": round(v.elev_m_km, 1), "temperature_c": s.temp_c,
        "soh_reported": round(v.soh_rep, 3),
        "trip_energy_kwh": round(req["total"], 1), "margin_kwh": round(req["margin"], 1),
        "reserve_kwh": round(req["reserve"], 1), "required_kwh": round(req["required"], 1),
        "arrive": slot_to_clock(v.arrive_slot), "depart": slot_to_clock(v.depart_slot),
        "soc_arrive": f"{v.soc_arrive:.0%}", "final_soc": f"{res.soc[i, v.depart_slot - 1]:.0%}",
        "waited_h": float(row.wait_h), "charged_kwh": round(float(p.sum() * DT_H * s.site.charger_efficiency), 1),
        "charging_slots": _charge_windows(p, s.price),
        "avg_price_paid": round(paid, 3), "avg_price_window": round(float(s.price[window].mean()), 3),
        "cheapest_price_in_window": round(float(s.price[window].min()), 3),
        "priciest_price_in_window": round(float(s.price[window].max()), 3),
        "peak_charge_kw": round(float(p.max()), 1), "max_rating_kw": round(v.max_kw_true, 1),
        "ready_on_time": bool(row.ready_on_time),
    }
