# Submission kit

## Demo video script (target 2:40, under 3:00)

Record the screen at http://localhost:8000 with voice-over. Keep the yard simulation visibly running for at least 60 s in total (press play at 4x).

| Time | On screen | Say |
|---|---|---|
| 0:00-0:15 | Yard view, trucks charging | "50 electric trucks, 11 shared chargers. Who charges when decides the energy bill, the battery wear, and whether a truck leaves on time. This is Fleet Charge Controller, a simulated depot built for the Physical AI track." |
| 0:15-0:40 | Press play 4x; trailers fill, queue grows, ready lane | "Each truck is drawn with a trailer that fills as the battery charges. Amber is charging, green is ready, slate is waiting. Vans, regional tractors and long-haul tractors have separate bay pools. Plug-in and unplug delays are simulated, and the trucks report noisy charge and health readings." |
| 0:40-1:05 | Click a truck, show energy breakdown | "Each truck charges only what its trip needs: distance, cargo weight, terrain and temperature feed a physics model calibrated to the published range, plus a 15 percent margin and a 10 percent reserve." |
| 1:05-1:30 | Bay schedule tab, hover a bar; site chart with price and headroom | "A two-stage optimiser assigns bays, then sets power for each truck inside its window, following the price, solar and the grid limit. Language models never pick a bay or a charge level, so deadlines never depend on a model." |
| 1:30-2:05 | Dispatcher drawer: type "Truck 5 is carrying 1.6 tonnes. Truck 30 route is now 280 km." Apply. Click Cold snap preset | "This is where Nvidia Nemotron on Nebius Token Factory comes in. Nemotron Nano turns a dispatcher's note into validated actions. Tavily looks up live weather and Nemotron extracts the temperature. The energy model recomputes, and the scheduler re-plans." |
| 2:05-2:25 | Click Explain on a truck | "Nemotron Super explains any decision in plain language from a fact sheet of the real numbers." |
| 2:25-2:50 | Compare tab | "Over eight simulated depot days the smart scheduler cuts cost 39 percent against first come first served, lowers the peak, charges at a gentler rate, and cuts hours spent nearly full from 241 to 127, with every departure made. Code and brief are in the repo. Thank you." |

Upload to YouTube as Public (not Unlisted) today.

## Devpost text

**Tagline:** Charges 50 electric trucks through 11 chargers: cheaper, gentler on batteries, and on time.

**Description:** Fleet Charge Controller is a scheduler and edge-control system for an electric truck depot with fewer chargers than trucks. It decides which truck takes which bay and when, then charges each truck at the cheapest and gentlest rate that still gets it to the gate with exactly the energy its trip needs. A physics model turns distance, cargo, terrain and temperature into required energy; a per-pool MILP assigns bays; an LP sets power against dynamic price, solar, the demand charge and a battery-health proxy; a one-minute edge loop guards deadlines under noisy telemetry. Nemotron on Nebius Token Factory parses dispatcher notes, extracts temperature from Tavily search results, and explains decisions. Deterministic optimisation, not the LLM, controls the chargers. Simulated depot, 50 trucks (Ford E-Transit, Volvo VNR Electric, Mercedes eActros 600). Result over 8 days: -39 % cost, -27 % peak, 0 missed departures.

**Nebius/NVIDIA usage:** Nebius Token Factory runtime calls to `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` (parsing, extraction) and `nvidia/nemotron-3-super-120b-a12b` (explanations).

**Built with:** Python, NumPy, SciPy (HiGHS), pandas, Nebius Token Factory, NVIDIA Nemotron, Tavily.

## Feedback draft (edit to match your experience)

- Token Factory: OpenAI-compatible endpoint made integration trivial. Model IDs are case-sensitive and must be copied from /v1/models.
- Nemotron reasoning models spent output tokens on hidden reasoning, so short max_tokens values returned truncated answers (finish_reason "length"). Documenting a recommended max_tokens or a way to cap reasoning would help.
- Nano was fast and reliable for structured JSON extraction; Super gave clearer explanations.
- Wish list: JSON-schema constrained output on Nemotron, and a cost estimate in the response.

## Final checklist
- [ ] Public GitHub repo pushed (MIT license, README) and URL pasted into Devpost
- [ ] YouTube video public, under 3:00
- [ ] Physical AI footage requirement: see note in chat (simulation, confirm with organisers)
- [ ] Feedback form filled
- [ ] Teammates added and accepted
- [ ] Rotate Nebius and Tavily keys after submission; do not edit repo after the deadline (Fri 30 Oct, 10:00 AM PT)
