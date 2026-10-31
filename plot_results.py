#!/usr/bin/env python3

"""
EdgePilot Experimental Results Visualisation

Generates figures directly from the CSV files produced by
the EdgePilot experiments.

Input:
    results/experiment-dynamic-load.csv
    results/experiment-algorithm-comparison.csv

Output:
    results/figures/dynamic-cpu-pressure.png
    results/figures/dynamic-scores.png
    results/figures/trend-component.png
    results/figures/algorithm-comparison.png
    results/experiment-summary.txt
"""

import csv
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path.home() / "edgepilot"
RESULTS = ROOT / "results"
FIGURES = RESULTS / "figures"

DYNAMIC_FILE = RESULTS / "experiment-dynamic-load.csv"

COMPARISON_FILE = (
    RESULTS
    / "experiment-algorithm-comparison.csv"
)

SUMMARY_FILE = RESULTS / "experiment-summary.txt"


FIGURES.mkdir(
    parents=True,
    exist_ok=True
)


# ---------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------

def read_csv(path):

    with path.open(
        newline="",
        encoding="utf-8"
    ) as file:

        return list(
            csv.DictReader(file)
        )


def as_float(value):

    if value is None:
        return None

    value = value.strip()

    if value == "":
        return None

    return float(value)


# ---------------------------------------------------------
# Load experimental data
# ---------------------------------------------------------

dynamic = read_csv(DYNAMIC_FILE)

comparison = read_csv(
    COMPARISON_FILE
)


# ---------------------------------------------------------
# Organise rows by node
# ---------------------------------------------------------

def group_by_node(rows):

    grouped = defaultdict(list)

    for row in rows:
        grouped[row["node"]].append(row)

    for node in grouped:

        grouped[node].sort(
            key=lambda row:
                int(row["observation"])
        )

    return grouped


dynamic_nodes = group_by_node(dynamic)

comparison_nodes = group_by_node(
    comparison
)


# ---------------------------------------------------------
# Phase markers
# ---------------------------------------------------------

def add_dynamic_phase_markers():

    # Stress starts at observation 6.
    plt.axvline(
        5.5,
        linestyle="--",
        linewidth=1
    )

    # Recovery begins at observation 11.
    plt.axvline(
        10.5,
        linestyle="--",
        linewidth=1
    )

    top = plt.ylim()[1]

    plt.text(
        5.65,
        top * 0.94,
        "CPU stress starts",
        fontsize=9
    )

    plt.text(
        10.65,
        top * 0.94,
        "Stress removed",
        fontsize=9
    )


# ---------------------------------------------------------
# Figure 1:
# Dynamic CPU pressure
# ---------------------------------------------------------

plt.figure(
    figsize=(10, 6)
)

for node in sorted(dynamic_nodes):

    rows = dynamic_nodes[node]

    observations = [
        int(row["observation"])
        for row in rows
    ]

    cpu = [
        as_float(row["cpu_percent"])
        for row in rows
    ]

    plt.plot(
        observations,
        cpu,
        marker="o",
        label=node
    )


plt.xlabel("Observation")

plt.ylabel(
    "CPU Utilisation (%)"
)

plt.title(
    "EdgePilot Dynamic Load Experiment — Worker CPU Pressure"
)

plt.xticks(
    range(1, 15)
)

plt.grid(
    alpha=0.25
)

plt.legend()

add_dynamic_phase_markers()

plt.tight_layout()

plt.savefig(
    FIGURES
    / "dynamic-cpu-pressure.png",
    dpi=200
)

plt.close()


# ---------------------------------------------------------
# Figure 2:
# Baseline vs trend-aware score for stressed node
# ---------------------------------------------------------

node1 = dynamic_nodes[
    "edge-node-1"
]

observations = [
    int(row["observation"])
    for row in node1
]

baseline_scores = [
    as_float(row["baseline_score"])
    for row in node1
]

trend_observations = []

trend_scores = []

for row in node1:

    score = as_float(
        row["trend_score"]
    )

    if score is not None:

        trend_observations.append(
            int(row["observation"])
        )

        trend_scores.append(score)


plt.figure(
    figsize=(10, 6)
)

plt.plot(
    observations,
    baseline_scores,
    marker="o",
    label="Baseline score"
)

plt.plot(
    trend_observations,
    trend_scores,
    marker="s",
    label="Trend-aware score"
)

plt.xlabel("Observation")

plt.ylabel(
    "Resource-Pressure Score"
)

plt.title(
    "Stressed Worker — Baseline and Trend-Aware Scores"
)

