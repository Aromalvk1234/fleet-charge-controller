# Fleet Charge Controller

A scheduler and edge-control system for an electric truck depot with **fewer charging bays than trucks**. It decides which truck takes which bay and when, then charges each truck at the cheapest and gentlest rate that still gets it to the gate on time with exactly the energy its trip needs.

Built for the **Nebius x NVIDIA Global AI Hackathon**, Best Apps and Agents track. A long technical brief with the math, diagrams and results is in [docs/project-brief.html](docs/project-brief.html).

## What it does

| Concern | How it is handled |
|---|---|
| How much energy a truck needs | Physics model from distance, cargo weight, terrain and temperature, calibrated to each truck's published range, plus a 15 % margin and a 10 % reserve |
| Which truck gets which bay | Per-pool time-indexed MILP over arrival, deadline and charge time; separate pools for each truck model |
| How fast to charge | LP inside each truck's bay window: dynamic price net of solar, demand charge, battery-health proxy, site limit |
| Handling delays | Plug-in and unplug time in the schedule and in the simulator; trucks must be ready 15 minutes before departure |
| SOC and SOH | Trucks report noisy SOC every slot and SOH once; physics runs on the true values; older packs have less capacity and accept less power |
| Changes during the day | Re-plans on arrivals, finished trucks, faults, departures and every 4 slots; a 1-minute edge guard adds power if a truck falls behind |
| Plain-language operations | Nemotron turns dispatcher notes into validated actions, Tavily + Nemotron supply live temperature, Nemotron explains decisions from a fact sheet |

### Fleet (simulated, public specs, see `fleetcharge/specs.py`)
28 x Ford E-Transit (89 kWh, 19.2 kW AC) · 14 x Volvo VNR Electric (~452 kWh usable, 150 kW depot DC) · 8 x Mercedes-Benz eActros 600 (~590 kWh usable, 180 kW depot DC). 11 bays: 6 van, 3 regional tractor, 2 long-haul tractor. Site: 1.5 MW grid limit, 900 kW solar.

## Architecture

```
 dispatcher notes ─► Nemotron Nano ─► validated actions ─┐
 Tavily weather ───► Nemotron Nano ─► temperature ───────┤
                                                         ▼
 price, solar ─►  physics energy model ─► Stage 1: bay MILP (per pool) ─► Stage 2: power LP
                                               ▲                                │ kW setpoints
        reported SOC / SOH (noisy) ────────────┘                                ▼
                                                      edge agents, 1-minute loop (taper, thermal,
                                                      deadline guard, site cap) in the yard twin
```

Language models never choose a charge level or a bay. Optimisation is deterministic, so deadlines are not at the mercy of a model.

| Role | Model on Nebius Token Factory |
|---|---|
| Note parsing, temperature extraction | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` |
| Explanations | `nvidia/nemotron-3-super-120b-a12b` |

Override with `NEMOTRON_FAST_MODEL` and `NEMOTRON_STRONG_MODEL`. Every model and Tavily call has an offline fallback.

## Run

Test build for judges: clone, install, run. It works without any keys (offline fallbacks); add the two keys to see live Nemotron and Tavily.

```bash
pip install -r requirements.txt
export NEBIUS_API_KEY=...      # optional, offline fallbacks otherwise
export TAVILY_API_KEY=...      # optional
python server.py               # http://localhost:8000  (first simulation takes ~25 s)
python -m pytest -q
```

The web interface (`web/`) shows the yard as a lot: bays with trucks whose trailers fill as the battery charges (with time left to the target on each card), per-pool waiting queues with slack chips, a ready lane, filters for at-risk trucks and trucks above 80 % charge, a scrubber with tariff, solar, arrival and departure markers and quick-jump buttons, a dual-axis site power chart with grid-limit headroom shading, a bay schedule with hover details and a synced cursor, a policy comparison with a battery-stress panel, a truck drawer with the energy breakdown and a Nemotron explanation, and a dispatcher drawer with recommended actions, one-click scenarios (grid curtailment, cold snap, route extension) and free-text notes. It adapts from desktop monitors to 1,024 px tablets.

Benchmark (designed as a **Nebius Serverless Job**, see `Dockerfile`):

```bash
python scripts/benchmark.py --seeds 20 --workers 8 --out results/benchmark.csv
python scripts/benchmark.py --seeds 20 --temp -10 --fault-rate 0.004      # cold day + charger faults
```

## Results (simulated, 8 depot-days, with delays and noisy telemetry)

| Policy | Cost / day | Peak kW | Missed / day | Mean wait (h) | Avg charge rate | Hours above 80 % charge |
|---|---|---|---|---|---|---|
| First come, first served | $1,804 | 872 | 0.00 | 1.2 | 0.29 C | 241 |
| Earliest departure first | $1,806 | 872 | 0.00 | 1.2 | 0.29 C | 238 |
| **Smart scheduler** | **$1,095 (-39 %)** | 635 | 0.00 | 2.7 | 0.23 C | 127 |
| Ideal (a charger per truck, bound) | $649 | 392 | 0.00 | 0.0 | 0.17 C | 50 |

On six cold days (-10 °C) with charger faults: smart $1,305 against $1,937 for first come (-33 %), and both miss about 0.5 departures per day. The smart scheduler makes trucks wait longer by design: it holds them for cheaper charging windows and still dispatches them on time, and it charges more gently (lower average rate, far fewer hours sitting nearly full). These numbers come from a synthetic generator and say nothing about a real depot.

## Nebius / NVIDIA usage
- **Nebius Token Factory**: runtime Nemotron calls for note parsing, temperature extraction and explanations.
- **NVIDIA Nemotron**: Nano (notes, temperature) and Super (explanations). Nebius retires both on 9 Nov 2026, so each role has a fallback chain that is tried in order: fast `NVIDIA-Nemotron-3-Nano-30B-A3B` -> `Nemotron-3.5-Super-VL`; strong `nemotron-3-super-120b-a12b` -> `Nemotron-3-Ultra-550b-a55b`. If every model fails, the app uses its offline fallbacks. Override with comma-separated lists, e.g. `NEMOTRON_FAST_MODEL=modelA,modelB` and `NEMOTRON_STRONG_MODEL=modelC,modelD`. The model that answered is shown in the UI.
- **Tavily**: live weather lookup that feeds the energy model.
- **Nebius Serverless Jobs**: the benchmark script and Dockerfile are ready; a large cloud run has not been done yet.

## Limitations
- Prices, solar, arrivals, trips, cargo and SOH are synthetic. Truck battery and charging figures are public; mass and drag values are estimates tied to the real range by calibration.
- The battery-health term is a convex proxy, not a fitted ageing model.
- The planner is deterministic with a fixed cushion; a robust planner could use less margin.
- Occasional missed departures remain, mostly after charger faults on tight deadlines.
- No real charger protocol: the edge agents are simulated.
