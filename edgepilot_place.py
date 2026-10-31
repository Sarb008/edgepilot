#!/usr/bin/env python3

"""
EdgePilot Workload Placement Prototype

This program:
1. Reads live CPU and memory metrics from Kubernetes workers.
2. Calculates the EdgePilot baseline resource-pressure score.
3. Selects the worker with the lowest score.
4. Creates a Kubernetes workload constrained to that worker.
5. Waits for the workload to become Ready.
6. Verifies the node on which Kubernetes actually placed the workload.

EdgePilot selects the target worker and injects a nodeSelector.
Kubernetes remains responsible for the final scheduling/binding and
pod lifecycle management.
"""

import json
import subprocess
import sys


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

NODE_LABEL = "edgepilot.io/node"

WORKLOAD_NAME = "edgepilot-demo"


# ---------------------------------------------------------
# kubectl helper
# ---------------------------------------------------------

def kubectl(args, input_text=None):
    """
    Execute a kubectl command.

    Raises RuntimeError if the command fails.
    """

    result = subprocess.run(
        ["kubectl", *args],
        input=input_text,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
        )

    return result.stdout


# ---------------------------------------------------------
# Discover EdgePilot workers
# ---------------------------------------------------------

def get_workers():
    """
    Return a mapping:

        real Kubernetes node name
            ->
        EdgePilot logical node name

    Example:

        k3d-edgepilot-agent-2
            ->
        edge-node-3
    """

    raw = kubectl([
        "get",
        "nodes",
        "-l",
        NODE_LABEL,
        "-o",
        "json",
    ])

    data = json.loads(raw)

    workers = {}

    for item in data["items"]:

        real_name = (
            item["metadata"]["name"]
        )

        logical_name = (
            item["metadata"]
            ["labels"]
            [NODE_LABEL]
        )

        workers[real_name] = logical_name

    return workers


# ---------------------------------------------------------
# Collect live resource metrics
# ---------------------------------------------------------

def get_metrics(workers):
    """
    Read live Kubernetes node metrics.

    Only workers carrying the EdgePilot node label
    are considered.
    """

    raw = kubectl([
        "top",
        "nodes",
        "--no-headers",
    ])

    results = []

    for line in raw.strip().splitlines():

        parts = line.split()

        if len(parts) < 5:
            continue

        real_name = parts[0]

        # Ignore the control-plane and any
        # non-EdgePilot nodes.
        if real_name not in workers:
            continue

        cpu = float(
            parts[2].rstrip("%")
        )

        memory = float(
            parts[4].rstrip("%")
        )

        # EdgePilot baseline resource-pressure score:
        #
        # R_base = 0.5*C + 0.5*M

        score = (
            0.5 * cpu
            + 0.5 * memory
        )

        results.append({
            "real_name": real_name,
            "logical_name": workers[
                real_name
            ],
            "cpu": cpu,
            "memory": memory,
            "score": score,
        })

    return results


# ---------------------------------------------------------
# EdgePilot node selection
# ---------------------------------------------------------

def choose_node(results):
    """
    Select the worker with the lowest
    resource-pressure score.
    """

    if not results:
        raise RuntimeError(
            "No worker metrics available."
        )

    return min(
        results,
        key=lambda row: row["score"]
    )


# ---------------------------------------------------------
# Create Kubernetes workload
# ---------------------------------------------------------

def create_workload(selected):
    """
    Create a demonstration nginx pod.

    EdgePilot selects the logical worker.

    The selected worker is injected into the
    Kubernetes Pod specification using nodeSelector.

    Kubernetes then performs the actual scheduling
    and binding.
    """

    manifest = {

        "apiVersion": "v1",

        "kind": "Pod",

        "metadata": {

            "name": WORKLOAD_NAME,

            "labels": {
                "app": "edgepilot-demo"
            }
        },

        "spec": {

            "nodeSelector": {
                NODE_LABEL:
                    selected["logical_name"]
            },

            "containers": [

                {
                    "name": "web",

                    "image": "nginx:alpine",

                    "resources": {

                        "requests": {
                            "cpu": "20m",
                            "memory": "16Mi"
                        },

                        "limits": {
                            "cpu": "100m",
                            "memory": "64Mi"
                        }
                    }
                }
            ]
        }
    }

    # Remove an older demonstration pod,
    # if one exists.

    kubectl([
        "delete",
        "pod",
        WORKLOAD_NAME,
        "--ignore-not-found=true",
    ])

    # Submit the generated manifest directly
    # to Kubernetes through stdin.

    kubectl(
        [
            "apply",
            "-f",
            "-"
        ],
        input_text=json.dumps(manifest),
    )


# ---------------------------------------------------------
# Wait for workload
# ---------------------------------------------------------