plt.xticks(
    range(1, 15)
)

plt.grid(
    alpha=0.25
)

plt.legend()

add_dynamic_phase_markers()

plt.tight_layout()

plt.savefig(
    FIGURES
    / "dynamic-scores.png",
    dpi=200
)

plt.close()


# ---------------------------------------------------------
# Figure 3:
# Trend component
# ---------------------------------------------------------

plt.figure(
    figsize=(10, 6)
)

for node in sorted(dynamic_nodes):

    observations = []

    trends = []

    for row in dynamic_nodes[node]:

        trend = as_float(
            row["trend_component"]
        )

        if trend is None:
            continue

        observations.append(
            int(row["observation"])
        )

        trends.append(trend)

    plt.plot(
        observations,
        trends,
        marker="o",
        label=node
    )


plt.xlabel("Observation")

plt.ylabel(
    "Trend Component (T)"
)

plt.title(
    "EdgePilot Trend Component During Dynamic Resource Pressure"
)

plt.xticks(
    range(1, 15)
)

plt.grid(
    alpha=0.25
)

plt.legend()

add_dynamic_phase_markers()

plt.tight_layout()

plt.savefig(
    FIGURES
    / "trend-component.png",
    dpi=200
)

plt.close()


# ---------------------------------------------------------
# Figure 4:
# Algorithm comparison
#
# Instead of merely plotting four score lines,
# calculate the preference margin:
#
# score(node-2) - score(node-1)
#
# Positive:
# node-1 is preferred.
#
# Smaller positive value:
# node-1's advantage has narrowed.
#
# Negative:
# node-2 is preferred.
# ---------------------------------------------------------

comparison_by_observation = defaultdict(
    dict
)

for row in comparison:

    observation = int(
        row["observation"]
    )

    comparison_by_observation[
        observation
    ][row["node"]] = row


margin_observations = []

baseline_margins = []

trend_margin_observations = []

trend_margins = []


for observation in sorted(
    comparison_by_observation
):

    nodes = comparison_by_observation[
        observation
    ]

    if (
        "edge-node-1" not in nodes
        or "edge-node-2" not in nodes
    ):
        continue

    node1_row = nodes[
        "edge-node-1"
    ]

    node2_row = nodes[
        "edge-node-2"
    ]

    base1 = as_float(
        node1_row["baseline_score"]
    )

    base2 = as_float(
        node2_row["baseline_score"]
    )

    margin_observations.append(
        observation
    )

    baseline_margins.append(
        base2 - base1
    )

    trend1 = as_float(
        node1_row["trend_score"]
    )

    trend2 = as_float(
        node2_row["trend_score"]
    )

    if (
        trend1 is not None
        and trend2 is not None
    ):

        trend_margin_observations.append(
            observation
        )

        trend_margins.append(
            trend2 - trend1
        )


plt.figure(
    figsize=(10, 6)
)

plt.axhline(
    0,
    linewidth=1
)

plt.plot(
    margin_observations,
    baseline_margins,
    marker="o",
    label="Baseline preference margin"
)

plt.plot(
    trend_margin_observations,
    trend_margins,
    marker="s",
    label="Trend-aware preference margin"
)

plt.xlabel("Observation")

plt.ylabel(
    "Node-2 Score − Node-1 Score"
)

plt.title(
    "Baseline vs Trend-Aware Candidate Preference"
)

plt.xticks(
    range(1, 13)
)

plt.grid(
    alpha=0.25
)

plt.legend()

plt.tight_layout()

plt.savefig(
    FIGURES
    / "algorithm-comparison.png",
    dpi=200
)

plt.close()


# ---------------------------------------------------------
# Quantitative summary
# ---------------------------------------------------------

max_cpu_row = max(
    dynamic,
    key=lambda row:
        as_float(
            row["cpu_percent"]
        )
)

max_cpu = as_float(
    max_cpu_row["cpu_percent"]
)


trend_rows = [
    row
    for row in dynamic
    if as_float(
        row["trend_component"]
    ) is not None
]

max_trend_row = max(
    trend_rows,
    key=lambda row:
        as_float(
            row["trend_component"]
        )
)

max_trend = as_float(
    max_trend_row[
        "trend_component"
    ]
)


# Count recommendation divergence in the
# controlled comparison experiment.

divergence_observations = set()

for row in comparison:

    if (
        row.get(
            "divergence",
            ""
        ).lower()
        == "yes"
    ):
        divergence_observations.add(
            int(
                row["observation"]
            )
        )


# Find the smallest positive comparison margins
# after trend-aware scores become available.

