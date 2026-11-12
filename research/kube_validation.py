"""Conservative, opt-in Kubernetes probe for EdgePilot v2.

Read-only by default. --execute creates ONE disposable Pod in an explicitly
selected namespace and deletes only that exact Pod on completion by default.

CPU telemetry here describes *Kubernetes CPU requests/reservations*, NOT
measured CPU utilization, kubelet admission guarantees, or a scheduler trace.
It is not calibrated by synthetic wall-clock times.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, Callable
from uuid import uuid4

from .models import TelemetrySample, Workload
from .policies import Greedy

_QUANTITY = re.compile(r"^([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(n|u|m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei)?$")
_MULTIPLIER = {
    None: Decimal(1), 'n': Decimal('0.000000001'), 'u': Decimal('0.000001'),
    'm': Decimal('0.001'), 'k': Decimal(1000), 'M': Decimal(10) ** 6,
    'G': Decimal(10) ** 9, 'T': Decimal(10) ** 12, 'P': Decimal(10) ** 15,
    'E': Decimal(10) ** 18,
    **{s: Decimal(1024) ** power for power, s in enumerate(('Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei'), 1)},
}
_NAMESPACE = re.compile(r'^edgepilot-v2-validation(?:-[a-z0-9]+)*$')


def cpu_cores(quantity: str | int | float) -> float:
    """Parse Kubernetes CPU quantity (e.g. 500m, 1, 250u) into cores."""
    if isinstance(quantity, bool) or not isinstance(quantity, (str, int, float)):
        raise ValueError('CPU quantity must be numeric or Kubernetes quantity string')
    text = str(quantity).strip()
    m = _QUANTITY.fullmatch(text)
    if not m:
        raise ValueError(f'invalid CPU quantity: {quantity!r}')
    try:
        value = Decimal(m.group(1)) * _MULTIPLIER[m.group(2)]
        result = float(value)
    except (InvalidOperation, OverflowError) as e:
        raise ValueError('invalid CPU quantity') from e
    if not math.isfinite(result) or result < 0:
        raise ValueError('CPU quantity must be finite and nonnegative')
    return result


def _container_cpu(container: dict[str, Any]) -> float:
    resources = container.get('resources') or {}
    return cpu_cores((resources.get('requests') or {}).get('cpu', '0'))


def requested_cpu(pod: dict[str, Any]) -> float:
    """Effective CPU request approximation including init containers and overhead.

    Restartable init sidecars require more complex Kubernetes accounting and
    are explicitly rejected instead of silently undercounted.
    """
    spec = pod.get('spec') or {}
    containers = spec.get('containers') or []
    regular = sum(_container_cpu(c) for c in containers)
    initial = spec.get('initContainers') or []
    if any(c.get('restartPolicy') == 'Always' for c in initial):
        raise ValueError('restartable init sidecars are not supported by CPU request estimator')
    maximum_init = max((_container_cpu(c) for c in initial), default=0.0)
    pod_level = (spec.get('resources') or {}).get('requests') or {}
    # When set, pod-level CPU request takes precedence over container sum.
    if 'cpu' in pod_level:
        regular = cpu_cores(pod_level['cpu'])
    return max(regular, maximum_init) + cpu_cores((spec.get('overhead') or {}).get('cpu', '0'))


def _is_ready(node: dict[str, Any]) -> bool:
    conditions = (node.get('status') or {}).get('conditions') or []
    return any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in conditions)


def node_inventory(nodes: dict[str, Any], pods: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute per-node allocatable CPU minus nonterminal Pod CPU requests.

    This is NOT actual CPU usage. Detect oversubscription rather than hiding it.
    """
    if not isinstance(nodes.get('items'), list) or not isinstance(pods.get('items'), list):
        raise ValueError('kubectl JSON must contain items lists')
    records: dict[str, dict[str, Any]] = {}
    for node in nodes['items']:
        name = (node.get('metadata') or {}).get('name')
        if not isinstance(name, str) or not name or name in records:
            raise ValueError('missing or duplicate node name')
        allocatable = cpu_cores(((node.get('status') or {}).get('allocatable') or {})['cpu'])
        labels = (node.get('metadata') or {}).get('labels') or {}
        taints = (node.get('spec') or {}).get('taints') or []
        ready = _is_ready(node)
        schedulable = (ready and not (node.get('spec') or {}).get('unschedulable', False)
                       and not any(t.get('effect') in ('NoSchedule', 'NoExecute') for t in taints))
        records[name] = {
            'name': name, 'hostname_label': labels.get('kubernetes.io/hostname'),
            'allocatable_cpu_cores': allocatable, 'requested_cpu_cores': 0.0,
            'active_pod_count': 0, 'ready': ready, 'schedulable_without_tolerations': schedulable,
        }
    for pod in pods['items']:
        if (pod.get('status') or {}).get('phase') in ('Succeeded', 'Failed'):
            continue
        nid = (pod.get('spec') or {}).get('nodeName')
        if nid in records:
            records[nid]['requested_cpu_cores'] += requested_cpu(pod)
            records[nid]['active_pod_count'] += 1
    for record in records.values():
        free = record['allocatable_cpu_cores'] - record['requested_cpu_cores']
        record['estimated_unreserved_cpu_cores'] = round(free, 9)
        record['oversubscribed_requests'] = free < -1e-9
        # Do not let Policy/TelemetrySample model silently clamp overload.
        record['usable_for_model'] = (record['schedulable_without_tolerations']
                                      and not record['oversubscribed_requests']
                                      and bool(record['hostname_label'])
                                      and record['allocatable_cpu_cores'] > 0)
    return [records[k] for k in sorted(records)]