def wait_for_workload():
    """
    Wait until Kubernetes reports that the
    demonstration pod is Ready.

    This is better than using a fixed sleep because
    container startup time can vary.
    """

    print(
        "Waiting for workload to become ready..."
    )

    try:

        kubectl([
            "wait",
            "--for=condition=Ready",
            f"pod/{WORKLOAD_NAME}",
            "--timeout=60s",
        ])

        return True

    except RuntimeError:

        print(
            "Pod did not become Ready within "
            "60 seconds; checking its "
            "current state."
        )

        return False


# ---------------------------------------------------------
# Retrieve final placement
# ---------------------------------------------------------

def get_workload_status():
    """
    Retrieve the actual Kubernetes node and
    current Pod phase.
    """

    raw = kubectl([
        "get",
        "pod",
        WORKLOAD_NAME,
        "-o",
        "json",
    ])

    pod = json.loads(raw)

    actual_node = (
        pod["spec"].get(
            "nodeName",
            "pending"
        )
    )

    phase = (
        pod["status"].get(
            "phase",
            "Unknown"
        )
    )

    return actual_node, phase


# ---------------------------------------------------------
# Main EdgePilot placement workflow
# ---------------------------------------------------------

def main():

    print("=" * 68)

    print(
        "EDGEPILOT — WORKLOAD PLACEMENT"
    )

    print("=" * 68)

    # -----------------------------------------------------
    # Step 1: Discover workers
    # -----------------------------------------------------

    workers = get_workers()

    if not workers:
        raise RuntimeError(
            "No EdgePilot workers found."
        )

    # -----------------------------------------------------
    # Step 2: Collect live metrics
    # -----------------------------------------------------

    results = get_metrics(workers)

    if not results:
        raise RuntimeError(
            "Metrics Server returned no "
            "EdgePilot worker metrics."
        )

    # -----------------------------------------------------
    # Step 3: Display current worker state
    # -----------------------------------------------------

    print()

    print(
        f'{"NODE":<14}'
        f'{"CPU":>8}'
        f'{"MEM":>8}'
        f'{"SCORE":>10}'
    )

    print("-" * 40)

    for row in sorted(
        results,
        key=lambda item:
            item["logical_name"]
    ):

        print(
            f'{row["logical_name"]:<14}'
            f'{row["cpu"]:>7.1f}%'
            f'{row["memory"]:>7.1f}%'
            f'{row["score"]:>10.2f}'
        )

    # -----------------------------------------------------
    # Step 4: EdgePilot chooses worker
    # -----------------------------------------------------

    selected = choose_node(results)

    print()

    print(
        "Selected worker: "
        f'{selected["logical_name"]}'
    )

    print(
        "Kubernetes node: "
        f'{selected["real_name"]}'
    )

    print(
        "Pressure score: "
        f'{selected["score"]:.2f}'
    )

    # -----------------------------------------------------
    # Step 5: Create workload
    # -----------------------------------------------------

    print()

    print(
        "Creating demonstration workload..."
    )

    create_workload(selected)

    # -----------------------------------------------------
    # Step 6: Wait for workload readiness
    # -----------------------------------------------------

    wait_for_workload()

    # -----------------------------------------------------
    # Step 7: Verify actual Kubernetes placement
    # -----------------------------------------------------

    actual_node, phase = (
        get_workload_status()
    )

    print()

    print("=" * 68)

    print(
        "PLACEMENT RESULT"
    )

    print("=" * 68)

    print(
        "EdgePilot recommendation : "
        f'{selected["logical_name"]}'
    )

    print(
        "Expected Kubernetes node  : "
        f'{selected["real_name"]}'
    )

    print(
        "Actual Kubernetes node    : "
        f"{actual_node}"
    )

    print(
        "Pod status                : "
        f"{phase}"
    )

    # -----------------------------------------------------
    # Step 8: Verify EdgePilot recommendation
    # -----------------------------------------------------

    if (
        actual_node
        == selected["real_name"]
    ):

        print()

        if phase == "Running":

            print(
                "SUCCESS: workload is running "
                "on EdgePilot's selected worker."
            )

        else:

            print(
                "PLACEMENT VERIFIED: workload "
                "was assigned to EdgePilot's "
                "selected worker, but its current "
                f"state is {phase}."
            )

    else:

        print()

        print(
            "WARNING: actual placement did "
            "not match EdgePilot's selected "
            "worker."
        )

    print("=" * 68)

    print()

    print(
        "Note: EdgePilot selects the target "
        "worker and injects a nodeSelector. "
        "Kubernetes performs the final "
        "scheduling and binding."
    )


# ---------------------------------------------------------
# Entry point
# ---------------------------------------------------------

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nEdgePilot placement "
            "interrupted."
        )

        sys.exit(130)

    except Exception as error:

        print(
            f"\nEdgePilot placement failed: "
            f"{error}"
        )

        sys.exit(1)
