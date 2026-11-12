# EdgePilot v2 — Milestone 6A: Kubernetes observation and latency validation

**Status:** Implementation unit-tested offline; **not yet executed on a real Kubernetes cluster** in this delivery. This is the first infrastructure-validation step, **not** a field study proving scheduling-policy superiority.

## Research objective and scope

The Milestone 5 synthetic reliability study found negative bounded-deferral effects under node outages and external contention. Before improving policies, we need an independently observed reference for real API-query latency, Pod creation-to-container-start latency, Pod execution and end-to-end deadline accounting.

Milestone 6A supplies:

1. Read-only Kubernetes preflight that queries node allocatable CPU and the CPU **requested by nonterminal bound Pods** across namespaces.
2. Conservative CPU telemetry snapshots with separate API-query start and receipt monotonic timestamps.
3. An **explicit opt-in** one-Pod smoke test on a chosen node, selected by the existing `Greedy` research policy using the observed request headroom. A `nodeSelector` keeps the **default Kubernetes scheduler** in the actual placement path.
4. Pod phase, server-reported creation/container start/finish timestamps, API submission latency, client-observed end-to-end latency, and conservative on-time status.
5. Bounded CPU (10–200 millicores requested), memory, 1–10 s container sleep, execution timeout (5–120 s), exact-name cleanup, JSON report, and offline tests with fake Kubernetes responses.

**What it does not do:** This smoke test does not compare four policies on identical real workloads; it does not implement an alternate kube-scheduler; it does not measure CPU utilization, account for all Kubernetes resource types, inject failures, or provide ground-truth telemetry sample timestamps. Those are future stages.

## Integration

Extract `edgepilot-v2-milestone6.zip` at the root of `D:\Development\edgepilot-v2` (merge the `research/`, `tests/`, and `configs/` directories, do not replace their contents).

```powershell
cd D:\Development\edgepilot-v2
python -m pytest -q
python -m research.kube_validation --help
```

Expected: **115 passed** (85 previous + 30 new). No Kubernetes installation or Docker engine is needed to run unit tests.

## Create a disposable local cluster — only if tools are installed

Check the tools first:

```powershell
docker info
kind version
kubectl version --client
kubectl config get-contexts
```

