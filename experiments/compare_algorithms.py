#!/usr/bin/env python3

"""
EdgePilot controlled baseline-vs-trend experiment.

Purpose:
Compare two candidate workers:

edge-node-1:
    initially low pressure, then rising CPU pressure.

edge-node-2:
    stable moderate CPU pressure.

The experiment records whether the baseline and
trend-aware heuristics rank the candidates differently.

edge-node-3 is intentionally excluded because this is
a controlled pairwise experiment, not a full-cluster
placement experiment.
"""

import csv
import json
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path


NODE_LABEL = "edgepilot.io/node"

CANDIDATES = [
    "edge-node-1",
    "edge-node-2",
]

HISTORY_SIZE = 5
INTERVAL = 5

RESULTS_FILE = (
    Path.home()
    / "edgepilot"
    / "results"
    / "experiment-algorithm-comparison.csv"
)

history = defaultdict(
    lambda: deque(maxlen=HISTORY_SIZE)
)


def kubectl(args, input_text=None, check=True):

    result = subprocess.run(
        ["kubectl", *args],
        input=input_text,
        capture_output=True,
        text=True,
    )

    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip())

    return result.stdout


def create_stress_pod(
    name,
    logical_node,
    cpu_limit
):

    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",

        "metadata": {
            "name": name,
        },

        "spec": {

            "nodeSelector": {
                NODE_LABEL: logical_node
            },

            "containers": [{
                "name": "stress",

                "image": "busybox:1.37",

                "command": [
                    "/bin/sh",
                    "-c",
                    "while :; do :; done"
                ],

                "resources": {

                    "requests": {
                        "cpu": "50m",
                        "memory": "8Mi"
                    },

                    "limits": {
                        "cpu": cpu_limit,
                        "memory": "16Mi"
                    }
                }
            }],

            "restartPolicy": "Never"
        }
    }

    kubectl(
        ["apply", "-f", "-"],
        input_text=json.dumps(manifest)
    )


def delete_pod(name):

    kubectl([
        "delete",
        "pod",
        name,
        "--ignore-not-found=true"
    ], check=False)


def mapping():

    data = json.loads(
        kubectl([
            "get",
            "nodes",
            "-l",
            NODE_LABEL,
            "-o",
            "json"
        ])
    )

    return {
        item["metadata"]["name"]:
        item["metadata"]["labels"][NODE_LABEL]
        for item in data["items"]
    }


def metrics():

    nodes = mapping()

    raw = kubectl([
        "top",
        "nodes",
        "--no-headers"
    ])

    result = []

    for line in raw.splitlines():

        parts = line.split()

        if len(parts) < 5:
            continue

        real = parts[0]

        if real not in nodes:
            continue

        logical = nodes[real]

        if logical not in CANDIDATES:
            continue

        result.append({
            "node": logical,
            "cpu": float(
                parts[2].rstrip("%")
            ),
            "memory": float(
                parts[4].rstrip("%")
            ),
        })

    return result


def baseline(cpu, memory):

    return (
        0.5 * cpu
        + 0.5 * memory
    )


def calculate_trend(
    node,
    cpu,
    memory
):

    samples = history[node]

    if len(samples) < HISTORY_SIZE:
        return None

    old_cpu, old_memory = samples[0]

    cpu_change = max(
        0,
        cpu - old_cpu
    )

    memory_change = max(
        0,
        memory - old_memory
    )

    return min(
        100,
        0.5 * cpu_change
        + 0.5 * memory_change
    )


def trend_score(
    cpu,
    memory,
    trend
):

    return (
        0.4 * cpu
        + 0.4 * memory
        + 0.2 * trend
    )


