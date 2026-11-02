#!/usr/bin/env python3

"""
EdgePilot preliminary repeated-trial experiment.

Runs the existing controlled baseline-vs-trend comparison
five times without modifying its experimental logic.

Each trial's raw CSV is preserved independently. Aggregate
statistics are then calculated across the repeated trials.

This is intended as preliminary Review-3 evidence, not as
a statistically powered final research evaluation.
"""

import csv
import shutil
import subprocess
import time
from pathlib import Path
from statistics import mean, median, stdev


PROJECT = Path.home() / "edgepilot"
RESULTS = PROJECT / "results"
TRIAL_DIR = RESULTS / "repeated-trials"

SOURCE_RESULT = (
    RESULTS / "experiment-algorithm-comparison.csv"
)

TRIAL_COUNT = 5
COOLDOWN_SECONDS = 30


def run_command(args):
    result = subprocess.run(
        args,
        cwd=PROJECT,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(args)}"
        )


def load_trial(path):
    rows = []

    with path.open(newline="") as file:
        reader = csv.DictReader(file)

        for row in reader:
            rows.append(row)

    return rows


def analyse_trial(trial_number, path):
    rows = load_trial(path)

    divergences = {
        int(row["observation"])
        for row in rows
        if row["divergence"] == "yes"
    }

    by_observation = {}

    for row in rows:
        obs = int(row["observation"])

        by_observation.setdefault(
            obs, {}
        )[row["node"]] = row

    reductions = []

    peak_trend = 0.0
    peak_cpu = 0.0

    for row in rows:
        peak_cpu = max(
            peak_cpu,
            float(row["cpu_percent"])
        )

        if row["trend_component"]:
            peak_trend = max(
                peak_trend,
                float(row["trend_component"])
            )

    for obs, nodes in by_observation.items():
        if (
            "edge-node-1" not in nodes
            or "edge-node-2" not in nodes
        ):
            continue

        n1 = nodes["edge-node-1"]
        n2 = nodes["edge-node-2"]

        if (
            not n1["trend_score"]
            or not n2["trend_score"]
        ):
            continue

        baseline_margin = (
            float(n2["baseline_score"])
            - float(n1["baseline_score"])
        )

        trend_margin = (
            float(n2["trend_score"])
            - float(n1["trend_score"])
        )

        reduction = (
            baseline_margin - trend_margin
        )

        reductions.append(
            (
                reduction,
                obs,
                baseline_margin,
                trend_margin,
            )
        )

    if reductions:
        best = max(
            reductions,
            key=lambda x: x[0]
        )

        (
            max_reduction,
            best_obs,
            baseline_margin,
            trend_margin,
        ) = best
    else:
        max_reduction = 0.0
        best_obs = 0
        baseline_margin = 0.0
        trend_margin = 0.0

    return {
        "trial": trial_number,
        "peak_cpu": round(peak_cpu, 2),
        "peak_trend": round(peak_trend, 2),
        "divergences": len(divergences),
        "best_observation": best_obs,
        "baseline_margin": round(baseline_margin, 2),
        "trend_margin": round(trend_margin, 2),
        "margin_reduction": round(max_reduction, 2),
    }


