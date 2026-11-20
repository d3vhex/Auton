#!/usr/bin/env python3
"""The Kubernetes operations this platform performs, over direct HTTPS.

:mod:`k8s_api` is the transport (TLS, token, HTTP verbs). This module is the
*vocabulary*: the handful of operations the tools, the evidence collector and
the playbooks actually need, expressed in terms of the API server rather than
in terms of kubectl's command line.

Each operation returns a plain, honest result:

* a **dict** for the object-level operations (get, list, patch, ...);
* a **string** for the report-level operations (describe, rollout status);
* an :class:`k8s_api.ApiError` when the API server refused — the caller
  decides whether that is a fallback to kubectl or a final answer.

The one thing this module deliberately does **not** do is decide whether a
mutation is allowed. That is :mod:`k8s_tools`' job: the namespace allowlist,
the write gate and the write-ahead audit all live there, and this module is
only reached after they have said yes.
"""
from __future__ import annotations

import json

import k8s_api
from k8s_api import ApiError

#: The restart annotation kubectl uses for ``rollout restart``.
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

#: The annotation that carries a Deployment's revision.
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _client():
    return k8s_api.client()


def _resource(name: str):
    resource = k8s_api.resolve_resource(name)
    if resource is None:
        raise ApiError(f"unknown resource {name!r}")
    return resource


def _labels_of(obj: dict) -> dict:
    return ((obj or {}).get("metadata") or {}).get("labels") or {}


def _selector_params(selector: str) -> dict:
    """Turn ``key=value,k2=v2`` into the API's ``labelSelector``."""
    if not selector:
        return {}
    return {"labelSelector": selector.strip()}


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def get_object(resource: str, name: str, namespace: str = "") -> dict:
    """GET one object."""
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    entry = _resource(resource)
    if not k8s_api.valid_name(name):
        raise ApiError(f"invalid object name {name!r}")
    return api.get_json(entry.object(name, namespace))


def list_objects(resource: str, namespace: str = "", selector: str = "",
                 field_selector: str = "") -> list[dict]:
    """LIST a collection, optionally filtered by labels."""
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    entry = _resource(resource)
    params = _selector_params(selector)
    if field_selector:
        params["fieldSelector"] = field_selector
    items, _ = api.list(entry.collection(namespace), params)
    return items


def pod_logs(pod: str, container: str = "", tail: int = 100,
             previous: bool = False, namespace: str = "",
             timestamps: bool = False) -> str:
    """The last ``tail`` lines of a container's log."""
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    params = {"tailLines": str(max(1, min(int(tail), 2000)))}
    if container:
        params["container"] = container
    if previous:
        params["previous"] = "true"
    if timestamps:
        params["timestamps"] = "true"
    path = f"/api/v1/namespaces/{namespace}/pods/{pod}/log"
    return api.get_text(path, params, timeout=60)


def events(namespace: str = "", name: str = "", kind: str = "",
           warning_only: bool = False, since_seconds: int = 0) -> list[dict]:
    """The events of a namespace, or of one object."""
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    selector = {}
    if name:
        selector["involvedObject.name"] = name
    if kind:
        selector["involvedObject.kind"] = kind
    if warning_only:
        selector["type"] = "Warning"
    items = list_objects("events", namespace, field_selector=_selector(
        selector))
    if since_seconds:
        items = _recent_events(items, since_seconds)
    return items


def _selector(pairs: dict) -> str:
    return ",".join(f"{key}={value}" for key, value in sorted(pairs.items()))


def _recent_events(items: list[dict], seconds: int) -> list[dict]:
    """Keep only the events of the last ``seconds``."""
    import datetime as dt
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    out = []
    for event in items:
        stamp = ((event.get("lastTimestamp")
                  or event.get("eventTime")
                  or event.get("firstTimestamp")) or "")
        try:
            when = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        if when >= cutoff:
            out.append(event)
    return out


def top_nodes() -> list[dict]:
    """The node metrics (``kubectl top nodes``)."""
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    document = api.get_json("/apis/metrics.k8s.io/v1beta1/nodes")
    items = document.get("items")
    return items if isinstance(items, list) else []


def top_pods(namespace: str = "") -> list[dict]:
    """The pod metrics (``kubectl top pods``)."""
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    if namespace:
        path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods"
    else:
        path = "/apis/metrics.k8s.io/v1beta1/pods"
    document = api.get_json(path)
    items = document.get("items")
    return items if isinstance(items, list) else []


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def apply_manifest(manifest: str, namespace: str = "") -> dict:
    """Apply a manifest with **server-side apply**.

    The API server accepts YAML directly (``application/apply-patch+yaml``),
    so no YAML parser is needed on this side — the cluster does the parsing.
    """
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    kind, name, api_version = _manifest_identity(manifest)
    entry = _resource(kind)
    path = entry.object(name, namespace)
    params = {"fieldManager": "auton", "force": "true"}
    return api.patch(path, manifest, patch_type="apply", params=params,
                     timeout=60)


