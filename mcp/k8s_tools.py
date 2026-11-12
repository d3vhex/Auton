#!/usr/bin/env python3
"""Kubernetes tools for the Auton agent (kubectl wrapper).

The pod runs with the ``auton-agent`` ServiceAccount; the namespace-scoped Role
is the security boundary, **not** this module. What this module adds is:

  * a pinned namespace allowlist — the model can target ``auton`` and
    ``auton-apps``; a stray ``-n kube-system`` is rejected before kubectl runs
  * argument validation — resource/name go through a whitelist regex, so model
    output cannot smuggle extra kubectl flags
  * a write gate (``AGENT_K8S_WRITE=0`` makes every mutating tool refuse)
  * a **write-ahead audit**: every mutation records its intent first and is
    refused if that record cannot be written (see ``audit.py``)
  * diagnostics that do the boring collection in code (``k8s_snapshot``,
    ``k8s_diagnose``) so a 7B model reasons over a compact summary instead of
    parsing 200 lines of JSON
  * clipped output — a list of 200 pods must not eat the context window

Environment:
  AGENT_NAMESPACE   primary namespace (default "auton")
  AGENT_NAMESPACES  comma list the tools may target (default: AGENT_NAMESPACE)
  AGENT_K8S_ENABLED "0" disables every tool (default "1")
  AGENT_K8S_WRITE   "1" enables apply/patch/scale/rollout/delete/exec (default "0")
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

import audit
import contextvars

NAMESPACE = os.environ.get("AGENT_NAMESPACE", "auton")
NAMESPACES = tuple(dict.fromkeys(
    [NAMESPACE] + [n.strip() for n in
                   os.environ.get("AGENT_NAMESPACES", "").split(",") if n.strip()]
))
ENABLED = os.environ.get("AGENT_K8S_ENABLED", "1").strip().lower() not in (
    "0", "false", "off", "no", ""
)
WRITE = os.environ.get("AGENT_K8S_WRITE", "0").strip().lower() in (
    "1", "true", "yes", "on"
)
MAX_CHARS = int(os.environ.get("AGENT_MAX_OUTPUT_CHARS", "8000"))
KUBECTL = shutil.which("kubectl") or "kubectl"

# Resource may be a comma list ("pods,svc") or "deployments.apps".
_RESOURCE_RE = re.compile(r"^[a-z][a-z0-9.-]*(/[a-z0-9][a-z0-9.-]*)?$")
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_./-]+=[A-Za-z0-9_./-]+(,[A-Za-z0-9_./-]+=[A-Za-z0-9_./-]+)*$")
_OUTPUTS = {"": "", "wide": "wide", "yaml": "yaml", "json": "json", "name": "name"}
_SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
_KUBECONFIG_PATH: str | None = None

# The ops controller wraps each repair in this scope so every action it takes is
# stored on the incident (audit records carry the same id). The model's own tool
# calls run without a scope, so they are unaffected.
_INCIDENT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "auton_incident_id", default="")


class incident_scope:
    """``with incident_scope("inc-123"):`` tags every audited mutation."""

    def __init__(self, incident_id: str):
        self.incident_id = incident_id or ""
        self._token = None

    def __enter__(self):
        self._token = _INCIDENT.set(self.incident_id)
        return self

    def __exit__(self, *exc):
        _INCIDENT.reset(self._token)
        return False


def _err(message: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "error": message, "hint": hint})


def _clip(text: str) -> str:
    """Head+tail clip: the start shows the resource, the end shows the error."""
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return text
    head = MAX_CHARS * 2 // 3
    tail = MAX_CHARS - head
    return (text[:head] + f"\n… ({len(text) - MAX_CHARS} chars clipped) …\n"
            + text[-tail:])


def _check(value: str, pattern: re.Pattern, what: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    if not pattern.match(value):
        return f"invalid {what} {value!r}"
    return None


def _kubeconfig_args() -> list[str]:
    """Explicit in-cluster kubeconfig, or [] to use the caller's setup.

    kubectl's implicit in-cluster loading silently falls back to
    ``http://localhost:8080`` when ``--request-timeout`` is non-zero (verified
    with kubectl 1.34 and 1.35 on this cluster) — and :8080 inside the agent pod
    is the agent's own authenticated API, so the error is a confusing 401.
    Writing the standard in-cluster config to a file removes the ambiguity.
    """
    global _KUBECONFIG_PATH
    if os.environ.get("KUBECONFIG"):
        return []  # host CLI run with an explicit kubeconfig
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "")
    if not (host and port and os.path.isfile(f"{_SA_DIR}/token")):
        return []
    if _KUBECONFIG_PATH is None:
        path = os.path.join(tempfile.gettempdir(), "auton-kubeconfig")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "apiVersion: v1\nkind: Config\n"
                "clusters:\n- name: in-cluster\n  cluster:\n"
                f"    server: https://{host}:{port}\n"
                f"    certificate-authority: {_SA_DIR}/ca.crt\n"
                "contexts:\n- name: in-cluster\n  context:\n"
                f"    cluster: in-cluster\n    user: agent\n"
                f"    namespace: {NAMESPACE}\n"
                "current-context: in-cluster\n"
                "users:\n- name: agent\n  user:\n"
                f"    tokenFile: {_SA_DIR}/token\n"
            )
        os.chmod(path, 0o600)
        _KUBECONFIG_PATH = path
    return ["--kubeconfig", _KUBECONFIG_PATH]


def _target_ns(namespace: str) -> tuple[str | None, str | None]:
    """Resolve a tool's `namespace` argument against the allowlist."""
    ns = (namespace or "").strip()
    if not ns:
        return NAMESPACE, None
    if ns not in NAMESPACES:
        return None, _err(
            f"namespace {ns!r} is not allowed",
            f"The agent may only target: {', '.join(NAMESPACES)}.",
        )
    return ns, None