def collect(observation, phase, writer):

    current = metrics()

    scored = []

    for row in current:

        base = baseline(
            row["cpu"],
            row["memory"]
        )

        trend = calculate_trend(
            row["node"],
            row["cpu"],
            row["memory"]
        )

        ts = (
            trend_score(
                row["cpu"],
                row["memory"],
                trend
            )
            if trend is not None
            else None
        )

        scored.append({
            **row,
            "baseline": base,
            "trend": trend,
            "trend_score": ts
        })

    base_winner = min(
        scored,
        key=lambda x: x["baseline"]
    )["node"]

    available = [
        x for x in scored
        if x["trend_score"] is not None
    ]

    trend_winner = (
        min(
            available,
            key=lambda x: x["trend_score"]
        )["node"]
        if available
        else ""
    )

    print(
        f"\nObservation {observation:02d}"
        f" | {phase}"
    )

    for row in sorted(
        scored,
        key=lambda x: x["node"]
    ):

        t = (
            f'{row["trend"]:.2f}'
            if row["trend"] is not None
            else "-"
        )

        ts = (
            f'{row["trend_score"]:.2f}'
            if row["trend_score"]
            is not None
            else "-"
        )

        print(
            f'{row["node"]:<12}'
            f' CPU={row["cpu"]:>5.1f}%'
            f' MEM={row["memory"]:>5.1f}%'
            f' BASE={row["baseline"]:>6.2f}'
            f' T={t:>6}'
            f' TREND={ts:>6}'
        )

    print(
        f"Baseline → {base_winner}"
    )

    print(
        "Trend    → "
        + (
            trend_winner
            if trend_winner
            else "warming up"
        )
    )

    if (
        trend_winner
        and base_winner != trend_winner
    ):
        print(
            "*** RANKING DIVERGENCE DETECTED ***"
        )

    timestamp = datetime.now(
        timezone.utc
    ).isoformat()

    for row in scored:

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
                if row["trend_score"]
                is not None
                else ""
            ),
            base_winner,
            trend_winner,
            (
                "yes"
                if trend_winner
                and base_winner != trend_winner
                else "no"
            )
        ])

    for row in scored:

        history[row["node"]].append(
            (
                row["cpu"],
                row["memory"]
            )
        )


def main():

    RESULTS_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    delete_pod(
        "edgepilot-stable-load"
    )

    delete_pod(
        "edgepilot-rising-load"
    )

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
            "divergence",
        ])

        print("=" * 70)
        print(
            "EDGEPILOT — BASELINE VS TREND"
        )
        print("=" * 70)

        print(
            "\nCollecting idle history..."
        )

        for observation in range(1, 6):

            collect(
                observation,
                "IDLE",
                writer
            )

            file.flush()

            time.sleep(INTERVAL)

        print(
            "\n>>> Starting stable load "
            "on edge-node-2 <<<"
        )

        create_stress_pod(
            "edgepilot-stable-load",
            "edge-node-2",
            "400m"
        )

        time.sleep(20)

        collect(
            6,
            "STABLE LOAD",
            writer
        )

        file.flush()

        print(
            "\n>>> Starting rising load "
            "on edge-node-1 <<<"
        )

        create_stress_pod(
            "edgepilot-rising-load",
            "edge-node-1",
            "300m"
        )

        for observation in range(
            7,
            13
        ):

            time.sleep(INTERVAL)

            collect(
                observation,
                "RISING LOAD",
                writer
            )

            file.flush()

    delete_pod(
        "edgepilot-stable-load"
    )

    delete_pod(
        "edgepilot-rising-load"
    )

    print()
    print("=" * 70)
    print("EXPERIMENT COMPLETE")
    print(
        f"Results: {RESULTS_FILE}"
    )
    print("=" * 70)


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print(
            "\nInterrupted. Cleaning up..."
        )

        delete_pod(
            "edgepilot-stable-load"
        )

        delete_pod(
            "edgepilot-rising-load"
        )

    except Exception:

        delete_pod(
            "edgepilot-stable-load"
        )

        delete_pod(
            "edgepilot-rising-load"
        )

        raise
