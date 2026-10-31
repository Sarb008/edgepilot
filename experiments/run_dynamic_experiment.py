#!/usr/bin/env python3

import csv
import json
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path


TARGET_NODE = "edge-node-1"
NODE_LABEL = "edgepilot.io/node"

INTERVAL = 5
TOTAL_OBSERVATIONS = 14

# Start stress after five clean observations.
STRESS_START_OBSERVATION = 6

# Remove stress after observation 10.
STRESS_STOP_OBSERVATION = 11

HISTORY_SIZE = 5

RESULTS_FILE = (
    Path.home()
    / "edgepilot"
    / "results"
    / "experiment-dynamic-load.csv"
)

STRESS_MANIFEST = (
    Path.home()
    / "edgepilot"
    / "k8s"
    / "cpu-stress.yaml"
)

history = defaultdict(
    lambda: deque(maxlen=HISTORY_SIZE)
)


def kubectl(args, check=True):
    result = subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
    )

    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip())

    return result.stdout


def worker_mapping():
    raw = kubectl([
        "get",
        "nodes",
        "-l",
        NODE_LABEL,
        "-o",
        "json",
    ])

    data = json.loads(raw)

    return {
        item["metadata"]["name"]:
        item["metadata"]["labels"][NODE_LABEL]
        for item in data["items"]
    }


def collect_metrics():
    mapping = worker_mapping()

    raw = kubectl([
        "top",
        "nodes",
        "--no-headers",
    ])

    rows = []

    for line in raw.strip().splitlines():
        parts = line.split()

        if len(parts) < 5:
            continue

        real_name = parts[0]

        if real_name not in mapping:
            continue

        rows.append({
            "node": mapping[real_name],
            "cpu": float(parts[2].rstrip("%")),
            "memory": float(parts[4].rstrip("%")),
        })

    return rows


def baseline(cpu, memory):
    return 0.5 * cpu + 0.5 * memory


def trend_component(node, cpu, memory):
    samples = history[node]

    if len(samples) < HISTORY_SIZE:
        return None

    old_cpu, old_memory = samples[0]

    cpu_delta = max(0.0, cpu - old_cpu)
    memory_delta = max(0.0, memory - old_memory)

    return min(
        100.0,
        0.5 * cpu_delta
        + 0.5 * memory_delta
    )


def trend_score(cpu, memory, trend):
    return (
        0.4 * cpu
        + 0.4 * memory
        + 0.2 * trend
    )


def start_stress():
    print(
        f"\n>>> STARTING CPU PRESSURE ON {TARGET_NODE} <<<\n"
    )

    # Ensure an old pod cannot interfere.
    kubectl(
        ["delete", "pod", "edgepilot-cpu-stress",
         "--ignore-not-found=true"],
        check=False,
    )

    kubectl([
        "apply",
        "-f",
        str(STRESS_MANIFEST),
    ])


def stop_stress():
    print(
        f"\n>>> REMOVING CPU PRESSURE FROM {TARGET_NODE} <<<\n"
    )

    kubectl([
        "delete",
        "pod",
        "edgepilot-cpu-stress",
        "--ignore-not-found=true",
    ], check=False)


def phase_for(observation):
    if observation < STRESS_START_OBSERVATION:
        return "idle"

    if observation < STRESS_STOP_OBSERVATION:
        return "stress"

    return "recovery"


def main():

    RESULTS_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    # Start every experiment from a clean stress state.
    stop_stress()

    with RESULTS_FILE.open(
        "w",
        newline=""
    ) as file:

        writer = csv.writer(file)

        writer.writerow([
            "timestamp",
            "observation",
            "phase",
            "node",
            "cpu_percent",
            "memory_percent",
            "baseline_score",
            "trend_component",
            "trend_score",
            "baseline_recommended",
            "trend_recommended",
        ])

        print("=" * 72)
        print("EDGEPILOT DYNAMIC LOAD EXPERIMENT")
        print("=" * 72)

        for observation in range(
            1,
            TOTAL_OBSERVATIONS + 1
        ):

            if observation == STRESS_START_OBSERVATION:
                start_stress()

                # Give Kubernetes time to start the pod.
                time.sleep(3)

            if observation == STRESS_STOP_OBSERVATION:
                stop_stress()

                # Give the metrics pipeline time to observe recovery.
                time.sleep(3)

            phase = phase_for(observation)

            metrics = collect_metrics()

            timestamp = datetime.now(
                timezone.utc
            ).isoformat()

            scored = []

            for metric in metrics:

                node = metric["node"]
                cpu = metric["cpu"]
                memory = metric["memory"]

                base = baseline(cpu, memory)

                trend = trend_component(
                    node,
                    cpu,
                    memory,
                )

                tscore = (
                    trend_score(
                        cpu,
                        memory,
                        trend
                    )
                    if trend is not None
                    else None
                )

                scored.append({
                    **metric,
                    "baseline": base,
                    "trend": trend,
                    "trend_score": tscore,
                })

            baseline_winner = min(
                scored,
                key=lambda row: row["baseline"]
            )["node"]

            trend_candidates = [
                row for row in scored
                if row["trend_score"] is not None
            ]

            trend_winner = (
                min(
                    trend_candidates,
                    key=lambda row: row["trend_score"]
                )["node"]
                if trend_candidates
                else ""
            )

            print(
                f"\nObservation {observation:02d} "
                f"| Phase: {phase.upper()}"
            )

            for row in sorted(
                scored,
                key=lambda item: item["node"]
            ):

                trend_text = (
                    f'{row["trend"]:.2f}'
                    if row["trend"] is not None
                    else "-"
                )

                score_text = (
                    f'{row["trend_score"]:.2f}'
                    if row["trend_score"] is not None
                    else "-"
                )

                print(
                    f'{row["node"]:<12} '
                    f'CPU={row["cpu"]:>5.1f}% '
                    f'MEM={row["memory"]:>5.1f}% '
                    f'BASE={row["baseline"]:>6.2f} '
                    f'T={trend_text:>6} '
                    f'TREND={score_text:>6}'
                )

                writer.writerow([
                    timestamp,
                    observation,
                    phase,
                    row["node"],
                    row["cpu"],
                    row["memory"],
                    f'{row["baseline"]:.2f}',
                    (
                        f'{row["trend"]:.2f}'
                        if row["trend"] is not None
                        else ""
                    ),
                    (
                        f'{row["trend_score"]:.2f}'
                        if row["trend_score"] is not None
                        else ""
                    ),
                    baseline_winner,
                    trend_winner,
                ])

            file.flush()

            print(
                f"Baseline recommendation: "
                f"{baseline_winner}"
            )

            print(
                "Trend recommendation:    "
                + (
                    trend_winner
                    if trend_winner
                    else "warming up"
                )
            )

            # Current sample becomes history only AFTER scoring.
            for row in scored:
                history[row["node"]].append(
                    (
                        row["cpu"],
                        row["memory"]
                    )
                )

            if observation < TOTAL_OBSERVATIONS:
                time.sleep(INTERVAL)

    # Safety cleanup even if experiment completed normally.
    stop_stress()

    print("\n" + "=" * 72)
    print("EXPERIMENT COMPLETE")
    print(f"Results saved to: {RESULTS_FILE}")
    print("=" * 72)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print("\nExperiment interrupted — cleaning up.")
        stop_stress()

    except Exception as error:
        print(f"\nExperiment failed: {error}")
        stop_stress()
        raise