def _execute(args: list[str], stdin: str | None = None, timeout: int = 30,
             namespace: str | None = None) -> tuple[str, str | None]:
    """Run kubectl once; return ``(stdout, error_json)``.

    ``error_json`` is None on success. Raw output (no clipping) so callers that
    need JSON can parse it; ``_run`` clips for the model.
    """
    if not ENABLED:
        return "", _err("Kubernetes tools are disabled.",
                        "Set AGENT_K8S_ENABLED=1 in the agent deployment.")
    if shutil.which("kubectl") is None:
        return "", _err("kubectl is not installed in this image.",
                        "Rebuild the image with kubectl (see mcp/Dockerfile).")
    cmd = [KUBECTL, *_kubeconfig_args(), "--namespace",
           namespace or NAMESPACE, *args]
    try:
        proc = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", _err(f"kubectl timed out after {timeout}s: {' '.join(args)}",
                        "Narrow the request (a specific pod, fewer logs).")
    except OSError as exc:
        return "", _err(f"could not run kubectl: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return "", _err(
            f"kubectl exited {proc.returncode}: {_clip(detail)[:1500]}",
            "Check the resource name with k8s_get(); 'NotFound' means it "
            "does not exist, 'Forbidden' means RBAC denies the verb.")
    return proc.stdout or "", None


def _run(args: list[str], stdin: str | None = None, timeout: int = 30,
         namespace: str | None = None) -> str:
    out, error = _execute(args, stdin=stdin, timeout=timeout, namespace=namespace)
    if error:
        return error
    out = out.strip()
    return _clip(out) if out else "(ok, no output)"


def _query(args: list[str], namespace: str | None = None,
           timeout: int = 30) -> tuple[object | None, str | None]:
    """Run a ``-o json`` read and parse it; return ``(data, error_json)``."""
    out, error = _execute(args, timeout=timeout, namespace=namespace)
    if error:
        return None, error
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        return None, _err(f"could not parse kubectl JSON: {exc}",
                          "Re-run k8s_get with output=json to inspect it.")


def _require_write(action: str) -> str | None:
    if not WRITE:
        return _err(f"{action} is disabled (read-only Kubernetes mode).",
                    "Set AGENT_K8S_WRITE=1 in the agent deployment.")
    return None


def _audited(action: str, argv: list[str], *, stdin: str | None = None,
             timeout: int = 60, namespace: str | None = None,
             args: dict | None = None, plan: str = "", dry_run: str = "",
             evidence: str = "", incident_id: str = "") -> str:
    """Run a mutation, but only after its intent is durably recorded.

    If the audit log is unavailable the mutation is refused: "nothing without a
    record" has to be enforced in code, not in the prompt.
    """
    payload = {"argv": argv, **(args or {})}
    incident_id = incident_id or _INCIDENT.get()
    try:
        audit_id = audit.begin(action, payload, plan=plan, dry_run=dry_run,
                               actor="agent", incident_id=incident_id,
                               evidence=evidence)
    except audit.AuditError as exc:
        return _err(
            f"refused {action}: the audit log is not writable ({exc})",
            "Mutations are blocked without an audit record. Fix "
            "AGENT_AUDIT_DIR / the workspace volume and retry.",
        )
    out = _run(argv, stdin=stdin, timeout=timeout, namespace=namespace)
    failed = out.startswith('{"ok": false')
    try:
        audit.finish(audit_id, result=out,
                     status="error" if failed else "ok",
                     incident_id=incident_id)
    except audit.AuditError:
        pass  # already printed CRITICAL; the action itself cannot be undone
    return out


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #

