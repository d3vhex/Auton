#!/usr/bin/env python3
"""Structured evidence collection for an incident.

When Auton detects a problem it must not jump straight to a fix: it first
collects enough evidence to understand *why*. This module gathers that
evidence into a structured document instead of a blob of text, so the
Diagnosis Engine (:mod:`diagnosis`) can reason over it and the UI can render
it.

The shape follows the platform spec::

    {
      "resource": {
        "kind": "Pod",
        "name": "api-7fd89",
        "namespace": "auton-apps",
        "uid": "…"
      },
      "problem": "CrashLoopBackOff",
      "container": {
        "name": "api",
        "restart_count": 12,
        "last_termination_reason": "OOMKilled",
        "exit_code": 137
      },
      "resources": {
        "memory_request": "256Mi",
        "memory_limit": "512Mi"
      },
      "owner": {"kind": "Deployment", "name": "api"},
      "evidence": {
        "logs_available": true,
        "events_available": true,
        "node_metrics_available": true
      }
    }

Three rules hold everywhere in this module:

1. **Secrets are never read.** No Secret object is fetched and no credential
   value is ever placed in an evidence document. For image-pull credentials
   only the *presence* of a pull secret is reported (a boolean), never its
   name or content.
2. **Nothing is invented.** Evidence that could not be collected is ``None``
   and the matching availability flag is ``false``. There is no default that
   could be mistaken for a fact.
3. **Collection never mutates.** Everything here is a read.

Every collector swallows its own errors and records them in
``evidence["errors"]``: missing evidence must not crash the repair pipeline.
"""
from __future__ import annotations

import time

import k8s_tools

#: Reasons that mean "the container could not start at all".
IMAGE_REASONS = ("ImagePullBackOff", "ErrImagePull", "InvalidImageName",
                  "ImageInspectError", "RegistryUnavailable")
CONFIG_REASONS = ("CreateContainerConfigError", "CreateContainerError",
                  "RunContainerError", "ContainerCannotRun",
                  "InvalidImageName")

#: Node metrics need the metrics API; it may not be installed.
_NODE_METRICS_CACHE_TTL = 30.0
_NODE_METRICS_CACHE: dict[str, tuple[float, dict]] = {}


def _error(message: str) -> str:
    return message[:500]


def _query(args: list[str], namespace: str, timeout: int = 30):
    """Read a Kubernetes object; returns ``(data, error)``."""
    return k8s_tools._query(args, namespace=namespace, timeout=timeout)


def _owner_chain(pod: dict) -> list[dict]:
    """Resolve the owner chain of a pod (ReplicaSet → Deployment → …)."""
    chain: list[dict] = []
    current = pod
    for _ in range(5):
        owners = (current.get("metadata") or {}).get("ownerReferences") or []
        if not owners:
            break
        owner = owners[0]
        entry = {
            "kind": owner.get("kind", ""),
            "name": owner.get("name", ""),
            "uid": owner.get("uid", ""),
            "controller": bool(owner.get("controller")),
        }
        chain.append(entry)
        if entry["kind"] in ("Deployment", "StatefulSet", "DaemonSet",
                             "Job", "CronJob"):
            break
        # One level deeper: the ReplicaSet that owns the pod.
        data, error = _query(["get", "replicaset", entry["name"], "-o", "json"],
                             (current.get("metadata") or {}).get("namespace", ""))
        if error or not data:
            break
        current = data
    return chain


