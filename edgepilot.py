#!/usr/bin/env python3

import csv
import json
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------
# EdgePilot configuration
# ---------------------------------------------------------

NODE_LABEL = "edgepilot.io/node"

CPU_WEIGHT_BASELINE = 0.5
MEM_WEIGHT_BASELINE = 0.5

CPU_WEIGHT_TREND = 0.4
MEM_WEIGHT_TREND = 0.4
TREND_WEIGHT = 0.2

HISTORY_SIZE = 5

RESULTS_DIR = Path.home() / "edgepilot" / "results"
RESULTS_FILE = RESULTS_DIR / "edgepilot-timeseries.csv"

history = defaultdict(lambda: deque(maxlen=HISTORY_SIZE))


# ---------------------------------------------------------
# Kubernetes helpers
# ---------------------------------------------------------

def run_kubectl(args):
    result = subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def get_worker_mapping():
    """
    Returns:
        {
            "k3d-edgepilot-agent-0": "edge-node-1",
            ...
        }
    """

    raw = run_kubectl([
        "get",
        "nodes",
        "-l",
        NODE_LABEL,
        "-o",
        "json",
    ])

    data = json.loads(raw)

    mapping = {}

    for item in data["items"]:
        real_name = item["metadata"]["name"]
        logical_name = item["metadata"]["labels"][NODE_LABEL]

        mapping[real_name] = logical_name

    return mapping


def get_metrics():
    """
    Uses kubectl top nodes and returns CPU/memory percentages
    for EdgePilot-labelled worker nodes.
    """

    mapping = get_worker_mapping()

    raw = run_kubectl([
        "top",
        "nodes",
        "--no-headers",
    ])

    metrics = []

    for line in raw.strip().splitlines():
        columns = line.split()

        if len(columns) < 5:
            continue

        real_name = columns[0]

        if real_name not in mapping:
            continue

        cpu_percent = float(columns[2].rstrip("%"))
        memory_percent = float(columns[4].rstrip("%"))

        metrics.append({
            "real_name": real_name,
            "node": mapping[real_name],
            "cpu": cpu_percent,
            "memory": memory_percent,
        })

    return metrics


# ---------------------------------------------------------
# EdgePilot scoring
# ---------------------------------------------------------

def baseline_score(cpu, memory):
    return (
        CPU_WEIGHT_BASELINE * cpu
        + MEM_WEIGHT_BASELINE * memory
    )


def trend_component(node, cpu, memory):
    samples = history[node]

    if len(samples) < HISTORY_SIZE:
        return None

    oldest_cpu, oldest_memory = samples[0]

    cpu_increase = max(0.0, cpu - oldest_cpu)
    memory_increase = max(0.0, memory - oldest_memory)

    trend = (
        0.5 * cpu_increase
        + 0.5 * memory_increase
    )

    return min(100.0, trend)


def trend_score(cpu, memory, trend):
    return (
        CPU_WEIGHT_TREND * cpu
        + MEM_WEIGHT_TREND * memory
        + TREND_WEIGHT * trend
    )


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

def initialise_results_file():

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if not RESULTS_FILE.exists():
        with RESULTS_FILE.open("w", newline="") as file:
            writer = csv.writer(file)

            writer.writerow([
                "timestamp",
                "node",
                "cpu_percent",
                "memory_percent",
                "baseline_score",
                "trend_component",
                "trend_score",
            ])


def log_result(timestamp, row):

    with RESULTS_FILE.open("a", newline="") as file:

        writer = csv.writer(file)

        writer.writerow([
            timestamp,
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
        ])


# ---------------------------------------------------------
# Analysis
# ---------------------------------------------------------

def analyse_once():

    metrics = get_metrics()

    if not metrics:
        raise RuntimeError(
            "No EdgePilot-labelled worker metrics were found."
        )

    timestamp = datetime.now(timezone.utc).isoformat()

    rows = []

    # Calculate scores BEFORE adding current sample.
    # Therefore history[0] represents an earlier observation.
    for metric in metrics:

        node = metric["node"]
        cpu = metric["cpu"]
        memory = metric["memory"]

        base = baseline_score(cpu, memory)

        trend = trend_component(
            node,
            cpu,
            memory,
        )

        if trend is not None:
            t_score = trend_score(
                cpu,
                memory,
                trend,
            )
        else:
            t_score = None

        row = {
            **metric,
            "baseline": base,
            "trend": trend,
            "trend_score": t_score,
        }

        rows.append(row)

    # Store current observations after scoring.
    for row in rows:
        history[row["node"]].append(
            (row["cpu"], row["memory"])
        )

    for row in rows:
        log_result(timestamp, row)

    return rows


# ---------------------------------------------------------
# Display
# ---------------------------------------------------------

def display(rows):

    print()
    print("=" * 78)
    print("EDGEPILOT — RESOURCE PRESSURE ANALYSIS")
    print("=" * 78)

    print(
        f'{"NODE":<14}'
        f'{"CPU":>8}'
        f'{"MEM":>8}'
        f'{"BASE":>10}'
        f'{"TREND":>12}'
        f'{"TREND SCORE":>15}'
    )

    print("-" * 78)

    for row in sorted(
        rows,
        key=lambda x: x["node"]
    ):

        trend = (
            f'{row["trend"]:.2f}'
            if row["trend"] is not None
            else "warming up"
        )

        trend_score_value = (
            f'{row["trend_score"]:.2f}'
            if row["trend_score"] is not None
            else "warming up"
        )

        print(
            f'{row["node"]:<14}'
            f'{row["cpu"]:>7.1f}%'
            f'{row["memory"]:>7.1f}%'
            f'{row["baseline"]:>10.2f}'
            f'{trend:>12}'
            f'{trend_score_value:>15}'
        )

    print("-" * 78)

    baseline_winner = min(
        rows,
        key=lambda x: x["baseline"]
    )

    print(
        f'Baseline recommendation : '
        f'{baseline_winner["node"]} '
        f'(score {baseline_winner["baseline"]:.2f})'
    )

    trend_rows = [
        row
        for row in rows
        if row["trend_score"] is not None
    ]

    if trend_rows:

        trend_winner = min(
            trend_rows,
            key=lambda x: x["trend_score"]
        )

        print(
            f'Trend recommendation    : '
            f'{trend_winner["node"]} '
            f'(score {trend_winner["trend_score"]:.2f})'
        )

    else:

        print(
            "Trend recommendation    : "
            f"warming up ({HISTORY_SIZE} observations required)"
        )

    print()
    print(
        "Recommendation is advisory only; "
        "no Kubernetes scheduling decision was changed."
    )

    print(
        f"Results: {RESULTS_FILE}"
    )

    print("=" * 78)


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():

    initialise_results_file()

    print("EdgePilot starting...")
    print(
        f"Collecting {HISTORY_SIZE + 1} observations "
        "at 5-second intervals."
    )

    for sample in range(1, HISTORY_SIZE + 2):

        print(
            f"\nObservation "
            f"{sample}/{HISTORY_SIZE + 1}"
        )

        rows = analyse_once()

        display(rows)

        if sample < HISTORY_SIZE + 1:
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()

    except subprocess.CalledProcessError as error:

        print("kubectl command failed.")

        if error.stderr:
            print(error.stderr)

        raise SystemExit(1)

    except KeyboardInterrupt:

        print("\nEdgePilot stopped.")

        raise SystemExit(0)