def k8s_get(resource: str, name: str = "", selector: str = "",
            output: str = "", namespace: str = "") -> str:
    """List or read Kubernetes resources in an allowed namespace.

    resource: pods, deployments, svc, configmaps, pvc, events, jobs, ...
    Leave name empty to list; use selector like app=auton. output: wide|yaml|json.
    """
    bad = _check(resource, _RESOURCE_RE, "resource") or _check(name, _NAME_RE, "name")
    if bad:
        return _err(bad, "Use a plain resource name such as 'pods' or 'deployments'.")
    if selector and not _SELECTOR_RE.match(selector.strip()):
        return _err(f"invalid selector {selector!r}", "Use key=value or k1=v1,k2=v2.")
    if output not in _OUTPUTS:
        return _err(f"invalid output {output!r}", "Use one of: wide, yaml, json, name.")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    args = ["get", resource.strip()]
    if name.strip():
        args.append(name.strip())
    if selector.strip():
        args += ["--selector", selector.strip()]
    if _OUTPUTS[output]:
        args += ["--output", _OUTPUTS[output]]
    args.append("--request-timeout=20s")
    return _run(args, namespace=ns)


def k8s_describe(resource: str, name: str = "", namespace: str = "") -> str:
    """Describe a resource (includes recent events for pods/deployments)."""
    bad = _check(resource, _RESOURCE_RE, "resource") or _check(name, _NAME_RE, "name")
    if bad:
        return _err(bad, "Example: k8s_describe('pods', 'auton-abc123').")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    args = ["describe", resource.strip()]
    if name.strip():
        args.append(name.strip())
    args.append("--request-timeout=20s")
    return _run(args, namespace=ns)


def k8s_logs(pod: str, container: str = "", tail: int = 100,
             previous: bool = False, namespace: str = "") -> str:
    """Read pod logs (last `tail` lines; previous=true for the crashed attempt)."""
    bad = _check(pod, _NAME_RE, "pod") or _check(container, _NAME_RE, "container")
    if bad:
        return _err(bad, "Get the pod name from k8s_get('pods').")
    if not pod.strip():
        return _err("pod is required.")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    tail = max(1, min(int(tail), 2000))
    args = ["logs", pod.strip(), "--tail", str(tail)]
    if container.strip():
        args += ["--container", container.strip()]
    if previous:
        args.append("--previous")
    return _run(args, timeout=60, namespace=ns)


# --------------------------------------------------------------------------- #
# Write tools (gated by AGENT_K8S_WRITE)
# --------------------------------------------------------------------------- #

def k8s_apply(manifest: str, namespace: str = "") -> str:
    """Apply a YAML/JSON manifest (create or update). Omit metadata.namespace."""
    blocked = _require_write("k8s_apply")
    if blocked:
        return blocked
    if not manifest.strip():
        return _err("manifest is empty.")
    if len(manifest) > 200_000:
        return _err("manifest is too large (limit 200 KB).")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    digest = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    return _audited(
        "k8s_apply", ["apply", "-f", "-"], stdin=manifest, timeout=60,
        namespace=ns, plan="apply manifest",
        args={"manifest_sha256": digest, "manifest_bytes": len(manifest),
              "manifest_head": manifest[:1200]},
    )


def k8s_patch(resource: str, name: str, patch: str,
              patch_type: str = "strategic", namespace: str = "") -> str:
    """Patch one resource with a JSON patch (strategic|merge|json).

    Example: k8s_patch("deployment", "api", '{"spec":{"replicas":2}}').
    """
    blocked = _require_write("k8s_patch")
    if blocked:
        return blocked
    bad = _check(resource, _RESOURCE_RE, "resource") or _check(name, _NAME_RE, "name")
    if bad or not resource.strip() or not name.strip():
        return _err(bad or "resource and name are required.",
                    "Example: k8s_patch('deployments', 'api', '{\"spec\":{\"replicas\":2}}').")
    if patch_type not in ("strategic", "merge", "json"):
        return _err(f"invalid patch_type {patch_type!r}",
                    "Use strategic, merge or json.")
    if not patch.strip():
        return _err("patch is empty.", "Pass a JSON object, e.g. '{\"spec\":{\"replicas\":2}}'.")
    if len(patch) > 20_000:
        return _err("patch is too large (limit 20 KB).")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    return _audited(
        "k8s_patch",
        ["patch", resource.strip(), name.strip(), f"--type={patch_type}", "-p", patch],
        timeout=60, namespace=ns, plan=f"{patch_type} patch {resource}/{name}",
        args={"resource": resource.strip(), "name": name.strip(),
              "patch_type": patch_type, "patch": patch[:2000]},
    )


