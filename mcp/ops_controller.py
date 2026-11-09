#!/usr/bin/env python3
"""Autonomous Kubernetes operations controller.

Runs as its own Deployment (``auton-ops``) next to the API pod. It is *not* a
thread in the agent service: an agent task can take minutes and a crash in the
controller must not kill the API.

Loop (every ``AGENT_OPS_INTERVAL`` seconds, default 60):

1. Collect a read-only snapshot of each namespace in ``AGENT_OPS_NAMESPACES``
   (default ``auton-apps``) with the same code the model uses
   (``k8s_tools._collect``), so the controller and the agent see one truth.
2. Turn deterministic findings (OOMKilled, CrashLoopBackOff, stuck Terminating,
   failed rollout …) into incidents. Image/config/scheduling/PVC problems are
   reported, never "repaired" by deleting things.
3. Run the matching playbook from ``playbooks.py``. Every mutation is
   audit-gated and tagged with the incident id
   (``k8s_tools.incident_scope``).
4. Verify with a fresh read; close the incident as resolved / needs_human /
   failed, persist it, POST it to the API (``/incidents/ingest``, retried from a
   local queue when the API is down) and to the n8n ops webhook when configured.

Cooldowns (``AGENT_OPS_COOLDOWN``, default 1800 s) and an hourly action budget
(``AGENT_OPS_MAX_ACTIONS_PER_HOUR``, default 6) stop a flapping workload from
being patched forever.

``--once`` runs a single pass (used by chaos tests and CronJob-style setups).

Endpoints are ClusterIP; the controller talks to the API over the cluster
network (``http://auton:8080``) with the same bearer token.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

import audit
import incidents
import k8s_tools
import playbooks

NAMESPACES = playbooks.ops_namespaces()
INTERVAL = max(10, int(os.environ.get("AGENT_OPS_INTERVAL", "60")))
COOLDOWN = max(0, int(os.environ.get("AGENT_OPS_COOLDOWN", "1800")))
MAX_ACTIONS_PER_HOUR = max(1, int(
    os.environ.get("AGENT_OPS_MAX_ACTIONS_PER_HOUR", "6")))
DRY_RUN = os.environ.get("AGENT_OPS_DRY_RUN", "0").strip().lower() in (
    "1", "true", "yes", "on")
API_URL = os.environ.get("AGENT_API_URL", "http://auton:8080").rstrip("/")
API_TOKEN = os.environ.get("AGENT_API_TOKEN", "").strip()
N8N_WEBHOOK = os.environ.get("N8N_OPS_WEBHOOK", "").strip()
N8N_TOKEN = os.environ.get("N8N_OPS_TOKEN", "").strip() or API_TOKEN
UI_URL = os.environ.get("AGENT_UI_URL", "http://auton.localhost").rstrip("/")
MAX_RECORDS = max(50, int(os.environ.get("AGENT_OPS_MAX_INCIDENTS", "500")))
HEARTBEAT = os.environ.get("AGENT_OPS_HEARTBEAT", "/tmp/ops-heartbeat").strip()
OPS_DIR = os.environ.get("AGENT_OPS_DIR", "").strip() or os.environ.get(
    "AGENT_AUDIT_DIR", "/workspace/.ops")
STORE = incidents.IncidentStore(os.path.join(OPS_DIR, "incidents"),
                                max_records=MAX_RECORDS)
STOP = threading.Event()


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [ops] {message}",
          flush=True)


def touch_heartbeat() -> None:
    """Liveness signal: a pass started, so the loop is not wedged."""
    if not HEARTBEAT:
        return
    try:
        pathlib.Path(HEARTBEAT).write_text(str(int(time.time())),
                                           encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Detection (rule-based; the LLM is not in this path)
# --------------------------------------------------------------------------- #

# pod reason -> (incident cause, severity, whether a playbook exists)
REASONS = {
    "OOMKilled": ("oom", "critical"),
    "CrashLoopBackOff": ("crashloop", "critical"),
    "ImagePullBackOff": ("image", "critical"),
    "ErrImagePull": ("image", "critical"),
    "CreateContainerConfigError": ("config", "critical"),
    "CreateContainerError": ("config", "critical"),
    "RunContainerError": ("config", "critical"),
    "ContainerCannotRun": ("config", "critical"),
    "InvalidImageName": ("image", "critical"),
    "Unschedulable": ("scheduling", "warning"),
    "Pending": ("scheduling", "warning"),
    "Failed": ("failed_pod", "warning"),
    "Error": ("failed_pod", "warning"),
    "NotReady": ("not_ready", "warning"),
}

REPORT_ONLY = {
    "image": ("Fix the image reference/tag (or the registry imagePullSecret), "
              "then update the Deployment."),
    "config": ("Create or fix the ConfigMap/Secret key the container "
               "references; the agent will not read or create Secrets."),
    "scheduling": ("Free node capacity or adjust requests/limits/taints. The "
                   "cluster is a single node, so capacity is physical."),
    "failed_pod": ("Inspect the pod's logs/owner; a bare pod has no controller "
                   "to recreate it."),
    "not_ready": ("Readiness probe is failing; inspect logs and the probe "
                  "configuration."),
    "unavailable": ("The Deployment has no available replicas and no failing "
                    "pod explained it; inspect events and the rollout history."),
    "statefulset": ("StatefulSet is unhealthy; its data/ordering semantics need "
                    "a human decision (no automatic rollback)."),
    "pvc": ("PVC is not Bound; check the storage class/provisioner. Never "
            "delete a PVC to 'retry' — data loss is irreversible."),
}

CAUSE_TEXT = {
    "oom": "container was OOMKilled (memory limit too low)",
    "crashloop": "container is crash-looping",
    "stuck": "pod is stuck Terminating",
    "rollout": "Deployment rollout failed / replicas could not be created",
    "unavailable": "Deployment has no available replicas",
    "replicas_zero": "Deployment is scaled to zero",
    "image": "image cannot be pulled",
    "config": "container config (ConfigMap/Secret) is missing or invalid",
    "scheduling": "pod cannot be scheduled",
    "failed_pod": "pod reached phase Failed",
    "not_ready": "container is running but not Ready",
    "statefulset": "StatefulSet is unavailable",
    "pvc": "PersistentVolumeClaim is not Bound",
}


def _finding(kind: str, name: str, cause: str, severity: str,
             observation: str, container: str = "") -> dict:
    return {"kind": kind, "name": name, "cause": cause, "severity": severity,
            "observation": observation, "container": container}


def _workload_for(snapshot_pod: dict, namespace: str) -> tuple[str, str]:
    pod, err = playbooks._get("pod", snapshot_pod["name"], namespace)
    if err:
        return "", ""
    return playbooks.workload_for_pod(pod, namespace)


def findings(snapshot: dict) -> list[dict]:
    """Deterministic list of things to act on for one namespace snapshot."""
    namespace = snapshot["namespace"]
    found: list[dict] = []
    covered: set[str] = set()

    for pod in snapshot["pods"]:
        if pod.get("protected"):
            continue
        if pod["deleting"]:
            found.append(_finding(
                "Pod", pod["name"], "stuck", "critical",
                f"pod/{pod['name']} has been Terminating for "
                f"{pod['deleting_age'] or 'unknown'}"))
            continue
        for reason in pod["reasons"]:
            mapped = REASONS.get(reason)
            if not mapped:
                continue
            cause, severity = mapped
            found.append(_finding(
                "Pod", pod["name"], cause, severity,
                f"pod/{pod['name']} reports {reason} "
                f"(restarts={pod['restarts']}, phase={pod['phase']})"))
            kind, name = _workload_for(pod, namespace)
            if kind == "Deployment" and name:
                covered.add(name)

    for name, work in (("deployments", "Deployment"),
                       ("statefulsets", "StatefulSet")):
        for item in snapshot[name]:
            if item.get("protected"):
                continue
            problems = item["problems"]
            if not problems:
                continue
            if work == "StatefulSet":
                found.append(_finding(
                    "StatefulSet", item["name"], "statefulset", "warning",
                    f"statefulset/{item['name']} is unhealthy: "
                    + ", ".join(problems)))
                continue
            if item["name"] in covered:
                continue
            if ("ProgressDeadlineExceeded" in problems
                    or "ReplicaFailure" in problems):
                found.append(_finding(
                    "Deployment", item["name"], "rollout", "critical",
                    f"deployment/{item['name']} rollout failed: "
                    + ", ".join(problems)))
            elif item.get("desired", 0) == 0:
                found.append(_finding(
                    "Deployment", item["name"], "replicas_zero", "warning",
                    f"deployment/{item['name']} is scaled to zero"))
            else:
                found.append(_finding(
                    "Deployment", item["name"], "unavailable", "warning",
                    f"deployment/{item['name']} has 0 available replicas "
                    f"(ready={item['ready']}) without a failing pod"))

    for pvc in snapshot["pvcs"]:
        if pvc.get("phase") and pvc["phase"] != "Bound":
            found.append(_finding(
                "PersistentVolumeClaim", pvc["name"], "pvc", "warning",
                f"pvc/{pvc['name']} is {pvc['phase']} "
                f"(class={pvc.get('storage_class') or '-'})"))

    unique: dict[tuple[str, str, str], dict] = {}
    for item in found:
        unique.setdefault((item["kind"], item["name"], item["cause"]), item)
    return list(unique.values())


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #

def _diagnose(item: dict, namespace: str) -> str:
    if item["kind"] != "Pod":
        return ""
    try:
        return k8s_tools.k8s_diagnose(item["name"], namespace=namespace)
    except Exception as exc:  # evidence is best effort
        return f"(diagnose failed: {type(exc).__name__}: {exc})"


def _run_playbook(item: dict, namespace: str, evidence: str) -> dict:
    cause = item["cause"]
    name = item["name"]
    if cause == "oom":
        return playbooks.fix_oom(name, namespace, container=item["container"])
    if cause == "stuck":
        return playbooks.fix_stuck(name, namespace, age="")
    if cause == "crashloop":
        return playbooks.fix_crashloop(name, namespace, evidence=evidence)
    if cause == "rollout":
        return playbooks.fix_rollout(name, namespace, reason="rollout failed")
    if cause == "replicas_zero":
        return playbooks.fix_replicas(name, namespace)
    return {"action": "report_only", "ok": False, "detail": "",
            "verification": "", "needs_human": True}


def _human_action(cause: str) -> str:
    return REPORT_ONLY.get(cause, "")


def build_report(record: dict) -> str:
    """Deterministic, short report (the UI and Slack/Telegram use this)."""
    cause = CAUSE_TEXT.get(record.get("cause"), record.get("cause") or "problem")
    lines = [f"{record.get('resource')} in {record.get('namespace')}: {cause}."]
    if record.get("observation"):
        lines.append(f"Observed: {record['observation']}")
    for action in record.get("actions") or []:
        state = "ok" if action.get("ok") else "not applied"
        lines.append(f"Action: {action.get('action')} ({state}) — "
                     f"{action.get('detail')}")
    if record.get("verification"):
        lines.append(f"Verified: {record['verification']}")
    if record.get("needs_human"):
        task = _human_action(record.get("cause", ""))
        lines.append("Human action required" + (f": {task}" if task else "."))
    if record.get("status") == "resolved":
        lines.append("Status: resolved automatically.")
    elif record.get("status") == "needs_human":
        lines.append("Status: waiting for a human.")
    elif record.get("status") == "failed":
        lines.append("Status: automatic repair failed.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _post(url: str, payload: dict, token: str = "", timeout: int = 15) -> int:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def notification_payload(record: dict) -> dict:
    last = (record.get("actions") or [{}])[-1]
    return {
        "incident_id": record.get("id"),
        "severity": record.get("severity"),
        "status": record.get("status"),
        "namespace": record.get("namespace"),
        "resource": record.get("resource"),
        "summary": record.get("observation"),
        "action": last.get("detail") or last.get("action") or "",
        "verification": record.get("verification", ""),
        "report": record.get("report", ""),
        "url": f"{UI_URL}/incidents/{record.get('id')}",
        "created_at": record.get("created_iso"),
    }


def _send_api(record: dict) -> bool:
    try:
        _post(f"{API_URL}/incidents/ingest", record, API_TOKEN)
        return True
    except Exception as exc:
        log(f"API ingest failed for {record.get('id')}: "
            f"{type(exc).__name__}: {exc}; queued locally")
        return False


def _send_n8n(record: dict) -> bool:
    if not N8N_WEBHOOK:
        return True
    try:
        _post(N8N_WEBHOOK, notification_payload(record), N8N_TOKEN)
        return True
    except Exception as exc:
        log(f"n8n webhook failed for {record.get('id')}: "
            f"{type(exc).__name__}: {exc}")
        return False


def notify(record: dict) -> dict:
    delivered = _send_api(record)
    if delivered:
        STORE.drop_report(record["id"])
    else:
        try:
            STORE.queue_report(record)
        except incidents.IncidentError as exc:
            log(f"cannot queue report {record.get('id')}: {exc}")
    _send_n8n(record)
    if delivered and not record.get("notified"):
        updated = STORE.update(record["id"], notified=True) or record
        updated["notified"] = True
        return updated
    return record


def flush_pending() -> None:
    """Retry reports the API did not accept before (API restarted, etc.)."""
    try:
        pending = STORE.pending_reports()
    except incidents.IncidentError as exc:
        log(f"cannot read pending reports: {exc}")
        return
    for record in pending:
        if _send_api(record):
            STORE.drop_report(record["id"])
            _send_n8n(record)
            log(f"delivered queued report {record.get('id')}")


# --------------------------------------------------------------------------- #
# Incident lifecycle
# --------------------------------------------------------------------------- #

def _actions_last_hour() -> int:
    cutoff = time.time() - 3600
    count = 0
    for record in STORE.list(limit=500, status="resolved,failed,needs_human"):
        if (record.get("updated_at") or 0) >= cutoff and record.get("actions"):
            count += 1
    return count


def handle(item: dict, namespace: str) -> None:
    resource = incidents.resource_key(item["kind"], item["name"])
    existing = STORE.find_recent(resource, item["cause"], within=COOLDOWN)
    if existing:
        log(f"skip {resource} {item['cause']}: incident {existing['id']} "
            f"({existing.get('status')}) is within the cooldown")
        return
    if not DRY_RUN and _actions_last_hour() >= MAX_ACTIONS_PER_HOUR:
        log(f"skip {resource} {item['cause']}: hourly action budget "
            f"({MAX_ACTIONS_PER_HOUR}) reached")
        return

    try:
        record = STORE.create(
            namespace=namespace, kind=item["kind"], name=item["name"],
            cause=item["cause"], observation=item["observation"],
            severity=item["severity"])
    except incidents.IncidentError as exc:
        log(f"cannot create incident for {resource}: {exc}")
        return
    log(f"incident {record['id']}: {resource} {item['cause']} "
        f"({item['severity']})")

    evidence = _diagnose(item, namespace)
    if evidence:
        record["evidence"] = evidence
        STORE.save(record)

    if item["cause"] in REPORT_ONLY:
        record.update({
            "status": "needs_human", "needs_human": True,
            "verification": "no automatic action taken",
            "actions": [{"action": "report_only", "ok": False,
                         "detail": _human_action(item["cause"])}],
        })
        record["report"] = build_report(record)
        STORE.save(record)
        notify(record)
        return

    plan = f"run {item['cause']} playbook on {resource}"
    if DRY_RUN:
        record.update({
            "status": "detected", "needs_human": False,
            "actions": [{"action": "dry_run", "ok": True, "detail": plan}],
            "verification": "dry run: nothing was changed",
        })
        record["report"] = build_report(record)
        STORE.save(record)
        notify(record)
        return

    record.update({"status": "acting", "attempts": (record.get("attempts") or 0) + 1})
    STORE.save(record)
    try:
        with k8s_tools.incident_scope(record["id"]):
            result = _run_playbook(item, namespace, evidence)
    except Exception as exc:
        result = {"action": "playbook_error", "ok": False,
                  "detail": f"{type(exc).__name__}: {exc}",
                  "verification": "", "needs_human": True}
        log(f"playbook for {record['id']} raised: {exc}")

    actions = record.get("actions") or []
    actions.append({"action": result.get("action"),
                    "ok": bool(result.get("ok")),
                    "detail": result.get("detail", "")})
    try:
        audit_rows = audit.incident_actions(record["id"])
        audit_ids = list(dict.fromkeys(
            row.get("audit_id") for row in audit_rows if row.get("audit_id")))
    except Exception:
        audit_ids = []
    if result.get("ok"):
        status = "resolved"
    elif result.get("needs_human"):
        status = "needs_human"
    else:
        status = "failed"
    record.update({
        "status": status,
        "needs_human": bool(result.get("needs_human")),
        "actions": actions,
        "verification": result.get("verification", ""),
        "audit_ids": audit_ids,
    })
    record["report"] = build_report(record)
    STORE.save(record)
    log(f"incident {record['id']} -> {status}: {result.get('detail')}")
    notify(record)


def run_once() -> dict:
    touch_heartbeat()
    flush_pending()
    summary = {"namespaces": list(NAMESPACES), "findings": 0, "errors": 0}
    for namespace in NAMESPACES:
        snapshot, error = k8s_tools._collect(namespace)
        if error:
            log(f"snapshot failed for {namespace}: {error}")
            summary["errors"] += 1
            continue
        items = findings(snapshot)
        summary["findings"] += len(items)
        for item in items:
            if STOP.is_set():
                return summary
            try:
                handle(item, namespace)
            except Exception as exc:
                summary["errors"] += 1
                log(f"handling {item['kind']}/{item['name']} failed: "
                    f"{type(exc).__name__}: {exc}")
    try:
        STORE.prune()
    except incidents.IncidentError as exc:
        log(f"prune failed: {exc}")
    return summary


def _stop(signum, frame):  # noqa: ARG001 - signal handler signature
    log(f"received signal {signum}; finishing the current pass")
    STOP.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true",
                        help="run a single pass and exit")
    args = parser.parse_args(argv)

    if not API_TOKEN:
        log("warning: AGENT_API_TOKEN is empty; /incidents/ingest will answer 401")
    if not k8s_tools.ENABLED:
        log("error: Kubernetes tools are disabled (AGENT_K8S_ENABLED=0)")
        return 2
    if not k8s_tools.WRITE and not DRY_RUN:
        log("warning: AGENT_K8S_WRITE=0; playbooks will refuse to mutate "
            "(use AGENT_OPS_DRY_RUN=1 for a report-only controller)")
    log(f"starting: namespaces={','.join(NAMESPACES)} interval={INTERVAL}s "
        f"cooldown={COOLDOWN}s dry_run={DRY_RUN} api={API_URL} "
        f"n8n={'on' if N8N_WEBHOOK else 'off'}")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if args.once:
        summary = run_once()
        log(f"pass complete: {json.dumps(summary)}")
        return 0

    while not STOP.is_set():
        try:
            summary = run_once()
            log(f"pass complete: {json.dumps(summary)}")
        except Exception as exc:  # a loop must never die on one bad pass
            log(f"pass failed: {type(exc).__name__}: {exc}")
        STOP.wait(INTERVAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
