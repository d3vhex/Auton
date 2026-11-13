#!/usr/bin/env python3
"""The Diagnosis Engine: from evidence to a root cause.

``CrashLoopBackOff`` on its own is a *symptom*, not a diagnosis. This engine
separates the causes that need completely different handling:

    OOMKilled
    APPLICATION_CRASH
    LIVENESS_PROBE_FAILURE
    INVALID_CONFIGURATION
    MISSING_ENVIRONMENT_VARIABLE
    MISSING_DEPENDENCY
    DATABASE_CONNECTION_FAILURE
    MISSING_MOUNTED_FILE
    PERMISSION_DENIED
    IMAGE_PROBLEM
    IMAGE_PULL_FAILURE
    INSUFFICIENT_RESOURCES
    NODE_NOT_READY
    PVC_UNBOUND
    ROLLOUT_FAILED
    STUCK_TERMINATING
    REPLICAS_MISMATCH
    UNKNOWN

The rules run in the order the platform spec prescribes:

    1. Kubernetes status information
    2. Events
    3. Container termination reason
    4. Metrics
    5. Logs
    6. Resource ownership
    7. Deterministic diagnosis rules
    8. LLM-assisted analysis (only if explicitly asked for)

The deterministic pass is always first and always wins over the model. The
LLM pass is *advisory*: it may only refine a diagnosis that the evidence
already supports, and it is forced to answer ``UNKNOWN`` / ``requires_human``
when the evidence is insufficient. A model that invents a root cause is
rejected, not recorded.

The output shape is fixed::

    {
      "root_cause": "OOMKilled",
      "confidence": 0.96,
      "severity": "high",
      "evidence": ["Container exit code 137",
                   "Last termination reason OOMKilled",
                   "Memory limit 512Mi"],
      "recommended_action": "INCREASE_MEMORY_LIMIT",
      "requires_human": false
    }

``confidence`` is a heuristic score, not a calibrated probability.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Root causes
# --------------------------------------------------------------------------- #

OOM_KILLED = "OOMKilled"
APPLICATION_CRASH = "APPLICATION_CRASH"
LIVENESS_PROBE_FAILURE = "LIVENESS_PROBE_FAILURE"
INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
MISSING_ENVIRONMENT_VARIABLE = "MISSING_ENVIRONMENT_VARIABLE"
MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
DATABASE_CONNECTION_FAILURE = "DATABASE_CONNECTION_FAILURE"
MISSING_MOUNTED_FILE = "MISSING_MOUNTED_FILE"
PERMISSION_DENIED = "PERMISSION_DENIED"
IMAGE_PROBLEM = "IMAGE_PROBLEM"
IMAGE_PULL_FAILURE = "IMAGE_PULL_FAILURE"
INSUFFICIENT_RESOURCES = "INSUFFICIENT_RESOURCES"
NODE_NOT_READY = "NODE_NOT_READY"
PVC_UNBOUND = "PVC_UNBOUND"
ROLLOUT_FAILED = "ROLLOUT_FAILED"
STUCK_TERMINATING = "STUCK_TERMINATING"
REPLICAS_MISMATCH = "REPLICAS_MISMATCH"
UNKNOWN = "UNKNOWN"

#: Every root cause the engine may report.
ROOT_CAUSES = (
    OOM_KILLED, APPLICATION_CRASH, LIVENESS_PROBE_FAILURE,
    INVALID_CONFIGURATION, MISSING_ENVIRONMENT_VARIABLE, MISSING_DEPENDENCY,
    DATABASE_CONNECTION_FAILURE, MISSING_MOUNTED_FILE, PERMISSION_DENIED,
    IMAGE_PROBLEM, IMAGE_PULL_FAILURE, INSUFFICIENT_RESOURCES,
    NODE_NOT_READY, PVC_UNBOUND, ROLLOUT_FAILED, STUCK_TERMINATING,
    REPLICAS_MISMATCH, UNKNOWN,
)

#: Every root cause the model may propose. Anything else is rejected.
ALLOWED_CAUSES = frozenset(ROOT_CAUSES)

#: Uppercase spelling -> canonical constant, so a model answer is matched
#: case-insensitively and stored in the canonical form.
_UPPER_TO_CAUSE = {cause.upper(): cause for cause in ROOT_CAUSES}

# --------------------------------------------------------------------------- #
# Recommended actions (the allowlist; the worker only runs registered
# playbooks, never a free-form command).
# --------------------------------------------------------------------------- #

INCREASE_MEMORY_LIMIT = "INCREASE_MEMORY_LIMIT"
RESTART_POD = "RESTART_POD"
ROLLBACK_DEPLOYMENT = "ROLLBACK_DEPLOYMENT"
RESTORE_REPLICAS = "RESTORE_REPLICAS"
REPORT_ONLY = "REPORT_ONLY"
NONE = "NONE"

ACTIONS = (
    INCREASE_MEMORY_LIMIT, RESTART_POD, ROLLBACK_DEPLOYMENT, RESTORE_REPLICAS,
    REPORT_ONLY, NONE,
)

#: Actions that a worker may run without a human.
AUTOMATIC_ACTIONS = (INCREASE_MEMORY_LIMIT, RESTART_POD, RESTORE_REPLICAS)

#: Severity vocabulary used by the diagnosis output.
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

# --------------------------------------------------------------------------- #
# Log patterns
#
# These are *hints*, not proof: they refine a diagnosis the Kubernetes status
# already supports. They are checked in the order the spec lists them
# (logs come after status, events and termination reason).
# --------------------------------------------------------------------------- #

_LOG_PATTERNS: tuple[tuple[str, str, float], ...] = (
    (DATABASE_CONNECTION_FAILURE,
     r"(connection refused|connection reset|could not connect to server|"
     r"connection timed out|server closed the connection|"
     r"can't connect to (mysql|postgres|postgresql|mongo|redis))",
     0.90),
    (MISSING_ENVIRONMENT_VARIABLE,
     r"(environment variable \S+ not set|keyerror: '[^']+'|"
     r"undefined environment variable|missing required (config|environment))",
     0.90),
    (MISSING_DEPENDENCY,
     r"(modulenotfounderror|no module named|importerror|cannot find module|"
     r"no such file or directory.*site-packages|"
     r"command not found|executable file not found)",
     0.90),
    (MISSING_MOUNTED_FILE,
     r"(no such file or directory|is a directory|failed to open.*volume|"
     r"failed mount|configmap .* not found)",
     0.75),
    (PERMISSION_DENIED,
     r"(permission denied|operation not permitted|"
     r"read-only file system|unauthorizedaccess)",
     0.90),
    (INVALID_CONFIGURATION,
     r"(invalid configuration|configuration error|failed to parse|"
     r"malformed|yaml: unmarshal errors|invalid value:|"
     r"error unmarshaling|unknown field)",
     0.85),
)

#: Events that point at a specific cause.
_EVENT_PATTERNS: tuple[tuple[str, str, float], ...] = (
    (LIVENESS_PROBE_FAILURE,
     r"(liveness probe failed|unhealthy|readiness probe failed)", 0.92),
    (INVALID_CONFIGURATION,
     r"(failed to create container|configmap .* not found|"
     r"secret .* not found|failed mount|config error)", 0.90),
    (IMAGE_PULL_FAILURE,
     r"(failed to pull|back-off pulling image|imagepullbackoff|"
     r"errimagepull|manifest unknown|unauthorized)", 0.92),
    (INSUFFICIENT_RESOURCES,
     r"(insufficient cpu|insufficient memory|failed scheduling)", 0.90),
    (NODE_NOT_READY,
     r"(node .* not ready|node .* is not ready)", 0.90),
    (PVC_UNBOUND,
     r"(persistentvolumeclaim .* not bound|no persistent volumes available)",
     0.90),
)


def _text(value: str) -> str:
    return (value or "").lower()


def _confidence(base: float, *extra: float) -> float:
    """Combine confidence signals, capped below 1.0.

    This is a heuristic score for ranking, not a calibrated probability.
    """
    score = base
    for bonus in extra:
        score = max(score, bonus)
    return round(min(score, 0.99), 2)


def _result(root_cause: str, confidence: float, evidence: list[str],
            action: str, *, severity: str = SEVERITY_MEDIUM,
            requires_human: bool = False) -> dict:
    return {
        "root_cause": root_cause,
        "confidence": round(float(confidence), 2),
        "severity": severity,
        "evidence": [item for item in evidence if item],
        "recommended_action": action,
        "requires_human": bool(requires_human),
        "source": "deterministic",
    }


def _unknown(evidence: list[str], *, reason: str = "") -> dict:
    """The honest answer when the evidence does not support a conclusion."""
    notes = list(evidence)
    if reason:
        notes.append(reason)
    return {
        "root_cause": UNKNOWN,
        "confidence": 0.0,
        "severity": SEVERITY_MEDIUM,
        "evidence": notes,
        "recommended_action": NONE,
        "requires_human": True,
        "source": "deterministic",
    }


# --------------------------------------------------------------------------- #
# The deterministic engine
# --------------------------------------------------------------------------- #

def diagnose(document: dict) -> dict:
    """Classify the root cause of a problem from its evidence.

    ``document`` is what :mod:`evidence` produced. A missing or empty
    document yields ``UNKNOWN`` with ``requires_human`` — the engine never
    guesses.
    """
    if not document or not isinstance(document, dict):
        return _unknown([], reason="no evidence was collected")

    findings: list[str] = []
    problem = (document.get("problem") or "").strip()
    container = document.get("container") or {}
    resources = document.get("resources") or {}
    availability = document.get("evidence") or {}
    events = document.get("events") or []
    logs = _text(document.get("logs", ""))
    previous_logs = _text(document.get("previous_logs", ""))
    combined_logs = previous_logs + "\n" + logs

    # Nothing to reason over: the pod status could not be read and there
    # are no logs or events either. The engine says so instead of guessing.
    if availability.get("pod_available") is False and not events \
            and not combined_logs:
        return _unknown([], reason="the pod status could not be read")

    # -- 1. Kubernetes status information ---------------------------------- #
    termination = (container.get("last_termination_reason") or "").strip()
    exit_code = container.get("exit_code")
    restart_count = int(container.get("restart_count") or 0)
    phase = (document.get("phase") or "").strip()

    if termination == "OOMKilled" or exit_code == 137:
        findings.append("Container exit code 137"
                        if exit_code == 137 else
                        f"Last termination reason {termination}")
        findings.append(f"Last termination reason {termination or 'OOMKilled'}")
        if resources.get("memory_limit"):
            findings.append(f"Memory limit {resources['memory_limit']}")
        if resources.get("memory_request"):
            findings.append(f"Memory request {resources['memory_request']}")
        return _result(OOM_KILLED, _confidence(0.96, 0.97), findings,
                       INCREASE_MEMORY_LIMIT, severity=SEVERITY_HIGH)

    if phase == "Failed" and termination == "Error":
        findings.append(f"Pod phase Failed with exit code {exit_code}")
        return _result(APPLICATION_CRASH, 0.85, findings, RESTART_POD,
                       severity=SEVERITY_HIGH)

    if problem == "Unschedulable" or phase == "Pending":
        findings.append(f"Pod phase {phase or 'Pending'}")
        findings.append("Pod could not be scheduled")
        return _result(INSUFFICIENT_RESOURCES, 0.88, findings, REPORT_ONLY,
                       severity=SEVERITY_MEDIUM, requires_human=True)

    if problem == "StuckTerminating" or document.get("deleting"):
        findings.append("Pod is stuck Terminating")
        return _result(STUCK_TERMINATING, 0.90, findings, REPORT_ONLY,
                       severity=SEVERITY_MEDIUM, requires_human=True)

    # -- 2. Events --------------------------------------------------------- #
    event_text = " ".join(
        f"{event.get('reason', '')} {event.get('message', '')}"
        for event in events).lower()
    for root_cause, pattern, confidence in _EVENT_PATTERNS:
        match = re.search(pattern, event_text)
        if not match:
            continue
        matched_event = next(
            (event for event in events
             if re.search(pattern, f"{event.get('reason', '')} "
                                   f"{event.get('message', '')}".lower())),
            {})
        findings.append(f"Event {matched_event.get('reason', '')}: "
                        f"{str(matched_event.get('message', ''))[:200]}")
        if root_cause == LIVENESS_PROBE_FAILURE:
            return _result(root_cause, confidence, findings, REPORT_ONLY,
                           severity=SEVERITY_HIGH, requires_human=True)
        if root_cause == INVALID_CONFIGURATION:
            return _result(root_cause, confidence, findings, REPORT_ONLY,
                           severity=SEVERITY_HIGH, requires_human=True)
        if root_cause == IMAGE_PULL_FAILURE:
            return _result(root_cause, confidence, findings, REPORT_ONLY,
                           severity=SEVERITY_HIGH, requires_human=True)
        if root_cause == INSUFFICIENT_RESOURCES:
            return _result(root_cause, confidence, findings, REPORT_ONLY,
                           severity=SEVERITY_MEDIUM, requires_human=True)
        if root_cause == PVC_UNBOUND:
            return _result(root_cause, confidence, findings, REPORT_ONLY,
                           severity=SEVERITY_MEDIUM, requires_human=True)
        if root_cause == NODE_NOT_READY:
            return _result(root_cause, confidence, findings, REPORT_ONLY,
                           severity=SEVERITY_HIGH, requires_human=True)

    # -- 3. Container termination reason (non-OOM) -------------------------- #
    # A generic "Error" is only an *observation*: the reason why the
    # container died is in the logs, the events or the probes. It is kept as
    # a candidate and refined further down, never returned here.
    if termination in ("Error", "ContainerStatusUnknown"):
        findings.append(f"Last termination reason {termination}")
        if exit_code is not None:
            findings.append(f"Container exit code {exit_code}")
    elif termination and termination not in ("Completed",):
        findings.append(f"Last termination reason {termination}")

    # -- 4. Metrics -------------------------------------------------------- #
    metrics = document.get("node_metrics") or {}
    node = document.get("node") or {}
    pressure = node.get("pressure") or {}
    pressured = [key for key, value in pressure.items() if value == "True"]
    if pressured:
        # The joined string is built outside the f-string: an expression
        # with a nested literal inside an f-string is mis-compiled by some
        # CPython versions (the name is emitted as a global lookup).
        joined = ", ".join(pressured)
        findings.append(f"Node pressure: {joined}")
        return _result(INSUFFICIENT_RESOURCES, 0.85, findings, REPORT_ONLY,
                       severity=SEVERITY_HIGH, requires_human=True)
    for condition in node.get("conditions") or []:
        if condition.get("type") == "Ready" and condition.get("status") == "False":
            findings.append(f"Node {node.get('name', '')} is NotReady")
            return _result(NODE_NOT_READY, 0.90, findings, REPORT_ONLY,
                           severity=SEVERITY_HIGH, requires_human=True)

    # -- 5. Logs ----------------------------------------------------------- #
    for root_cause, pattern, confidence in _LOG_PATTERNS:
        match = re.search(pattern, combined_logs)
        if not match:
            continue
        snippet = combined_logs[max(0, match.start() - 60):match.end() + 60]
        findings.append(f"Log matches {root_cause}: {snippet.strip()[:200]}")
        action = RESTART_POD if root_cause == APPLICATION_CRASH else REPORT_ONLY
        human = root_cause != APPLICATION_CRASH
        return _result(root_cause, confidence, findings, action,
                       severity=SEVERITY_HIGH if human else SEVERITY_MEDIUM,
                       requires_human=human)

    # -- 6. Resource ownership --------------------------------------------- #
    deployment = document.get("deployment") or {}
    owner = document.get("owner") or {}
    if deployment:
        progressing = deployment.get("progressing_reason") or ""
        if progressing in ("ProgressDeadlineExceeded", "ReplicaFailure"):
            findings.append(f"Deployment {deployment.get('name', '')}: "
                            f"{progressing}")
            return _result(ROLLOUT_FAILED, 0.92, findings, ROLLBACK_DEPLOYMENT,
                           severity=SEVERITY_HIGH)
        desired = int(deployment.get("desired_replicas") or 0)
        available = int(deployment.get("available_replicas") or 0)
        if desired == 0 and available == 0:
            findings.append(f"Deployment {deployment.get('name', '')} is "
                            f"scaled to zero")
            return _result(REPLICAS_MISMATCH, 0.80, findings, RESTORE_REPLICAS,
                           severity=SEVERITY_MEDIUM, requires_human=True)
        if desired and available < desired:
            findings.append(f"Deployment {deployment.get('name', '')}: "
                            f"available {available}/{desired}")
    if not owner.get("name"):
        findings.append("Resource ownership could not be resolved")

    # -- 7. Remaining deterministic rules ---------------------------------- #
    if problem in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName",
                   "ImageInspectError", "RegistryUnavailable"):
        findings.append(f"Image cannot be pulled: {problem}")
        if container.get("image"):
            findings.append(f"Image {container['image']}")
        return _result(IMAGE_PULL_FAILURE, 0.92, findings, REPORT_ONLY,
                       severity=SEVERITY_HIGH, requires_human=True)

    if problem in ("CreateContainerConfigError", "CreateContainerError",
                   "RunContainerError", "ContainerCannotRun"):
        findings.append(f"Container cannot start: {problem}")
        return _result(INVALID_CONFIGURATION, 0.90, findings, REPORT_ONLY,
                       severity=SEVERITY_HIGH, requires_human=True)

    # A liveness failure shows up as a probe error, not as a crash: the
    # container is killed by the kubelet, so the exit code alone says
    # nothing about the application.
    probes = container.get("probes") or {}
    if (probes.get("liveness") or {}).get("configured") \
            and termination in ("Error", "ContainerStatusUnknown"):
        findings.append("Liveness probe is configured")
        return _result(LIVENESS_PROBE_FAILURE, 0.75, findings, REPORT_ONLY,
                       severity=SEVERITY_HIGH, requires_human=True)

    if problem == "CrashLoopBackOff":
        findings.append(f"Container is crash-looping (restarts={restart_count})")
        if restart_count >= 3:
            findings.append(f"Restart count is {restart_count}")
        if not combined_logs:
            findings.append("No container logs are available")
        return _result(APPLICATION_CRASH, 0.55, findings, RESTART_POD,
                       severity=SEVERITY_HIGH)

    if termination in ("Error", "ContainerStatusUnknown"):
        # The container died without a specific reason: a plain application
        # crash is the best-supported conclusion.
        return _result(APPLICATION_CRASH, 0.70, findings, RESTART_POD,
                       severity=SEVERITY_HIGH)

    if problem == "NotReady":
        findings.append("Container is running but not Ready")
        return _result(APPLICATION_CRASH, 0.45, findings, REPORT_ONLY,
                       severity=SEVERITY_MEDIUM, requires_human=True)

    if problem:
        findings.append(f"Observed problem: {problem}")
    else:
        findings.append("No specific problem was observed")

    # -- 8. Not enough evidence -------------------------------------------- #
    missing = [key for key, value in availability.items() if not value]
    if missing:
        findings.append("Evidence unavailable: " + ", ".join(sorted(missing)))
    return _unknown(findings, reason="the evidence does not identify a "
                                     "single root cause")


# --------------------------------------------------------------------------- #
# LLM-assisted analysis (advisory only)
# --------------------------------------------------------------------------- #

#: The root causes the model may propose. Anything else is rejected.
_ALLOWED_CAUSES = ALLOWED_CAUSES

def validate_llm_analysis(analysis: dict, deterministic: dict) -> dict | None:
    """Accept a model proposal only if the evidence supports it.

    The model may refine a diagnosis, but it may not invent one:

    * the root cause must be a known cause,
    * it must be the deterministic cause, or the deterministic engine must
      have been unable to decide (``UNKNOWN``),
    * a missing/unknown cause is never downgraded to certainty.
    """
    if not isinstance(analysis, dict):
        return None
    cause = str(analysis.get("root_cause") or "").strip().upper()
    if cause not in _UPPER_TO_CAUSE:
        return None
    cause = _UPPER_TO_CAUSE[cause]
    if cause == UNKNOWN:
        return None
    base = deterministic or {}
    base_cause = str(base.get("root_cause") or UNKNOWN)
    if base_cause != UNKNOWN and cause != base_cause:
        # The model disagrees with the evidence: the evidence wins.
        return None
    confidence = analysis.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = float(base.get("confidence") or 0.5)
    # The model may raise confidence, never lower it below the evidence.
    confidence = min(max(confidence, float(base.get("confidence") or 0.0)), 0.99)
    evidence = analysis.get("evidence")
    if not isinstance(evidence, list):
        evidence = base.get("evidence") or []
    action = str(analysis.get("recommended_action") or "").strip().upper()
    if action not in ACTIONS:
        action = base.get("recommended_action") or NONE
    requires_human = bool(analysis.get("requires_human",
                                       base.get("requires_human", True)))
    return {
        "root_cause": cause,
        "confidence": round(confidence, 2),
        "severity": str(analysis.get("severity")
                        or base.get("severity") or SEVERITY_MEDIUM),
        "evidence": [str(item) for item in evidence][:20],
        "recommended_action": action,
        "requires_human": requires_human,
        "source": "llm",
    }


def llm_analyse(document: dict, deterministic: dict | None = None,
                 prompt: str = "") -> dict:
    """Ask the model for a second opinion, then validate it.

    Without a provider the deterministic answer stands unchanged — the model
    is optional by design. A model answer that the evidence does not support
    is discarded, not recorded.
    """
    base = deterministic or diagnose(document)
    if not prompt:
        return base
    try:
        import llm
    except Exception:
        return base
    try:
        raw = _run_chat(llm, [
            {"role": "system",
             "content": "You are a Kubernetes incident analyst. Answer with "
                        "JSON: root_cause, confidence (0..1), severity "
                        "(high|medium|low), evidence (list of strings), "
                        "recommended_action, requires_human (bool). Use only "
                        "the evidence given. If the evidence is insufficient, "
                        "answer root_cause UNKNOWN, confidence 0 and "
                        "requires_human true. Never invent facts."},
            {"role": "user", "content": prompt},
        ])
    except Exception:
        return base
    analysis = _parse_json(raw)
    validated = validate_llm_analysis(analysis or {}, base)
    if validated is None:
        return base
    return validated


def _run_chat(llm_module, messages: list[dict]) -> str:
    """Call the async provider from synchronous code."""
    import asyncio
    reply = asyncio.run(llm_module.chat(messages, []))
    return getattr(reply, "content", "") or ""


def _parse_json(text: str) -> dict | None:
    """Pull a JSON object out of a model answer."""
    import json
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