def k8s_scale(deployment: str, replicas: int, namespace: str = "") -> str:
    """Scale a deployment/statefulset to `replicas`."""
    blocked = _require_write("k8s_scale")
    if blocked:
        return blocked
    bad = _check(deployment, _RESOURCE_RE, "deployment")
    if bad:
        return _err(bad, "Example: k8s_scale('deployment/auton', 0).")
    try:
        replicas = int(replicas)
    except (TypeError, ValueError):
        return _err(f"invalid replicas {replicas!r}", "Pass an integer, e.g. 1.")
    if not 0 <= replicas <= 10:
        return _err(f"replicas out of range: {replicas}", "Use 0-10.")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    return _audited(
        "k8s_scale",
        ["scale", deployment.strip(), f"--replicas={replicas}",
         "--request-timeout=20s"],
        timeout=60, namespace=ns, plan=f"scale {deployment} to {replicas}",
        args={"deployment": deployment.strip(), "replicas": replicas},
    )


def k8s_rollout(action: str, deployment: str = "", namespace: str = "") -> str:
    """Rollout control: status | restart | undo on a deployment."""
    action = (action or "status").strip().lower()
    if action not in ("status", "restart", "undo"):
        return _err(f"invalid action {action!r}", "Use status, restart or undo.")
    if action != "undo":
        bad = _check(deployment, _RESOURCE_RE, "deployment")
        if bad or not deployment.strip():
            return _err(bad or "deployment is required.",
                        "Example: k8s_rollout('restart', 'deployment/auton').")
    if action != "status":
        blocked = _require_write(f"k8s_rollout {action}")
        if blocked:
            return blocked
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    args = ["rollout", action]
    if deployment.strip():
        args.append(deployment.strip())
    if action == "status":
        args.append("--timeout=120s")
    if action == "status":
        return _run(args, timeout=150, namespace=ns)
    return _audited("k8s_rollout", args, timeout=150, namespace=ns,
                    plan=f"rollout {action} {deployment}",
                    args={"action": action, "deployment": deployment.strip()})


def k8s_delete(resource: str, name: str, confirm: bool = False,
               namespace: str = "") -> str:
    """Delete a resource. Requires confirm=true — a typo should not delete a deployment."""
    blocked = _require_write("k8s_delete")
    if blocked:
        return blocked
    bad = _check(resource, _RESOURCE_RE, "resource") or _check(name, _NAME_RE, "name")
    if bad:
        return _err(bad, "Example: k8s_delete('pods', 'auton-abc123', confirm=True).")
    if not resource.strip() or not name.strip():
        return _err("resource and name are required.",
                    "Deleting a whole resource type is never allowed.")
    if resource.strip().split("/")[0] in ("pvc", "persistentvolumeclaims",
                                          "persistentvolumeclaim"):
        # Data loss is irreversible; report it and let a human decide.
        return _err("deleting PersistentVolumeClaims is disabled.",
                    "Explain in your report why the PVC must be deleted; a human "
                    "can run: kubectl -n <ns> delete pvc <name>")
    if not confirm:
        return _err("refusing to delete without confirm=true.",
                    "Re-issue the call with confirm=true if this is intended.")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    return _audited(
        "k8s_delete",
        ["delete", resource.strip(), name.strip(), "--request-timeout=30s"],
        timeout=90, namespace=ns, plan=f"delete {resource}/{name}",
        args={"resource": resource.strip(), "name": name.strip()},
    )


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #

# Waiting/terminated reasons that mean "this container is not healthy".
_BAD_REASONS = {
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull",
    "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
    "ContainerCannotRun", "InvalidImageName", "OOMKilled", "Error",
    "NotReady", "Unschedulable", "Pending", "Failed",
}


def _age_seconds(when: str | None) -> float | None:
    """Kubernetes timestamp -> age in seconds; None when unknown."""
    if not when:
        return None
    raw = when.replace("Z", "+00:00")
    try:
        then = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=_dt.timezone.utc)
    seconds = (now - then).total_seconds()
    return 0.0 if seconds < 0 else seconds


def _fmt_age(when: str | None) -> str:
    """Kubernetes timestamp -> compact age ("3m", "2h"); '' when unknown."""
    seconds = _age_seconds(when)
    if seconds is None:
        return ""
    seconds = int(seconds)
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _owner(pod: dict) -> tuple[str, str]:
    refs = (pod.get("metadata") or {}).get("ownerReferences") or []
    if not refs:
        return "", ""
    return refs[0].get("kind", ""), refs[0].get("name", "")


def _pod_containers(pod: dict) -> list[dict]:
    return (pod.get("spec") or {}).get("containers") or []


def _protected(meta: dict) -> bool:
    labels = (meta or {}).get("labels") or {}
    return str(labels.get("auton.io/protect", "")).strip().lower() in (
        "1", "true", "yes", "on")