If any are missing, see the [kind Quick Start](https://kind.sigs.k8s.io/docs/user/quick-start) and [kubectl installation guide](https://kubernetes.io/docs/tasks/tools/). You need a running Docker engine, `kind` and `kubectl` on your PATH. Kind's docs advise adequate Docker VM RAM (8 GiB recommended on Windows for cluster work); multi-node kind still runs on one computer and is not a substitute for independent edge/cloud hosts.

If you do **not already have a test cluster named `edgepilot-v2`**, create one:

```powershell
kind create cluster --name edgepilot-v2 --config configs/kind-edgepilot-v2.yaml
kubectl --context kind-edgepilot-v2 get nodes -o wide
kubectl --context kind-edgepilot-v2 create namespace edgepilot-v2-validation
```

Do not use a production cluster. `kind create cluster` may download a node image. The kind config includes a control-plane and **two workers**. The probe excludes NotReady, cordoned, and NoSchedule/NoExecute-tainted nodes from its selectable list (by default, a kind control-plane node will not be selected).

## Step A — Safe read-only inventory

```powershell
python -m research.kube_validation `
  --context kind-edgepilot-v2 `
  --output research_outputs/kubernetes/preflight.json

Get-Content research_outputs/kubernetes/preflight.json
```

The program refuses to proceed if the current `kubectl` context is not the explicitly requested context. It invokes `kubectl get nodes` and `kubectl get pods --all-namespaces` only. It **does not create or delete anything** unless `--execute` is added.

Useful JSON keys:

| Key | Meaning |
|---|---|
| `snapshot.api_read_duration_seconds` | Local wall-clock monotonic duration of both Kubernetes API reads |
| `snapshot.node_inventory[].allocatable_cpu_cores` | CPU the node reports as allocatable to Pods |
| `snapshot.node_inventory[].requested_cpu_cores` | CPU requests of nonterminal Pods already bound to that node (not actual consumption) |
| `snapshot.node_inventory[].estimated_unreserved_cpu_cores` | Subtraction-based request headroom; may be negative |
| `snapshot.node_inventory[].usable_for_model` | Node is Ready, untainted for ordinary Pods, has hostname label, and is not request-oversubscribed |
| `snapshot.measurement_window_start_monotonic` | Start of a two-API-query window; **not an authoritative cluster measurement timestamp** |
| `snapshot.receipt_monotonic` | Time both API responses were available to the client |

**No future-state leakage:** No policy can use telemetry until after the read completes. A conservative age proxy uses request start as the estimated measurement time, but this is a bound/approximation rather than a true device/metric timestamp.

**Request estimator limitations:** Counts regular-container requests, max ordinary init-container request, and pod overhead. Supports pod-level CPU request. Restartable init sidecars trigger an explicit error to avoid silently incorrect accounting. Ignores pod status `Succeeded`/`Failed`; unbound Pending Pods are not charged to any node. CPU time-series usage would require an additional metrics source.

## Step B — Opt-in one-Pod latency smoke test

Only after inspecting the preflight report, run:

```powershell
python -m research.kube_validation `
  --context kind-edgepilot-v2 `
  --execute `
  --namespace edgepilot-v2-validation `
  --cpu-m 50 `
  --sleep-seconds 2 `
  --deadline-seconds 45 `
  --output research_outputs/kubernetes/smoke.json

Get-Content research_outputs/kubernetes/smoke.json
```

This creates **one Pod** named `epv2-smoke-...` in the **already-existing** `edgepilot-v2-validation` namespace, requests 50m CPU and 32Mi memory (with small resource limits), runs `busybox:1.36.1` with `sleep 2`, and polls until `Succeeded`, `Failed`, or the observation deadline is exceeded. It then deletes **that exact Pod** by name, unless you explicitly pass `--keep-pod`. A deletion failure is reported, never hidden. Image pulls, cluster CPU pressure, or other issues can cause it to fail; failures are useful validation evidence.

**On-time definition:** `smoke.on_time_observed` requires a successful terminal Pod phase **observed by the client** before the local monotonic deadline, including API submission, scheduling, image pull, runtime, and poll time. This is conservative: polling lag may cause a false deadline miss. The server timestamp fields are diagnostics and not subtracted from local monotonic time (different clocks cannot safely be mixed).

The smoke test chooses a node based on Kubernetes CPU **requests**, but uses `spec.nodeSelector` rather than setting `spec.nodeName`. The default scheduler can still reject that placement; this is **policy-directed default scheduling**, not a replacement scheduler. The probe runs only **Greedy**, so it does not establish a policy improvement.

## Validation, interpretation and next milestone

Before treating measurements as publication-quality, we must add real telemetry source timestamps (e.g. metrics-server or instrumented exporter), controlled staleness/delay, repeatable workload arrival patterns, experiment-order randomization, matched hardware conditions, multiple policies, scheduling-failure attribution, external load/outage injection under permissioned local test conditions, and confidence intervals. **Never compare this one Pod's latency directly with synthetic model times and call it simulator calibration.** Build a matched scenario with the same requested CPU, work duration, and exact deadline semantics first.

To inspect the report and commit the code (not generated data):

```powershell
git add research/kube_validation.py tests/test_kube_validation.py configs/kind-edgepilot-v2.yaml README_MILESTONE6.md
git commit -m "feat(research): add opt-in Kubernetes observation and latency validation"
git status
```

If you're finished with the disposable cluster, cleanup is a separate **explicit** action:

```powershell
kubectl --context kind-edgepilot-v2 delete namespace edgepilot-v2-validation
kind delete cluster --name edgepilot-v2
```

Do not run these cleanup commands on anything you need to keep. Neither command is executed by the Python tool automatically.

## Verified in delivery environment

`python -m pytest -q` passed **115 tests**, exercising both old v2 behavior and this new probe. All new tests are **offline mock tests**. No live Kubernetes result was generated or claimed.