paired_margins = []

for observation in sorted(
    comparison_by_observation
):

    nodes = comparison_by_observation[
        observation
    ]

    if (
        "edge-node-1" not in nodes
        or "edge-node-2" not in nodes
    ):
        continue

    n1 = nodes["edge-node-1"]
    n2 = nodes["edge-node-2"]

    b1 = as_float(
        n1["baseline_score"]
    )

    b2 = as_float(
        n2["baseline_score"]
    )

    t1 = as_float(
        n1["trend_score"]
    )

    t2 = as_float(
        n2["trend_score"]
    )

    if (
        t1 is None
        or t2 is None
    ):
        continue

    paired_margins.append({
        "observation": observation,
        "baseline_margin":
            b2 - b1,
        "trend_margin":
            t2 - t1
    })


# Find the observation where trend awareness
# reduced node-1's baseline preference the most.

best_reduction = None

for item in paired_margins:

    reduction = (
        item["baseline_margin"]
        - item["trend_margin"]
    )

    if (
        best_reduction is None
        or reduction
        > best_reduction["reduction"]
    ):

        best_reduction = {
            **item,
            "reduction": reduction
        }


with SUMMARY_FILE.open(
    "w",
    encoding="utf-8"
) as file:

    file.write(
        "EDGEPILOT EXPERIMENTAL SUMMARY\n"
    )

    file.write(
        "=" * 50 + "\n\n"
    )

    file.write(
        "Dynamic Load Experiment\n"
    )

    file.write(
        "-" * 50 + "\n"
    )

    file.write(
        f"Maximum observed worker CPU: "
        f"{max_cpu:.1f}% "
        f"({max_cpu_row['node']}, "
        f"observation "
        f"{max_cpu_row['observation']})\n"
    )

    file.write(
        f"Maximum trend component: "
        f"{max_trend:.2f} "
        f"({max_trend_row['node']}, "
        f"observation "
        f"{max_trend_row['observation']})\n"
    )

    file.write(
        "\nAlgorithm Comparison Experiment\n"
    )

    file.write(
        "-" * 50 + "\n"
    )

    file.write(
        "Recommendation divergences: "
        f"{len(divergence_observations)}\n"
    )

    if best_reduction:

        file.write(
            "Largest reduction in "
            "node-1 preference margin:\n"
        )

        file.write(
            f"  Observation: "
            f"{best_reduction['observation']}\n"
        )

        file.write(
            f"  Baseline margin: "
            f"{best_reduction['baseline_margin']:.2f}\n"
        )

        file.write(
            f"  Trend-aware margin: "
            f"{best_reduction['trend_margin']:.2f}\n"
        )

        file.write(
            f"  Margin reduction: "
            f"{best_reduction['reduction']:.2f}\n"
        )

    file.write(
        "\nInterpretation\n"
    )

    file.write(
        "-" * 50 + "\n"
    )

    file.write(
        "The baseline heuristic reacted to "
        "current resource pressure and avoided "
        "the strongly loaded worker.\n"
    )

    file.write(
        "The trend-aware heuristic detected "
        "positive short-term pressure growth.\n"
    )

    if divergence_observations:

        file.write(
            "At least one observation produced "
            "different baseline and trend-aware "
            "recommendations.\n"
        )

    else:

        file.write(
            "No recommendation divergence was "
            "observed in the controlled comparison "
            "experiment.\n"
        )

        file.write(
            "Trend awareness changed relative "
            "candidate preference, but was not "
            "strong enough to reverse the final "
            "recommendation under this workload.\n"
        )


# ---------------------------------------------------------
# Console output
# ---------------------------------------------------------

print()
print("=" * 60)
print("EDGEPILOT RESULTS GENERATED")
print("=" * 60)

print(
    f"Maximum CPU observed: "
    f"{max_cpu:.1f}%"
)

print(
    f"Maximum trend component: "
    f"{max_trend:.2f}"
)

print(
    "Recommendation divergences: "
    f"{len(divergence_observations)}"
)

if best_reduction:

    print(
        "Largest preference-margin "
        "reduction:"
    )

    print(
        f"  Observation "
        f"{best_reduction['observation']}"
    )

    print(
        f"  Baseline: "
        f"{best_reduction['baseline_margin']:.2f}"
    )

    print(
        f"  Trend-aware: "
        f"{best_reduction['trend_margin']:.2f}"
    )

print()

print(
    f"Figures saved to: {FIGURES}"
)

print(
    f"Summary saved to: {SUMMARY_FILE}"
)

print("=" * 60)