def _pod_summary(pod: dict) -> dict:
    """Flatten the parts of a Pod that matter for triage."""
    meta = pod.get("metadata") or {}
    status = pod.get("status") or {}
    statuses = (status.get("containerStatuses") or [])
    total = len(_pod_containers(pod)) or len(statuses)
    ready = sum(1 for c in statuses if c.get("ready"))
    restarts = sum(int(c.get("restartCount") or 0) for c in statuses)
    reasons: list[str] = []
    last_terminated = ""
    for c in statuses:
        state = c.get("state") or {}
        waiting = (state.get("waiting") or {}).get("reason") or ""
        terminated = state.get("terminated") or {}
        previous = (c.get("lastState") or {}).get("terminated") or {}
        if waiting in _BAD_REASONS:
            reasons.append(waiting)
        if terminated.get("reason") in _BAD_REASONS:
            reasons.append(terminated["reason"])
            last_terminated = terminated.get("reason", "")
        if previous and not last_terminated:
            last_terminated = previous.get("reason", "")
            if previous.get("reason") in ("OOMKilled", "Error"):
                reasons.append(previous["reason"])
        if not c.get("ready") and status.get("phase") == "Running":
            if waiting or terminated:
                pass
            elif "NotReady" not in reasons:
                reasons.append("NotReady")
    for cond in status.get("conditions") or []:
        if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
            reasons.append("Unschedulable")
    if status.get("phase") == "Pending" and not reasons:
        reasons.append("Pending")
    kind, owner = _owner(pod)
    started = status.get("startTime") or meta.get("creationTimestamp")
    return {
        "name": meta.get("name", ""),
        "phase": status.get("phase", "Unknown"),
        "ready": f"{ready}/{total}",
        "restarts": restarts,
        "reasons": list(dict.fromkeys(reasons)),
        "owner": f"{kind}/{owner}" if owner else "",
        "node": (pod.get("spec") or {}).get("nodeName", ""),
        "started": _fmt_age(started),
        "age_seconds": _age_seconds(started),
        "deleting": bool(meta.get("deletionTimestamp")),
        "deleting_age": _fmt_age(meta.get("deletionTimestamp")),
        "deleting_seconds": _age_seconds(meta.get("deletionTimestamp")),
        "last_terminated": last_terminated,
        "protected": _protected(meta),
    }


def _deploy_summary(deploy: dict) -> dict:
    meta = deploy.get("metadata") or {}
    status = deploy.get("status") or {}
    conds = {c.get("type"): c for c in status.get("conditions") or []}
    problems: list[str] = []
    avail = int(status.get("availableReplicas") or 0)
    desired = int((deploy.get("spec") or {}).get("replicas") or 0)
    if avail < desired:
        problems.append("Unavailable")
    failing = conds.get("Progressing") or {}
    if failing.get("status") == "False":
        problems.append(failing.get("reason") or "ProgressDeadlineExceeded")
    if any(c.get("type") == "ReplicaFailure" and c.get("status") == "True"
           for c in status.get("conditions") or []):
        problems.append("ReplicaFailure")
    return {
        "name": meta.get("name", ""),
        "ready": f"{int(status.get('readyReplicas') or 0)}/{desired}",
        "updated": int(status.get("updatedReplicas") or 0),
        "available": avail,
        "desired": desired,
        "problems": problems,
        "protected": _protected(meta),
    }


def _event_summary(event: dict) -> dict:
    obj = event.get("involvedObject") or {}
    last = (event.get("lastTimestamp") or event.get("eventTime")
            or (event.get("metadata") or {}).get("creationTimestamp") or "")
    return {
        "type": event.get("type", ""),
        "reason": event.get("reason", ""),
        "object": f"{obj.get('kind', '')}/{obj.get('name', '')}".strip("/"),
        "message": (event.get("message") or "").strip(),
        "count": int(event.get("count") or 1),
        "age": _fmt_age(last),
        "last": last,
    }


def _collect(namespace: str) -> tuple[dict | None, str | None]:
    """Gather the raw snapshot for one namespace (no formatting)."""
    pods, error = _query(["get", "pods", "-o", "json", "--request-timeout=20s"],
                         namespace=namespace)
    if error:
        return None, error
    deployments, _ = _query(
        ["get", "deployments", "-o", "json", "--request-timeout=20s"],
        namespace=namespace)
    statefulsets, _ = _query(
        ["get", "statefulsets", "-o", "json", "--request-timeout=20s"],
        namespace=namespace)
    pvcs, _ = _query(["get", "pvc", "-o", "json", "--request-timeout=20s"],
                     namespace=namespace)
    events, _ = _query(
        ["get", "events", "--field-selector", "type=Warning", "-o", "json",
         "--request-timeout=20s"], namespace=namespace)
    return {
        "namespace": namespace,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(
            timespec="seconds"),
        "pods": [_pod_summary(p) for p in (pods or {}).get("items") or []],
        "deployments": [_deploy_summary(d)
                        for d in (deployments or {}).get("items") or []],
        "statefulsets": [_deploy_summary(s)
                         for s in (statefulsets or {}).get("items") or []],
        "pvcs": [
            {
                "name": (p.get("metadata") or {}).get("name", ""),
                "phase": (p.get("status") or {}).get("phase", ""),
                "storage_class": (p.get("spec") or {}).get("storageClassName", ""),
            }
            for p in (pvcs or {}).get("items") or []
        ],
        "events": [_event_summary(e)
                   for e in (events or {}).get("items") or []],
    }, None