def _manifest_identity(manifest: str) -> tuple[str, str, str]:
    """``(resource, name, apiVersion)`` from a manifest, without a YAML parser.

    The API server can apply the manifest itself; this only needs enough of it
    to know **which** object to address. The kind and the name are read from
    the few lines that carry them, which is why the manifest is rejected when
    it does not have a plain ``kind:`` and ``name:``.
    """
    kind = ""
    name = ""
    api_version = ""
    for raw in str(manifest).splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        stripped = line.strip()
        if line[:1] not in (" ", "\t") and ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key == "kind" and not kind:
                kind = value
            elif key == "apiVersion" and not api_version:
                api_version = value
            elif key == "metadata" and not name:
                continue
        if not name and stripped.startswith("name:"):
            name = stripped.partition(":")[2].strip().strip('"').strip("'")
    if not kind:
        raise ApiError("the manifest has no 'kind:' line")
    if not name:
        raise ApiError("the manifest has no metadata.name")
    resource = k8s_api.resource_for_kind(kind)
    if resource is None:
        # Fall back on the lowercase kind ("Deployment" -> "deployment").
        resource = k8s_api.resolve_resource(kind.lower())
    if resource is None:
        raise ApiError(f"unknown kind {kind!r}")
    return resource.plural, name, api_version


def patch_object(resource: str, name: str, patch: str, patch_type: str = "merge",
                 namespace: str = "") -> dict:
    """PATCH one object."""
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    entry = _resource(resource)
    kinds = {"strategic": "strategic", "merge": "merge", "json": "json",
             "apply": "apply"}
    return api.patch(entry.object(name, namespace), patch,
                     patch_type=kinds.get(patch_type, "merge"), timeout=60)


def scale(resource: str, name: str, replicas: int, namespace: str = "") -> dict:
    """Scale a workload through its ``scale`` subresource."""
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    entry = _resource(resource)
    path = f"{entry.object(name, namespace)}/scale"
    body = json.dumps({"spec": {"replicas": int(replicas)}})
    return api.patch(path, body, patch_type="merge", timeout=60)


def delete_object(resource: str, name: str, namespace: str = "",
                  propagation: str = "background") -> dict:
    """DELETE one object."""
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    entry = _resource(resource)
    body = json.dumps({"apiVersion": "v1", "kind": "DeleteOptions",
                       "propagationPolicy": propagation})
    return api.delete(entry.object(name, namespace), body=body, timeout=90)


def rollout_restart(resource: str, name: str, namespace: str = "") -> dict:
    """Restart a workload (``kubectl rollout restart``)."""
    import datetime as dt
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    entry = _resource(resource)
    current = get_object(resource, name, namespace)
    template = ((current.get("spec") or {}).get("template") or {})
    annotations = dict(((template.get("metadata") or {}).get("annotations")
                        or {}))
    annotations[RESTART_ANNOTATION] = dt.datetime.now(
        dt.timezone.utc).isoformat(timespec="seconds")
    patch = json.dumps({"spec": {"template": {"metadata": {"annotations":
                                                      annotations}}}})
    return api.patch(entry.object(name, namespace), patch, patch_type="merge",
                     timeout=60)


def rollout_undo(resource: str, name: str, namespace: str = "",
                 to_revision: int = 0) -> dict:
    """Roll a workload back to its previous revision.

    kubectl finds the previous ReplicaSet through the ``revision`` annotation;
    this does the same and patches the pod template back.
    """
    api = _client()
    if api is None:
        raise ApiError(k8s_api.client_error() or "no API client")
    entry = _resource(resource)
    current = get_object(resource, name, namespace)
    owner = ((current.get("metadata") or {}).get("name") or "")
    sets = list_objects("replicasets", namespace)
    # The ReplicaSets of one Deployment carry a "revision" annotation; match
    # them by owner reference instead of by selector (the annotation value is
    # the *revision*, not the name).
    owned = []
    for item in sets:
        for owner_ref in ((item.get("metadata") or {}).get("ownerReferences")
                          or []):
            if owner_ref.get("kind") == "Deployment" and \
                    owner_ref.get("name") == owner:
                owned.append(item)
                break
    revisions = []
    for item in owned:
        annotations = ((item.get("metadata") or {}).get("annotations") or {})
        try:
            revision = int(annotations.get(REVISION_ANNOTATION, "0"))
        except (TypeError, ValueError):
            revision = 0
        revisions.append((revision, item))
    revisions.sort(key=lambda pair: pair[0])
    if not revisions:
        raise ApiError("there is no revision to roll back to")
    if to_revision:
        target = next((item for revision, item in revisions
                       if revision == int(to_revision)), None)
        if target is None:
            raise ApiError(f"revision {to_revision} does not exist")
    else:
        if len(revisions) < 2:
            raise ApiError("there is no previous revision to roll back to")
        target = revisions[-2][1]
    template = (target.get("spec") or {}).get("template") or {}
    patch = json.dumps({"spec": {"template": template}})
    return api.patch(entry.object(name, namespace), patch, patch_type="merge",
                     timeout=60)