def _container_report(pod: dict, wanted: str = "") -> dict:
    """The container-level facts the spec asks for."""
    spec_containers = (pod.get("spec") or {}).get("containers") or []
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    by_name = {s.get("name", ""): s for s in statuses}

    report: dict = {
        "name": "",
        "restart_count": 0,
        "last_termination_reason": "",
        "exit_code": None,
        "state": "",
        "ready": False,
        "image": "",
        "image_id": "",
    }
    # Pick the container the caller asked for, else the one that is failing.
    chosen = None
    if wanted and wanted in by_name:
        chosen = by_name[wanted]
    else:
        for status in statuses:
            state = status.get("state") or {}
            waiting = (state.get("waiting") or {}).get("reason") or ""
            terminated = (state.get("terminated") or {}).get("reason") or ""
            previous = (status.get("lastState") or {}).get("terminated") or {}
            if waiting or terminated or previous.get("reason") or not status.get("ready"):
                chosen = status
                break
        if chosen is None and statuses:
            chosen = statuses[0]
    if chosen is None:
        return report

    name = chosen.get("name", "")
    report["name"] = name
    report["restart_count"] = int(chosen.get("restartCount") or 0)
    report["ready"] = bool(chosen.get("ready"))
    report["image"] = chosen.get("image", "")
    report["image_id"] = (chosen.get("imageID") or "")[:200]

    state = chosen.get("state") or {}
    if state.get("running"):
        report["state"] = "running"
    elif state.get("waiting"):
        report["state"] = "waiting"
        report["last_termination_reason"] = (
            (state.get("waiting") or {}).get("reason") or "")
    elif state.get("terminated"):
        report["state"] = "terminated"
        report["last_termination_reason"] = (
            (state.get("terminated") or {}).get("reason") or "")

    # The *previous* termination is the interesting one for a crash loop:
    # the current state only says "waiting to restart".
    previous = (chosen.get("lastState") or {}).get("terminated") or {}
    if previous:
        report["last_termination_reason"] = previous.get("reason") or \
            report["last_termination_reason"]
        report["exit_code"] = previous.get("exitCode")
        report["previous_termination"] = {
            "reason": previous.get("reason", ""),
            "exit_code": previous.get("exitCode"),
            "started_at": previous.get("startedAt", ""),
            "finished_at": previous.get("finishedAt", ""),
        }

    # The container spec (resources, probes) for the chosen container.
    for spec in spec_containers:
        if spec.get("name") != name:
            continue
        limits = ((spec.get("resources") or {}).get("limits") or {})
        requests = ((spec.get("resources") or {}).get("requests") or {})
        report["resources"] = {
            "memory_request": requests.get("memory", ""),
            "memory_limit": limits.get("memory", ""),
            "cpu_request": requests.get("cpu", ""),
            "cpu_limit": limits.get("cpu", ""),
        }
        liveness = spec.get("livenessProbe") or {}
        readiness = spec.get("readinessProbe") or {}
        startup = spec.get("startupProbe") or {}
        report["probes"] = {
            "liveness": _probe_report(liveness),
            "readiness": _probe_report(readiness),
            "startup": _probe_report(startup),
        }
        # Only the *presence* of a pull secret is reported — never its name
        # or content.
        report["image_pull_secret_configured"] = bool(
            (pod.get("spec") or {}).get("imagePullSecrets"))
        break
    report.setdefault("resources", {
        "memory_request": "", "memory_limit": "",
        "cpu_request": "", "cpu_limit": "",
    })
    report.setdefault("probes", {
        "liveness": _probe_report({}),
        "readiness": _probe_report({}),
        "startup": _probe_report({}),
    })
    report.setdefault("image_pull_secret_configured", False)
    return report


def _probe_report(probe: dict) -> dict:
    """Summarise a probe without its command (which may hold arguments)."""
    if not probe:
        return {"configured": False, "type": "", "initial_delay_seconds": None,
                "period_seconds": None, "timeout_seconds": None,
                "failure_threshold": None}
    handler = probe.get("httpGet") or probe.get("tcpSocket") or \
        probe.get("exec") or probe.get("grpc") or {}
    kind = ""
    for candidate in ("httpGet", "tcpSocket", "exec", "grpc"):
        if candidate in probe:
            kind = candidate
            break
    return {
        "configured": True,
        "type": kind,
        "port": handler.get("port") if kind == "httpGet" else None,
        "path": handler.get("path") if kind == "httpGet" else None,
        "initial_delay_seconds": probe.get("initialDelaySeconds"),
        "period_seconds": probe.get("periodSeconds"),
        "timeout_seconds": probe.get("timeoutSeconds"),
        "failure_threshold": probe.get("failureThreshold"),
    }