def _snapshot_problems(snap: dict) -> list[str]:
    """Deterministic list of things a human would call 'broken'."""
    problems: list[str] = []
    for pod in snap["pods"]:
        bad = [r for r in pod["reasons"] if r in _BAD_REASONS]
        if bad:
            problems.append(f"pod/{pod['name']}: {', '.join(bad)}"
                            f" (restarts={pod['restarts']})")
        elif pod["deleting"]:
            problems.append(f"pod/{pod['name']}: stuck Terminating "
                            f"({pod['deleting_age'] or 'unknown'})")
    for kind in ("deployments", "statefulsets"):
        for work in snap[kind]:
            if work["problems"]:
                problems.append(f"{kind[:-1]}/{work['name']}: "
                                + ", ".join(work["problems"]))
    for pvc in snap["pvcs"]:
        if pvc["phase"] and pvc["phase"] != "Bound":
            problems.append(f"pvc/{pvc['name']}: {pvc['phase']}")
    return problems


def _format_snapshot(snap: dict) -> str:
    problems = _snapshot_problems(snap)
    lines = [f"snapshot namespace={snap['namespace']} at={snap['captured_at']}"]
    if problems:
        lines.append("PROBLEMS:")
        lines += [f"  ! {p}" for p in problems]
    else:
        lines.append("PROBLEMS: none detected")

    lines.append(f"PODS ({len(snap['pods'])}):")
    for pod in snap["pods"]:
        flags = ",".join(pod["reasons"]) or "ok"
        lines.append(
            f"  - {pod['name']} phase={pod['phase']} ready={pod['ready']}"
            f" restarts={pod['restarts']} owner={pod['owner'] or '-'}"
            f" node={pod['node'] or '-'} age={pod['started'] or '?'}"
            f" [{flags}]"
            + (f" DELETING {pod['deleting_age']}" if pod["deleting"] else "")
            + (" PROTECTED" if pod["protected"] else "")
        )

    for kind, title in (("deployments", "DEPLOYMENTS"),
                        ("statefulsets", "STATEFULSETS")):
        lines.append(f"{title} ({len(snap[kind])}):")
        for work in snap[kind]:
            flags = ",".join(work["problems"]) or "ok"
            lines.append(
                f"  - {work['name']} ready={work['ready']}"
                f" updated={work['updated']} available={work['available']}"
                f" [{flags}]"
                + (" PROTECTED" if work["protected"] else "")
            )

    if snap["pvcs"]:
        lines.append(f"PVCS ({len(snap['pvcs'])}):")
        for pvc in snap["pvcs"]:
            lines.append(f"  - {pvc['name']} phase={pvc['phase']}"
                         f" class={pvc['storage_class'] or '-'}")

    lines.append(f"WARNING EVENTS ({len(snap['events'])}):")
    for event in snap["events"][:25]:
        lines.append(
            f"  - [{event['reason']}] {event['object']} x{event['count']}"
            f" {event['age']} ago: {_clip(event['message'])[:300]}"
        )
    return "\n".join(lines)


def k8s_snapshot(namespace: str = "") -> str:
    """Compact health snapshot of a namespace: pods, deployments, statefulsets,
    PVCs, warning events and a deterministic PROBLEMS list.

    Call this first whenever the user asks what is wrong or before any
    remediation. It is read-only and cheap (a few kubectl gets).
    """
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    snap, error = _collect(ns)
    if error:
        return error
    return _clip(_format_snapshot(snap))


def _pod_logs(pod: str, container: str, *, previous: bool,
              tail: int = 60, namespace: str) -> str:
    args = ["logs", pod, "--tail", str(tail)]
    if container:
        args += ["--container", container]
    if previous:
        args.append("--previous")
    out, error = _execute(args, timeout=30, namespace=namespace)
    if error:
        return "  (no logs available)"
    text = out.strip()
    if not text:
        return "  (empty)"
    return "\n".join("  " + line for line in text.splitlines()[-tail:])