def rollout_status(resource: str, name: str, namespace: str = "") -> str:
    """A short rollout report for one workload."""
    current = get_object(resource, name, namespace)
    metadata = current.get("metadata") or {}
    spec = current.get("spec") or {}
    status = current.get("status") or {}
    desired = int(spec.get("replicas") or 0)
    ready = int(status.get("readyReplicas") or 0)
    updated = int(status.get("updatedReplicas") or 0)
    available = int(status.get("availableReplicas") or 0)
    conditions = status.get("conditions") or []
    lines = [
        f"{resource}/{metadata.get('name')} namespace={metadata.get('namespace')}",
        f"  desired={desired} updated={updated} ready={ready} "
        f"available={available}",
    ]
    for condition in conditions:
        lines.append(f"  {condition.get('type')}: "
                     f"{condition.get('status')} "
                     f"({condition.get('reason') or '-'})")
        if condition.get("message"):
            lines.append(f"    {condition['message']}")
    if desired == ready == updated == available and desired > 0:
        lines.append("  rollout status: complete")
    else:
        lines.append("  rollout status: in progress")
    return "\n".join(lines)


def exec_command(pod: str, command: str, container: str = "",
                 namespace: str = "", timeout: int = 60) -> tuple[str, str]:
    """Run ``sh -c <command>`` in a pod (the v4.channel.k8s.io WebSocket)."""
    return k8s_api.exec_command(pod, ["sh", "-c", command],
                                 container=container, namespace=namespace,
                                 timeout=timeout)


# --------------------------------------------------------------------------- #
# Describe
# --------------------------------------------------------------------------- #

def describe(resource: str, name: str, namespace: str = "") -> str:
    """A kubectl-like ``describe`` of one object.

    kubectl's own text is a Go template; this renders the same *facts* from
    the object itself, plus the events of that object. The model reads the
    report, not the format.
    """
    current = get_object(resource, name, namespace)
    metadata = current.get("metadata") or {}
    spec = current.get("spec") or {}
    status = current.get("status") or {}
    lines = [f"Name:         {metadata.get('name')}",
             f"Namespace:    {metadata.get('namespace') or namespace}",
             f"Kind:         {current.get('kind') or resource}",
             f"Labels:       {_render_map(metadata.get('labels'))}",
             f"Annotations:  {_render_map(metadata.get('annotations'))}"]
    if spec.get("replicas") is not None:
        lines.append(f"Replicas:     {spec.get('replicas')} desired, "
                     f"{status.get('readyReplicas') or 0} ready")
    if spec.get("selector"):
        lines.append(f"Selector:     {_render_map(spec.get('selector'))}")
    template = spec.get("template") or {}
    for container in ((template.get("spec") or {}).get("containers") or []):
        limits = ((container.get("resources") or {}).get("limits") or {})
        requests = ((container.get("resources") or {}).get("requests") or {})
        lines.append(f"Container:    {container.get('name')}")
        lines.append(f"  Image:      {container.get('image')}")
        if limits:
            lines.append(f"  Limits:     {_render_map(limits)}")
        if requests:
            lines.append(f"  Requests:   {_render_map(requests)}")
    for key in ("phase", "reason", "message", "nodeName", "podIP", "hostIP"):
        if status.get(key):
            lines.append(f"Status:       {key}={status.get(key)}")
    for condition in (status.get("conditions") or []):
        lines.append(f"Condition:    {condition.get('type')}="
                     f"{condition.get('status')} "
                     f"({condition.get('reason') or '-'})")
        if condition.get("message"):
            lines.append(f"              {condition['message']}")
    for container_status in (status.get("containerStatuses") or []):
        state = container_status.get("state") or {}
        waiting = (state.get("waiting") or {}).get("reason")
        terminated = (state.get("terminated") or {}).get("reason")
        lines.append(f"Container:    {container_status.get('name')} "
                     f"ready={container_status.get('ready')} "
                     f"restarts={container_status.get('restartCount')} "
                     f"state={waiting or terminated or 'running'}")
    # The events of this object.
    try:
        object_events = events(namespace, name=name)
    except ApiError:
        object_events = []
    lines.append("Events:")
    if object_events:
        for event in object_events[-12:]:
            lines.append(f"  {event.get('type', '?')} "
                         f"{event.get('reason', '?')} "
                         f"{event.get('count', 1)}x "
                         f"{event.get('message', '')}")
    else:
        lines.append("  <none>")
    return "\n".join(lines)


def _render_map(values) -> str:
    if not values:
        return "<none>"
    return ", ".join(f"{key}={value}" for key, value in sorted(values.items()))
