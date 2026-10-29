"""Policy benchmark over many simulated depot-days. Designed to run as a
Nebius Serverless Job (one container, many seeds) or locally.

  python scripts/benchmark.py --seeds 20 --workers 4 --out results/benchmark.csv
  python scripts/benchmark.py --seeds 20 --temp -10 --fault-rate 0.004   # cold day + charger faults
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fleetcharge.scenario import make_scenario  # noqa: E402
from fleetcharge.yard import POLICIES, run  # noqa: E402


def one(args):
    seed, temp, fault_rate = args
    scn = make_scenario(seed, temp_c=temp)
    rows = []
    for p in POLICIES:
        r = run(scn, p, fault_rate=fault_rate, seed=seed)
        rows.append({"seed": seed, "temp_c": temp, "fault_rate": fault_rate, **r.metrics})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--temp", type=float, default=15.0)
    ap.add_argument("--fault-rate", type=float, default=0.0)
    ap.add_argument("--out", default="results/benchmark.csv")
    a = ap.parse_args()
    jobs = [(s, a.temp, a.fault_rate) for s in range(a.seeds)]
    with ProcessPoolExecutor(a.workers) as ex:
        rows = [r for chunk in ex.map(one, jobs) for r in chunk]
    df = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    df.to_csv(a.out, index=False)
    cols = ["total_cost", "peak_kw", "missed", "mean_wait_h", "bay_utilisation",
            "solar_self_consumption", "overcharge_kwh"]
    g = df.groupby("policy")[cols].mean().loc[list(POLICIES)]
    g["cost_vs_fcfs_%"] = (g["total_cost"] / g.loc["fcfs", "total_cost"] - 1) * 100
    print(g.round(2).to_string())
    print(f"\n{a.seeds} depot-days x {len(POLICIES)} policies -> {a.out}")


if __name__ == "__main__":
    main()