def _classify(pod: dict, summary: dict, events: list[dict],
              limit: str) -> tuple[str, str]:
    """Deterministic cause -> suggested playbook. The model explains, code decides."""
    reasons = set(summary["reasons"])
    if "ImagePullBackOff" in reasons or "ErrImagePull" in reasons:
        return ("image cannot be pulled (bad name/tag, missing registry auth "
                "or wrong platform)",
                "report only: fix the image reference or imagePullSecret; "
                "do not retry blindly")
    if "CreateContainerConfigError" in reasons:
        return ("a referenced ConfigMap/Secret key is missing or invalid",
                "report only: the missing config must be created by a human")
    if "OOMKilled" in reasons or summary["last_terminated"] == "OOMKilled":
        hint = ""
        if limit:
            hint = f" current limit={limit};"
        return (f"container was OOMKilled (memory limit too low);{hint}",
                "playbook oom: raise the memory limit by 25% (max 2Gi), "
                "restart the workload, verify restarts stop")
    if "CrashLoopBackOff" in reasons:
        return ("container crashes on start (application/config error or a "
                "dependency is unreachable)",
                "playbook crashloop: read the previous logs; if the cause is "
                "config/dependency report only, otherwise delete the pod once")
    if "Unschedulable" in reasons:
        return ("pod cannot be scheduled (insufficient cpu/memory, taints or "
                "volume affinity)",
                "report only: node capacity or scheduling rules must change")
    if summary["phase"] == "Pending":
        pvc_event = any("persistentvolumeclaim" in e["message"].lower()
                        or "unbound" in e["message"].lower() for e in events)
        if pvc_event:
            return ("pod is Pending waiting for a PersistentVolumeClaim",
                    "report only: check storageclass/provisioner; do not delete "
                    "the PVC")
        return ("pod is Pending without an obvious scheduling error",
                "collect events; report only until the cause is known")
    if summary["deleting"]:
        return ("pod is stuck in Terminating (kubelet/finalizer not releasing)",
                "playbook stuck: delete with grace period 0")
    if "Failed" in reasons or summary["phase"] == "Failed":
        return ("pod reached phase Failed",
                "check logs and owner; delete/recreate only if the workload "
                "controller will recreate it")
    return ("no single known failure signature",
            "read logs and describe output, then report; do not guess a fix")


def k8s_diagnose(pod: str, namespace: str = "", container: str = "") -> str:
    """Triage one pod: status, owner, warning events, current + previous logs
    and a deterministic cause/playbook suggestion.

    Use after k8s_snapshot flags a pod. Evidence is clipped so it fits a small
    model's context. Read-only.
    """
    if not (pod or "").strip():
        return _err("pod is required.", "Get the name from k8s_snapshot().")
    bad = _check(pod, _NAME_RE, "pod") or _check(container, _NAME_RE, "container")
    if bad:
        return _err(bad, "Example: k8s_diagnose('auton-abc123').")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err

    pod_obj, error = _query(["get", "pods", pod.strip(), "-o", "json",
                             "--request-timeout=20s"], namespace=ns)
    if error:
        return error
    summary = _pod_summary(pod_obj)
    containers = _pod_containers(pod_obj)
    target = container.strip() or (
        (containers[0].get("name") if containers else ""))
    limits = ""
    for c in containers:
        if c.get("name") == target:
            limits = ((c.get("resources") or {}).get("limits") or {}).get(
                "memory", "")
            break

    events_obj, _ = _query(
        ["get", "events", "--field-selector",
         f"involvedObject.name={pod.strip()}", "-o", "json",
         "--request-timeout=20s"], namespace=ns)
    events = [_event_summary(e)
              for e in (events_obj or {}).get("items") or []]

    lines = [f"pod/{summary['name']} namespace={ns}"]
    lines.append(f"  phase={summary['phase']} ready={summary['ready']}"
                 f" restarts={summary['restarts']} node={summary['node'] or '-'}"
                 f" owner={summary['owner'] or '-'} age={summary['started'] or '?'}")
    if summary["deleting"]:
        lines.append(f"  DELETING for {summary['deleting_age'] or 'unknown'}")
    for c in containers:
        res = c.get("resources") or {}
        lines.append(f"  container {c.get('name')} image={c.get('image')}"
                     f" limits={res.get('limits') or '-'}")
    lines.append("  reasons: " + (", ".join(summary["reasons"]) or "none"))
    lines.append("events:")
    if events:
        for event in events[-12:]:
            lines.append(f"  - [{event['reason']}] x{event['count']}"
                         f" {event['age']} ago: {_clip(event['message'])[:300]}")
    else:
        lines.append("  (none)")
    lines.append(f"logs (current, {target or 'first container'}):")
    lines.append(_pod_logs(pod.strip(), container.strip(), previous=False,
                           namespace=ns))
    if summary["restarts"] > 0:
        lines.append("logs (previous container):")
        lines.append(_pod_logs(pod.strip(), container.strip(), previous=True,
                               namespace=ns))
    cause, action = _classify(pod_obj, summary, events, limits)
    lines.append(f"diagnosis: {cause}")
    lines.append(f"suggested_action: {action}")
    return _clip("\n".join(lines))