def _pod_events(namespace: str, name: str) -> tuple[list[dict], bool]:
    """Warning events for one pod. Returns ``(events, available)``."""
    data, error = _query(
        ["get", "events", "--field-selector",
         f"involvedObject.name={name},type=Warning", "-o", "json"],
        namespace)
    if error or not data:
        return [], False
    events = []
    for item in (data.get("items") or []):
        events.append({
            "reason": item.get("reason", ""),
            "message": (item.get("message") or "")[:500],
            "type": item.get("type", ""),
            "count": int(item.get("count") or 1),
            "last_timestamp": (item.get("lastTimestamp")
                               or item.get("eventTime")
                               or (item.get("metadata") or {}).get(
                                   "creationTimestamp", "")),
        })
    return events, True


def _pod_logs(namespace: str, name: str, container: str = "",
              previous: bool = False) -> tuple[str, bool]:
    """Container logs. Returns ``(text, available)``."""
    args = ["logs", name]
    if container:
        args += ["-c", container]
    if previous:
        args.append("--previous")
    args += ["--tail=100"]
    out, error = k8s_tools._execute(args, timeout=20, namespace=namespace)
    if error:
        return "", False
    return (out or "").strip()[-4000:], True


def _node_report(node_name: str) -> dict:
    """Node capacity and pressure. Never fails the collection."""
    report = {"name": node_name, "available": False, "allocatable": {},
              "capacity": {}, "conditions": [], "pressure": {}}
    if not node_name:
        return report
    data, error = _query(["get", "node", node_name, "-o", "json"], "default")
    if error or not data:
        # A namespaced read of a cluster object does not work; ask explicitly.
        data, error = k8s_tools._query(["get", "node", node_name, "-o", "json"],
                                       namespace=None)
    if error or not data:
        return report
    report["available"] = True
    report["allocatable"] = (data.get("status") or {}).get("allocatable") or {}
    report["capacity"] = (data.get("status") or {}).get("capacity") or {}
    for condition in (data.get("status") or {}).get("conditions") or []:
        report["conditions"].append({
            "type": condition.get("type", ""),
            "status": condition.get("status", ""),
            "reason": condition.get("reason", ""),
            "message": (condition.get("message") or "")[:300],
        })
    for key in ("MemoryPressure", "DiskPressure", "PIDPressure"):
        for condition in report["conditions"]:
            if condition["type"] == key:
                report["pressure"][key] = condition["status"]
    return report


def _node_metrics(namespace: str, node_name: str) -> tuple[dict, bool]:
    """Node and container metrics from the metrics API, if it is installed.

    The metrics API is optional (it is a separate server), so a failure only
    means "not available" — it never fails the collection.
    """
    if not node_name:
        return {}, False
    now = time.time()
    cached = _NODE_METRICS_CACHE.get(node_name)
    if cached and now - cached[0] < _NODE_METRICS_CACHE_TTL:
        return cached[1], bool(cached[1])
    report: dict = {"nodes": [], "pods": []}
    # `kubectl top nodes` is cluster scoped, so the namespace argument that
    # `_execute` injects is ignored by the server.
    nodes, error = k8s_tools._query(["top", "nodes", "--no-headers"],
                                    namespace=None, timeout=15)
    if not error:
        for line in (nodes or "").splitlines():
            parts = line.split()
            if len(parts) >= 4:
                report["nodes"].append({
                    "name": parts[0], "cpu": parts[1], "memory": parts[2],
                    "memory_percent": parts[3],
                })
    pods, error = k8s_tools._query(["top", "pods", "--no-headers"],
                                    namespace=namespace, timeout=15)
    if not error:
        for line in (pods or "").splitlines():
            parts = line.split()
            if len(parts) >= 4:
                report["pods"].append({
                    "namespace": parts[0], "pod": parts[1],
                    "cpu": parts[2], "memory": parts[3],
                })
    available = bool(report["nodes"] or report["pods"])
    _NODE_METRICS_CACHE[node_name] = (now, report if available else {})
    return (report if available else {}), available


