"""Fleet Charge Controller web server (stdlib only).

    python server.py            # http://localhost:8000

Serves the single-page yard UI from ./web and a small JSON API that runs the
real simulator and scheduler:

    GET  /api/status                 run progress
    POST /api/run   {seed,temp,fault}  start a simulation of all four policies
    GET  /api/data                   results of the last run (compact arrays)
    GET  /api/truck?vid=T07          energy requirement breakdown for one truck
    POST /api/explain {vid,question} Nemotron explanation grounded in scheduler facts
    POST /api/notes   {text}         dispatcher notes -> actions -> scenario update
    POST /api/preset  {name}         curtail | cold | routes | reset
    POST /api/apply   {actions}      apply recommended actions
    POST /api/weather {city}         live temperature (Tavily + Nemotron) -> energy model
"""
from __future__ import annotations

import dataclasses
import json
import math
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from fleetcharge import agent
from fleetcharge.scenario import MAX_PLANNED_TARGET, make_scenario, slot_to_clock
from fleetcharge.yard import LABELS, POLICIES, STATE_NAMES, run

WEB = Path(__file__).parent / "web"
LOCK = threading.Lock()


class App:
    def __init__(self) -> None:
        self.seed, self.temp, self.fault = 0, 15.0, 0.0
        self.scn = make_scenario(self.seed, temp_c=self.temp)
        self.results: dict = {}
        self.running = False
        self.progress = {"done": 0, "total": len(POLICIES), "label": "idle"}
        self.error: str | None = None
        self.log: list[str] = []
        self.weather: dict | None = None
        self.version = 0

    def start(self, seed: int | None = None, temp: float | None = None, fault: float | None = None) -> None:
        with LOCK:
            if self.running:
                return
            if seed is not None and (seed != self.seed):
                self.seed = seed
                self.scn = make_scenario(seed, temp_c=self.temp if temp is None else temp)
                self.log, self.weather = [], None
            if temp is not None and abs(temp - self.scn.temp_c) > 1e-9:
                self.scn.apply_temperature(temp)
            self.temp = self.scn.temp_c
            if fault is not None:
                self.fault = fault
            self.running, self.error = True, None
            self.progress = {"done": 0, "total": len(POLICIES), "label": "starting"}
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            res = {}
            for i, p in enumerate(POLICIES):
                self.progress = {"done": i, "total": len(POLICIES), "label": LABELS[p]}
                res[p] = run(self.scn, p, fault_rate=self.fault, seed=self.seed)
            self.results = res
            self.version += 1
            self.progress = {"done": len(POLICIES), "total": len(POLICIES), "label": "done"}
        except Exception as e:  # surface to the UI instead of dying silently
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.running = False

    # ---- scenario presets, one-click apply, recommendations ---------------
    def preset(self, name: str) -> dict:
        s = self.scn
        if name == "curtail":
            s.site = dataclasses.replace(s.site, grid_limit_kw=900.0)
            lines = ["Grid curtailment alert: site import limit cut from 1,500 kW to 900 kW for the whole day"]
        elif name == "cold":
            s.apply_temperature(-10.0)
            self.temp = -10.0
            lines = ["Cold snap at -10 C: heating, battery and tyre losses raise every truck's energy requirement"]
        elif name == "routes":
            limited = 0
            for v in s.vehicles:
                if v.model_key != "vnr_electric":
                    continue
                orig = v.route_km
                want = orig + 3 * v.model.avg_speed_kmh
                v.route_km = want
                v.refresh_target(s.temp_c, s.policy)
                while v.target > MAX_PLANNED_TARGET and v.route_km > orig:
                    v.route_km *= 0.97
                    v.refresh_target(s.temp_c, s.policy)
                limited += v.route_km < want - 1
            n = sum(1 for v in s.vehicles if v.model_key == "vnr_electric")
            lines = [f"Route extension: regional tractors drive 3 h further (about +180 km); "
                     f"{limited} of {n} are held to what the battery can carry"]
        elif name == "reset":
            self.scn = make_scenario(self.seed, temp_c=15.0)
            self.temp, self.log, self.weather = 15.0, [], None
            lines = ["Scenario reset to the original day"]
        else:
            raise ValueError("unknown preset")
        if name != "reset":
            self.log.extend(lines)
        return {"lines": lines}

    def apply(self, actions: list[dict]) -> dict:
        acts = agent._validate(actions, self.scn)
        lines = agent.apply_actions(self.scn, acts)
        self.log.extend(lines)
        return {"lines": lines}

    def _max_route_for_energy(self, v, energy_kwh: float) -> int:
        s, orig = self.scn, v.route_km
        lo, hi = 1.0, orig
        for _ in range(30):
            mid = (lo + hi) / 2
            v.route_km = mid
            if v.requirement(s.temp_c, s.policy)["required"] <= energy_kwh:
                lo = mid
            else:
                hi = mid
        v.route_km = orig
        return int(lo)

    def _max_payload_for_target(self, v) -> int:
        s, orig = self.scn, v.payload_kg
        lo, hi = 0.0, orig
        for _ in range(30):
            mid = (lo + hi) / 2
            v.payload_kg = mid
            if v.requirement(s.temp_c, s.policy)["required"] <= MAX_PLANNED_TARGET * v.cap_plan_kwh:
                lo = mid
            else:
                hi = mid
        v.payload_kg = orig
        return int(lo)

    def recommendations(self, policy: str) -> list[dict]:
        res = self.results.get(policy)
        if res is None:
            return []
        s, recs = self.scn, []
        eta = s.site.charger_efficiency
        for i, v in enumerate(s.vehicles):
            row = res.vehicles.iloc[i]
            if row.ready_on_time:
                continue
            short, got = float(row.shortfall_kwh), float(row.final_kwh)
            opts = []
            if v.target > 1.0:
                kg = self._max_payload_for_target(v)
                km = self._max_route_for_energy(v, MAX_PLANNED_TARGET * v.cap_plan_kwh)
                detail = (f"{v.vid} needs {v.target_kwh:.0f} kWh for a {v.route_km:.0f} km trip with "
                          f"{v.payload_kg:,.0f} kg, more than its {v.cap_plan_kwh:.0f} kWh battery can hold.")
                if kg > 0:
                    opts.append({"label": f"Cut cargo to {kg:,} kg",
                                 "actions": [{"type": "set_payload", "vid": v.vid, "payload_kg": kg}]})
                else:
                    detail += " Even empty it cannot make the distance, so the trip needs a charging stop."
                opts.append({"label": f"Shorten the route to {km} km",
                             "actions": [{"type": "set_route", "vid": v.vid, "route_km": km}]})
                recs.append({"vid": v.vid, "severity": "critical", "rank": 0,
                             "title": f"{v.vid}: trip cannot be fully charged", "detail": detail, "options": opts})
                continue
            if not v.online:
                opts.append({"label": "Mark charger repaired",
                             "actions": [{"type": "charger_restored", "vid": v.vid}]})
            mins = int(math.ceil((short / (eta * 0.6 * v.max_kw_true) * 60 + 15) / 15) * 15) if short > 0.5 else 15
            opts.append({"label": f"Delay departure by {mins} min",
                         "actions": [{"type": "delay_departure", "vid": v.vid, "minutes": mins}]})
            if short > 0.5:
                km = self._max_route_for_energy(v, got)
                opts.append({"label": f"Shorten the route to {km} km",
                             "actions": [{"type": "set_route", "vid": v.vid, "route_km": km}]})
            detail = (f"{v.vid} was not ready at the gate by {s_clock(v.depart_slot)}: "
                      f"{short:.1f} kWh short of its {v.target_kwh:.0f} kWh requirement.")
            if not v.online:
                detail += " Its charger is marked faulted, so repair or swap the connector first."
            recs.append({"vid": v.vid, "severity": "critical", "rank": 1, "title": f"{v.vid} missed its departure",
                         "detail": detail, "options": opts})
        for e in [e for e in res.events if e["topic"] == "charger_fault"][:3]:
            vid, slot = e["vid"], e["slot"]
            v = s.vehicle(vid)
            cands = [s.vehicles[j] for j in range(len(s.vehicles))
                     if s.vehicles[j].model_key == v.model_key and res.states[j, slot] == 1]
            cands.sort(key=lambda c: -c.depart_slot)
            if cands:
                c = cands[0]
                detail = (f"{vid}'s connector failed at {e['time']} until {e['until']} and holds its bay. "
                          f"Recommend moving {vid} back to the waiting lot and docking {c.vid} "
                          f"(departs {s_clock(c.depart_slot)}) in the bay until it is repaired.")
            else:
                detail = (f"{vid}'s connector failed at {e['time']} until {e['until']}. No same-model truck "
                          f"is waiting, so keep the bay held and check the connector.")
            recs.append({"vid": vid, "severity": "warning", "title": f"Charger fault on {vid}",
                         "detail": detail, "options": []})
        recs.sort(key=lambda r: (r["severity"] != "critical", r.get("rank", 2)))
        return recs[:8]

    # ---- payloads -------------------------------------------------------
    def data(self) -> dict:
        s = self.scn
        out = {
            "version": self.version, "seed": self.seed, "temp_c": s.temp_c, "fault": self.fault,
            "clocks": [slot_to_clock(i) for i in range(96)],
            "price": [round(float(x), 3) for x in s.price], "solar": [round(float(x)) for x in s.solar_actual],
            "limit": s.site.grid_limit_kw, "bays": dict(s.yard.bays), "labels": LABELS,
            "states": STATE_NAMES, "log": self.log[-12:], "weather": self.weather,
            "trucks": [{
                "vid": v.vid, "model": v.model.name, "key": v.model_key, "route": round(v.route_km),
                "payload": round(v.payload_kg), "elev": round(v.elev_m_km, 1), "soh": round(v.soh_true, 3),
                "soh_rep": round(v.soh_rep, 3), "captrue": round(v.cap_true_kwh, 1), "a": v.arrive_slot, "d": v.depart_slot,
                "need": round(v.target_kwh, 1), "target": round(v.target, 3), "cap": round(v.cap_plan_kwh, 1),
                "maxkw": round(v.max_kw_true, 1), "prio": v.priority, "online": v.online,
            } for v in s.vehicles],
            "pol": {},
        }
        for p, r in self.results.items():
            m = r.metrics
            out["pol"][p] = {
                "st": r.states.tolist(),
                "soc": np.where(np.isnan(r.soc), -1, np.round(r.soc * 1000)).astype(int).tolist(),
                "pw": np.round(r.power).astype(int).tolist(),
                "bay": r.bay.tolist(), "q": r.qpos.tolist(),
                "load": [round(float(x)) for x in r.slots.load_kw], "grid": [round(float(x)) for x in r.slots.grid_kw],
                "m": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()},
                "recs": self.recommendations(p),
                "events": [e for e in r.events if e["topic"] in
                           ("docking", "plugged_in", "ready", "departure", "charger_fault", "charger_restored", "plan_shortfall")][:400],
            }
        return out

    def truck(self, vid: str) -> dict:
        v = self.scn.vehicle(vid)
        req = v.requirement(self.scn.temp_c, self.scn.policy)
        return {"vid": vid, "requirement": {k: round(x, 2) for k, x in req.items()},
                "temp_c": self.scn.temp_c, "cap_plan": round(v.cap_plan_kwh, 1), "cap_true": round(v.cap_true_kwh, 1),
                "llm": agent.llm_available()}

    def explain(self, vid: str, question: str, policy: str) -> dict:
        res = self.results.get(policy) or next(iter(self.results.values()))
        ans, src = agent.explain(question or f"Why did {vid} wait and charge when it did?",
                                 agent.build_facts(self.scn, res, vid))
        return {"answer": ans, "source": src}

    def notes(self, text: str) -> dict:
        acts, src = agent.parse_instructions(text, self.scn)
        lines = agent.apply_actions(self.scn, acts)
        self.log.extend(lines)
        return {"source": src, "actions": [a.__dict__ for a in acts], "lines": lines}

    def set_weather(self, city: str) -> dict:
        w = agent.weather(city)
        self.weather = w
        self.scn.apply_temperature(w["temp_c"])
        self.temp = w["temp_c"]
        self.log.append(f"Weather {city}: {w['temp_c']:.1f} °C ({w['source']}), trip energy recomputed")
        return w