def _parse_since(value: str) -> int:
    """'90s'/'15m'/'2h'/'1d' -> seconds (default 15m, clamped 1m..24h)."""
    raw = (value or "15m").strip().lower()
    match = re.match(r"^(\d+)([smhd])$", raw)
    if not match:
        return 900
    amount = int(match.group(1))
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return max(60, min(seconds, 86400))


def k8s_events(since: str = "15m", warnings_only: bool = True,
               namespace: str = "") -> str:
    """Recent events in a namespace (newest first), filtered by age.

    since: 90s | 15m | 2h | 1d. warnings_only=false includes Normal events.
    """
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    args = ["get", "events", "-o", "json", "--request-timeout=20s"]
    if warnings_only:
        args[2:2] = ["--field-selector", "type=Warning"]
    data, error = _query(args, namespace=ns)
    if error:
        return error
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(seconds=_parse_since(since)))
    rows: list[dict] = []
    for event in (data or {}).get("items") or []:
        summary = _event_summary(event)
        try:
            when = _dt.datetime.fromisoformat(
                summary["last"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            when = None
        if when and when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        if when and when < cutoff:
            continue
        rows.append(summary)
    rows.sort(key=lambda row: row["last"], reverse=True)
    if not rows:
        return f"no events in the last {since} (namespace={ns})"
    lines = [f"{len(rows)} event(s) in the last {since} (namespace={ns}):"]
    for row in rows[:40]:
        lines.append(f"- [{row['type']}/{row['reason']}] {row['object']}"
                     f" x{row['count']} {row['age']} ago:"
                     f" {_clip(row['message'])[:300]}")
    return _clip("\n".join(lines))


def k8s_wait(resource: str, name: str, condition: str = "ready",
             timeout: int = 60, namespace: str = "") -> str:
    """Block until a condition holds: ready | available | complete | delete.

    resource is pods, deployments, statefulsets or jobs. Read-only (it only
    watches). Use after a remediation to verify it worked.
    """
    bad = _check(resource, _RESOURCE_RE, "resource") or _check(name, _NAME_RE, "name")
    if bad or not resource.strip() or not name.strip():
        return _err(bad or "resource and name are required.",
                    "Example: k8s_wait('deployments', 'api', 'available', 90).")
    condition = (condition or "ready").strip().lower()
    mapping = {
        "ready": "condition=Ready",
        "available": "condition=Available",
        "complete": "condition=Complete",
        "delete": "delete",
    }
    if condition not in mapping:
        return _err(f"invalid condition {condition!r}",
                    "Use ready, available, complete or delete.")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    try:
        timeout = max(5, min(int(timeout), 300))
    except (TypeError, ValueError):
        return _err(f"invalid timeout {timeout!r}", "Pass seconds, e.g. 60.")
    return _run(
        ["wait", resource.strip(), name.strip(), f"--for={mapping[condition]}",
         f"--timeout={timeout}s"],
        timeout=timeout + 15, namespace=ns,
    )


def k8s_exec(pod: str, command: str, container: str = "",
             timeout: int = 60, namespace: str = "") -> str:
    """Run a shell command inside a pod (audited; mutations are logged).

    command runs via `sh -c`. Keep it read-only where possible (cat, env,
    nc -z, curl -s localhost:8080/health). Read-only root filesystems may
    reject writes. Only use when logs/snapshot are not enough.
    """
    blocked = _require_write("k8s_exec")
    if blocked:
        return blocked
    bad = _check(pod, _NAME_RE, "pod") or _check(container, _NAME_RE, "container")
    if bad or not (pod or "").strip():
        return _err(bad or "pod is required.",
                    "Example: k8s_exec('auton-abc123', 'cat /etc/os-release').")
    if not (command or "").strip():
        return _err("command is required.", "Example: k8s_exec('pod', 'env').")
    if len(command) > 4000:
        return _err("command is too long (limit 4000 chars).")
    ns, ns_err = _target_ns(namespace)
    if ns_err:
        return ns_err
    try:
        timeout = max(5, min(int(timeout), 120))
    except (TypeError, ValueError):
        return _err(f"invalid timeout {timeout!r}", "Pass seconds, e.g. 30.")
    args = ["exec", pod.strip()]
    if container.strip():
        args += ["--container", container.strip()]
    args += ["--", "sh", "-c", command.strip()]
    return _audited(
        "k8s_exec", args, timeout=timeout, namespace=ns,
        plan=f"exec in {pod}: {command[:200]}",
        args={"pod": pod.strip(), "container": container.strip(),
              "command": command.strip()[:2000]},
    )


TOOLS = [k8s_get, k8s_describe, k8s_logs, k8s_snapshot, k8s_diagnose,
         k8s_events, k8s_wait, k8s_apply, k8s_patch, k8s_scale, k8s_rollout,
         k8s_delete, k8s_exec]


def register(mcp) -> None:
    """Attach every tool in this module to an MCPServer instance."""
    for fn in TOOLS:
        mcp.tool()(fn)