def samples_from_inventory(inventory: list[dict[str, Any]],
                           measured_at: float, received_at: float) -> dict[str, TelemetrySample]:
    return {
        r['name']: TelemetrySample(r['name'], measured_at, received_at,
                                   r['allocatable_cpu_cores'], r['requested_cpu_cores'])
        for r in inventory if r['usable_for_model']
    }


class KubectlError(RuntimeError):
    pass


class Kubectl:
    """No shell execution; all actions are explicitly bound to one context."""
    def __init__(self, context: str, *, executable: str = 'kubectl',
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        if not isinstance(context, str) or not context.strip() or context.startswith('-'):
            raise ValueError('an explicit Kubernetes context is required')
        if not shutil.which(executable):
            raise KubectlError(f'{executable} is not installed or not on PATH')
        self.context = context
        self.executable = executable
        self.runner = runner

    def call(self, *args: str, stdin: str | None = None, timeout: float = 30.0) -> str:
        cmd = [self.executable, '--context', self.context, '--request-timeout=15s', *args]
        try:
            result = self.runner(cmd, input=stdin, capture_output=True, text=True,
                                 timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KubectlError(f'kubectl request failed: {exc}') from exc
        if result.returncode != 0:
            raise KubectlError(f'kubectl {args[0]} failed: {(result.stderr or result.stdout).strip()[:600]}')
        return result.stdout

    def get_json(self, *args: str) -> dict[str, Any]:
        try:
            return json.loads(self.call('get', *args, '-o', 'json'))
        except json.JSONDecodeError as exc:
            raise KubectlError('kubectl did not return valid JSON') from exc

    def verify_context(self):
        actual = self.call('config', 'current-context').strip()
        if actual != self.context:
            raise KubectlError(f'current context {actual!r} does not match --context {self.context!r}')


def capture_snapshot(client: Kubectl, *, monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Read-only snapshot. Timestamp is beginning of query, a conservative age proxy."""
    start = monotonic()
    nodes = client.get_json('nodes')
    pods = client.get_json('pods', '--all-namespaces')
    end = monotonic()
    inventory = node_inventory(nodes, pods)
    return {
        'measurement_window_start_monotonic': start,
        'receipt_monotonic': end,
        'api_read_duration_seconds': end - start,
        'node_inventory': inventory,
        'measurement_note': 'CPU reservations, not measured CPU usage. '
                            'Measurement timestamp is query start, not a Kubernetes source timestamp.',
    }


def pod_manifest(name: str, node_hostname: str, cpu_m: int = 50, sleep_seconds: int = 2) -> dict[str, Any]:
    if not re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', name) or len(name) > 63:
        raise ValueError('invalid pod name')
    if not node_hostname or not isinstance(node_hostname, str):
        raise ValueError('node hostname required')
    if type(cpu_m) is not int or not (10 <= cpu_m <= 200):
        raise ValueError('cpu_m must be integer in range 10..200')
    if type(sleep_seconds) is not int or not (1 <= sleep_seconds <= 10):
        raise ValueError('sleep_seconds must be integer in range 1..10')
    return {
        'apiVersion': 'v1', 'kind': 'Pod',
        'metadata': {'name': name, 'labels': {'app': 'edgepilot-v2-validation'}},
        'spec': {'restartPolicy': 'Never',
                 # nodeSelector still involves the default Kubernetes scheduler.
                 'nodeSelector': {'kubernetes.io/hostname': node_hostname},
                 'terminationGracePeriodSeconds': 0,
                 'containers': [{
                     'name': 'probe', 'image': 'busybox:1.36.1',
                     'imagePullPolicy': 'IfNotPresent',
                     'command': ['sh', '-c', f'sleep {sleep_seconds}'],
                     'resources': {'requests': {'cpu': f'{cpu_m}m', 'memory': '32Mi'},
                                   'limits': {'cpu': '250m', 'memory': '64Mi'}},
                 }]},
    }


def _datetime_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def pod_observation(pod: dict[str, Any]) -> dict[str, Any]:
    status = pod.get('status') or {}
    conditions = status.get('containerStatuses') or []
    terminated = next((x.get('state', {}).get('terminated') for x in conditions
                       if x.get('state', {}).get('terminated') is not None), None)
    created = _datetime_seconds((pod.get('metadata') or {}).get('creationTimestamp'))
    started = _datetime_seconds((terminated or {}).get('startedAt'))
    finished = _datetime_seconds((terminated or {}).get('finishedAt'))
    return {
        'phase': status.get('phase', 'Unknown'),
        'node_name': (pod.get('spec') or {}).get('nodeName'),
        'reason': status.get('reason'),
        'message': status.get('message'),
        'creation_timestamp': (pod.get('metadata') or {}).get('creationTimestamp'),
        'container_started_at': (terminated or {}).get('startedAt'),
        'container_finished_at': (terminated or {}).get('finishedAt'),
        'container_exit_code': (terminated or {}).get('exitCode'),
        'kubernetes_creation_to_container_start_seconds':
            (started - created) if created is not None and started is not None else None,
        'kubernetes_container_execution_seconds':
            (finished - started) if started is not None and finished is not None else None,
    }


def run_smoke(client: Kubectl, namespace: str, snapshot: dict[str, Any], *,
              timeout_seconds: float = 45.0, cpu_m: int = 50,
              sleep_seconds: int = 2, poll_interval: float = 0.5,
              cleanup: bool = True,
              monotonic: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Perform one bounded, namespace-scoped, policy-directed Pod probe.

    On-time is based on local monotonic OBSERVATION time; this includes polling
    delay. Kubernetes timestamps are diagnostics rather than clocks mixed with
    monotonic client timings.
    """
    if not _NAMESPACE.fullmatch(namespace):
        raise ValueError('namespace must be edgepilot-v2-validation or a suffixed variant')
    if not math.isfinite(timeout_seconds) or timeout_seconds < 5 or timeout_seconds > 120:
        raise ValueError('timeout must be 5..120 seconds')
    if not math.isfinite(poll_interval) or not 0.2 <= poll_interval <= 5:
        raise ValueError('poll interval must be 0.2..5 seconds')
    # Explicitly verify that the namespace already exists; never create it.
    client.get_json('namespace', namespace)
    workload_arrival = monotonic()
    w = Workload('validation-smoke', workload_arrival,
                 workload_arrival + timeout_seconds,
                 cpu_m / 1000, float(sleep_seconds))
    samples = samples_from_inventory(snapshot['node_inventory'],
                                     snapshot['measurement_window_start_monotonic'],
                                     snapshot['receipt_monotonic'])
    decision = Greedy().decide(w, workload_arrival, samples, 0)
    if decision is None or decision.node_id is None:
        raise KubectlError('no eligible node with enough estimated unreserved CPU')
    chosen = next(x for x in snapshot['node_inventory'] if x['name'] == decision.node_id)
    name = f'epv2-smoke-{uuid4().hex[:12]}'
    manifest = pod_manifest(name, chosen['hostname_label'], cpu_m, sleep_seconds)
    created = False
    result: dict[str, Any] = {
        'policy': 'greedy', 'pod_name': name, 'namespace': namespace,
        'selected_node': chosen['name'],
        'estimated_telemetry_age_at_decision_seconds':
            workload_arrival - snapshot['measurement_window_start_monotonic'],
        'deadline_seconds': timeout_seconds,
        'measurement_class': 'real Kubernetes API smoke test; not paired policy comparison',
        'terminal_phase': 'NotCreated', 'on_time_observed': False,
    }
    try:
        client.call('create', '-n', namespace, '-f', '-', stdin=json.dumps(manifest))
        created = True
        result['submit_api_elapsed_seconds'] = monotonic() - workload_arrival
        while True:
            try:
                current = client.get_json('pod', name, '-n', namespace)
                observed = pod_observation(current)
                result.update(observed)
                result['terminal_phase'] = observed['phase']
            except KubectlError as exc:
                result['last_poll_error'] = str(exc)
            elapsed = monotonic() - workload_arrival
            result['end_to_end_observed_seconds'] = elapsed
            if result['terminal_phase'] in ('Succeeded', 'Failed'):
                result['on_time_observed'] = (result['terminal_phase'] == 'Succeeded'
                                              and elapsed <= timeout_seconds)
                break
            if elapsed >= timeout_seconds:
                result['terminal_phase'] = 'TimedOutObservation'
                break
            sleep(min(poll_interval, timeout_seconds - elapsed))
    finally:
        if created and cleanup:
            try:
                client.call('delete', 'pod', name, '-n', namespace,
                            '--ignore-not-found=true', '--wait=false')
                result['cleanup_attempted_and_accepted'] = True
            except KubectlError as exc:
                result['cleanup_error'] = str(exc)
    return result


def _write(path: Path, payload: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True, help='Expected kubectl current context (explicitly required)')
    parser.add_argument('--output', type=Path, default=Path('research_outputs/kubernetes/preflight.json'))
    parser.add_argument('--execute', action='store_true', help='Opt in to creation of one disposable Pod')
    parser.add_argument('--namespace', default='edgepilot-v2-validation')
    parser.add_argument('--deadline-seconds', type=float, default=45.0)
    parser.add_argument('--poll-interval', type=float, default=0.5)
    parser.add_argument('--cpu-m', type=int, default=50)
    parser.add_argument('--sleep-seconds', type=int, default=2)
    parser.add_argument('--keep-pod', action='store_true', help='Do not delete the created pod after the probe')
    args = parser.parse_args(argv)
    client = Kubectl(args.context)
    client.verify_context()
    snapshot = capture_snapshot(client)
    report = {
        'study': 'EdgePilot v2 / Kubernetes feasibility and latency validation',
        'context': args.context, 'observed_at_utc': datetime.now(timezone.utc).isoformat(),
        'snapshot': snapshot,
        'warnings': ['CPU capacity is inferred from pod requests, not CPU utilization.',
                     'API reads are not atomic; node/pod lists can change between requests.',
                     'Live smoke tests use default kube-scheduler + nodeSelector, not a custom scheduler.',
                     'Image pulling and first-run startup latency can dominate results.',
                     'A single smoke test does not establish a statistically valid policy comparison.'],
    }
    if args.execute:
        report['smoke'] = run_smoke(client, args.namespace, snapshot,
                                    timeout_seconds=args.deadline_seconds,
                                    cpu_m=args.cpu_m, sleep_seconds=args.sleep_seconds,
                                    poll_interval=args.poll_interval,
                                    cleanup=not args.keep_pod)
    _write(args.output, report)
    print(f'Wrote {args.output} ({len(snapshot["node_inventory"])} nodes).')
    if args.execute:
        sm = report['smoke']
        print(f'Smoke Pod {sm["pod_name"]}: {sm["terminal_phase"]}; on-time observed={sm["on_time_observed"]}')


if __name__ == '__main__':
    main()