def s_clock(slot: int) -> str:
    return slot_to_clock(slot)


APP = App()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:   # keep the console quiet
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/status":
                return self._json({"running": APP.running, "progress": APP.progress, "error": APP.error,
                                   "version": APP.version, "llm": agent.llm_available(),
                                   "tavily": bool(os.getenv("TAVILY_API_KEY"))})
            if u.path == "/api/data":
                return self._json(APP.data())
            if u.path == "/api/truck":
                return self._json(APP.truck(q["vid"]))
            path = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            f = (WEB / path).resolve()
            if WEB.resolve() not in f.parents and f != WEB.resolve() or not f.is_file():
                self.send_error(404)
                return
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(str(f))[0] or "text/plain") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:
        u, b = urlparse(self.path), None
        try:
            b = self._body()
            if u.path == "/api/run":
                APP.start(b.get("seed"), b.get("temp"), b.get("fault"))
                return self._json({"started": True})
            if u.path == "/api/preset":
                return self._json(APP.preset(b["name"]))
            if u.path == "/api/apply":
                return self._json(APP.apply(b.get("actions", [])))
            if u.path == "/api/notes":
                return self._json(APP.notes(b.get("text", "")))
            if u.path == "/api/weather":
                return self._json(APP.set_weather(b.get("city", "Chicago")))
            if u.path == "/api/explain":
                return self._json(APP.explain(b["vid"], b.get("question", ""), b.get("policy", "smart")))
            self.send_error(404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    APP.start()
    srv = ThreadingHTTPServer((os.getenv("HOST", "127.0.0.1"), port), Handler)
    print(f"Fleet Charge Controller on http://localhost:{port}  (first simulation takes ~25 s)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