def collect(namespace: str, kind: str = "Pod", name: str = "",
            container: str = "", problem: str = "") -> dict:
    """Collect structured evidence for one resource.

    Never raises: a failed collection comes back as a document with
    ``errors`` filled in and the availability flags set to ``False``.
    """
    errors: list[str] = []
    document: dict = {
        "resource": {
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "uid": "",
        },
        "problem": problem,
        "container": {},
        "resources": {},
        "owner": {},
        "probes": {},
        "node": {},
        "events": [],
        "logs": "",
        "previous_logs": "",
        "evidence": {
            "pod_available": False,
            "logs_available": False,
            "previous_logs_available": False,
            "events_available": False,
            "node_available": False,
            "node_metrics_available": False,
            "owner_available": False,
        },
        "collected_at": time.time(),
        "errors": errors,
    }

    if kind.lower() != "pod":
        # Deployments/StatefulSets are handled through their pods; there is
        # nothing pod-shaped to collect.
        data, error = _query(["get", _plural(kind), name, "-o", "json"],
                             namespace)
        if error:
            errors.append(_error(f"cannot read {kind.lower()}/{name}: {error}"))
            return document
        document["resource"]["uid"] = (data.get("metadata") or {}).get("uid", "")
        document["resource"]["labels"] = dict(
            (data.get("metadata") or {}).get("labels") or {})
        document["evidence"]["pod_available"] = True
        document["resources"] = _workload_resources(data)
        document["owner"] = {"kind": kind, "name": name}
        document["evidence"]["owner_available"] = True
        return document

    pod, error = _query(["get", "pod", name, "-o", "json"], namespace)
    if error or not pod:
        errors.append(_error(f"cannot read pod/{name}: {error or 'not found'}"))
        return document

    meta = pod.get("metadata") or {}
    document["resource"]["uid"] = meta.get("uid", "")
    # The labels are part of the resource identity: the policy engine reads
    # them to honour `auton.io/protect=true`. Only the labels are collected —
    # never an annotation value that could carry a secret.
    document["resource"]["labels"] = dict(meta.get("labels") or {})
    document["evidence"]["pod_available"] = True

    status = pod.get("status") or {}
    document["problem"] = document["problem"] or _problem_of(pod)
    document["phase"] = status.get("phase", "")
    document["restart_count"] = sum(
        int(c.get("restartCount") or 0)
        for c in (status.get("containerStatuses") or []))

    container_report = _container_report(pod, container)
    document["container"] = container_report
    document["resources"] = container_report.get("resources") or {}
    document["probes"] = container_report.get("probes") or {}

    chain = _owner_chain(pod)
    if chain:
        document["owner"] = {
            "kind": chain[-1]["kind"],
            "name": chain[-1]["name"],
            "uid": chain[-1]["uid"],
            "chain": chain,
        }
        document["evidence"]["owner_available"] = True

    events, events_ok = _pod_events(namespace, name)
    document["events"] = events
    document["evidence"]["events_available"] = events_ok

    target_container = container_report.get("name") or container
    logs, logs_ok = _pod_logs(namespace, name, target_container)
    document["logs"] = logs
    document["evidence"]["logs_available"] = logs_ok
    previous_logs, previous_ok = _pod_logs(namespace, name, target_container,
                                           previous=True)
    document["previous_logs"] = previous_logs
    document["evidence"]["previous_logs_available"] = previous_ok

    node_name = (pod.get("spec") or {}).get("nodeName", "")
    node_report = _node_report(node_name)
    document["node"] = node_report
    document["evidence"]["node_available"] = node_report.get("available", False)
    metrics, metrics_ok = _node_metrics(namespace, node_name)
    document["node_metrics"] = metrics
    document["evidence"]["node_metrics_available"] = metrics_ok

    document["deployment"] = _deployment_for(namespace, chain)
    return document


def _plural(kind: str) -> str:
    value = (kind or "").strip().lower()
    return {
        "deployment": "deployment",
        "statefulset": "statefulset",
        "daemonset": "daemonset",
        "replicaset": "replicaset",
        "job": "job",
        "persistentvolumeclaim": "pvc",
    }.get(value, value or "pod")


def _workload_resources(workload: dict) -> dict:
    """Requests/limits of the first container of a workload."""
    spec = (workload.get("spec") or {}).get("template") or {}
    containers = ((spec.get("spec") or {}).get("containers") or [])
    if not containers:
        return {"memory_request": "", "memory_limit": "",
                "cpu_request": "", "cpu_limit": ""}
    resources = (containers[0].get("resources") or {})
    return {
        "memory_request": (resources.get("requests") or {}).get("memory", ""),
        "memory_limit": (resources.get("limits") or {}).get("memory", ""),
        "cpu_request": (resources.get("requests") or {}).get("cpu", ""),
        "cpu_limit": (resources.get("limits") or {}).get("cpu", ""),
    }