def write_summary(results):
    csv_path = (
        RESULTS
        / "repeated-trials-summary.csv"
    )

    txt_path = (
        RESULTS
        / "repeated-trials-summary.txt"
    )

    fields = [
        "trial",
        "peak_cpu",
        "peak_trend",
        "divergences",
        "best_observation",
        "baseline_margin",
        "trend_margin",
        "margin_reduction",
    ]

    with csv_path.open(
        "w",
        newline=""
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        writer.writeheader()
        writer.writerows(results)

    peak_cpus = [
        r["peak_cpu"] for r in results
    ]

    peak_trends = [
        r["peak_trend"] for r in results
    ]

    reductions = [
        r["margin_reduction"]
        for r in results
    ]

    total_divergences = sum(
        r["divergences"]
        for r in results
    )

    with txt_path.open("w") as file:
        file.write(
            "EDGEPILOT PRELIMINARY "
            "REPEATED-TRIAL SUMMARY\n"
        )

        file.write("=" * 55 + "\n\n")

        file.write(
            f"Trials completed: {len(results)}\n"
        )

        file.write(
            "Total recommendation "
            f"divergences: {total_divergences}\n\n"
        )

        file.write(
            "Peak CPU across trials\n"
        )

        file.write(
            f"  Mean: {mean(peak_cpus):.2f}%\n"
        )

        file.write(
            f"  Median: {median(peak_cpus):.2f}%\n"
        )

        if len(peak_cpus) > 1:
            file.write(
                "  Sample standard deviation: "
                f"{stdev(peak_cpus):.2f}\n"
            )

        file.write(
            "\nPeak trend component "
            "across trials\n"
        )

        file.write(
            f"  Mean: {mean(peak_trends):.2f}\n"
        )

        file.write(
            f"  Median: {median(peak_trends):.2f}\n"
        )

        if len(peak_trends) > 1:
            file.write(
                "  Sample standard deviation: "
                f"{stdev(peak_trends):.2f}\n"
            )

        file.write(
            "\nMaximum within-trial "
            "preference-margin reduction\n"
        )

        file.write(
            f"  Mean: {mean(reductions):.2f}\n"
        )

        file.write(
            f"  Median: {median(reductions):.2f}\n"
        )

        if len(reductions) > 1:
            file.write(
                "  Sample standard deviation: "
                f"{stdev(reductions):.2f}\n"
            )

        file.write(
            "\nInterpretation\n"
        )

        file.write("-" * 55 + "\n")

        file.write(
            "These five runs are a preliminary "
            "repeatability check for Review 3.\n"
        )

        file.write(
            "They are not treated as a statistically "
            "powered final evaluation.\n"
        )

        file.write(
            "The final research phase should use more "
            "trials, workloads, baselines, and "
            "application-level outcome metrics.\n"
        )

    return csv_path, txt_path


def main():
    TRIAL_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    results = []

    print("=" * 70)
    print(
        "EDGEPILOT PRELIMINARY "
        "REPEATED-TRIAL STUDY"
    )
    print("=" * 70)

    for trial in range(
        1,
        TRIAL_COUNT + 1
    ):
        print()
        print("#" * 70)
        print(
            f"STARTING TRIAL "
            f"{trial}/{TRIAL_COUNT}"
        )
        print("#" * 70)

        run_command([
            "python3",
            "experiments/compare_algorithms.py",
        ])

        destination = (
            TRIAL_DIR
            / f"trial-{trial}.csv"
        )

        shutil.copy2(
            SOURCE_RESULT,
            destination
        )

        result = analyse_trial(
            trial,
            destination
        )

        results.append(result)

        print()
        print(
            f"Trial {trial} preserved: "
            f"{destination}"
        )

        print(
            f"Peak CPU: "
            f"{result['peak_cpu']:.1f}%"
        )

        print(
            f"Peak trend: "
            f"{result['peak_trend']:.2f}"
        )

        print(
            "Recommendation divergences: "
            f"{result['divergences']}"
        )

        print(
            "Largest margin reduction: "
            f"{result['margin_reduction']:.2f}"
        )

        if trial < TRIAL_COUNT:
            print()
            print(
                "Cooling down for "
                f"{COOLDOWN_SECONDS}s..."
            )

            time.sleep(
                COOLDOWN_SECONDS
            )

    csv_path, txt_path = (
        write_summary(results)
    )

    print()
    print("=" * 70)
    print(
        "REPEATED-TRIAL STUDY COMPLETE"
    )
    print("=" * 70)

    print(
        f"Trial data: {TRIAL_DIR}"
    )

    print(
        f"Summary CSV: {csv_path}"
    )

    print(
        f"Summary text: {txt_path}"
    )


if __name__ == "__main__":
    main()