def _deployment_for(namespace: str, chain: list[dict]) -> dict:
    """The Deployment behind a pod, when there is one."""
    deployment = None
    for entry in chain:
        if entry["kind"] == "Deployment":
            deployment = entry
            break
    if deployment is None:
        return {}
    data, error = _query(["get", "deployment", deployment["name"], "-o", "json"],
                         namespace)
    if error or not data:
        return {"kind": "Deployment", "name": deployment["name"],
                "available": False}
    status = data.get("status") or {}
    meta = data.get("metadata") or {}
    conditions = {c.get("type"): c for c in (status.get("conditions") or [])}
    progressing = conditions.get("Progressing") or {}
    return {
        "kind": "Deployment",
        "name": deployment["name"],
        "available": True,
        "generation": meta.get("generation", 0),
        "resource_version": meta.get("resourceVersion", ""),
        "uid": meta.get("uid", ""),
        "desired_replicas": int((data.get("spec") or {}).get("replicas") or 0),
        "available_replicas": int(status.get("availableReplicas") or 0),
        "ready_replicas": int(status.get("readyReplicas") or 0),
        "updated_replicas": int(status.get("updatedReplicas") or 0),
        "progressing": progressing.get("status", ""),
        "progressing_reason": progressing.get("reason", ""),
        "conditions": [
            {"type": c.get("type", ""), "status": c.get("status", ""),
             "reason": c.get("reason", "")}
            for c in (status.get("conditions") or [])
        ],
        "resources": _workload_resources(data),
        "revision": (meta.get("annotations") or {}).get(
            "deployment.kubernetes.io/revision", ""),
    }


def _problem_of(pod: dict) -> str:
    """The symptom a pod is currently showing."""
    status = pod.get("status") or {}
    for container_status in status.get("containerStatuses") or []:
        state = container_status.get("state") or {}
        waiting = (state.get("waiting") or {}).get("reason") or ""
        if waiting:
            return waiting
        terminated = (state.get("terminated") or {}).get("reason") or ""
        if terminated:
            return terminated
        previous = (container_status.get("lastState") or {}).get("terminated") \
            or {}
        if previous.get("reason"):
            return previous["reason"]
    phase = status.get("phase", "")
    for condition in status.get("conditions") or []:
        if (condition.get("type") == "PodScheduled"
                and condition.get("status") == "False"):
            return "Unschedulable"
    return phase or "Unknown"


def summarize(document: dict) -> str:
    """A short text rendering of the evidence (for the incident record)."""
    if not document:
        return ""
    resource = document.get("resource") or {}
    container = document.get("container") or {}
    lines = [
        f"{resource.get('kind', '')}/{resource.get('name', '')} "
        f"namespace={resource.get('namespace', '')}",
        f"problem: {document.get('problem') or 'unknown'}"
        f" phase={document.get('phase', '')}"
        f" restarts={document.get('restart_count', 0)}",
    ]
    if container.get("name"):
        lines.append(
            f"container {container['name']}: "
            f"restarts={container.get('restart_count', 0)} "
            f"last_termination={container.get('last_termination_reason') or '-'} "
            f"exit_code={container.get('exit_code')}")
    resources = document.get("resources") or {}
    if resources.get("memory_limit") or resources.get("memory_request"):
        lines.append(
            f"resources: memory_request={resources.get('memory_request') or '-'} "
            f"memory_limit={resources.get('memory_limit') or '-'} "
            f"cpu_request={resources.get('cpu_request') or '-'} "
            f"cpu_limit={resources.get('cpu_limit') or '-'}")
    owner = document.get("owner") or {}
    if owner.get("name"):
        lines.append(f"owner: {owner.get('kind', '')}/{owner.get('name', '')}")
    events = document.get("events") or []
    if events:
        lines.append("events: " + ", ".join(
            f"{e.get('reason', '')} x{e.get('count', 1)}" for e in events[:6]))
    availability = document.get("evidence") or {}
    lines.append("available: " + ", ".join(
        f"{key}={'yes' if value else 'no'}"
        for key, value in sorted(availability.items())))
    errors = document.get("errors") or []
    for message in errors[:3]:
        lines.append(f"collection error: {message}")
    return "\n".join(lines)
