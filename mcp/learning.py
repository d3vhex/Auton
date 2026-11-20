#!/usr/bin/env python3
"""The Learn stage: close the loop between a repair and the next one.

Everything up to here was a pipeline: detect → analyze → diagnose → plan →
policy check → repair → verify. This module is the feedback edge: every
verified repair writes an **outcome**, and the outcomes change *how* the next
repair of the same resource is done.

    outcome      one repair attempt and what it achieved
    profile      what the platform has learned about one resource
    adaptation   one bounded change to that profile

The learning is **automatic** (the user asked for full auto-adaptation), so
the safety envelope below is not decoration — it is the difference between a
platform that gets better and a platform that gets dangerous.

The envelope
------------
Learning may only make the platform **more cautious** or tune a **bounded
number**. It may never:

* lower a risk tier (a learned tier is ``max(base, learned)``);
* change the action of a plan (the action allowlist of :mod:`diagnosis` is
  fixed);
* widen what the platform may touch (namespaces, permissions, resources);
* exceed the hard caps below (step, cooldown, budget, escalations);
* run without an audit record (the audit gate is write-ahead and fail-closed);
* do anything at all when the policy engine is disabled or the learning
  switch is off.

The adaptations are a small, named allowlist — there is no free-form
"learning rule", and an unknown adaptation name is rejected:

===================== ====================================================
``raise_oom_step``    raise the memory bump for a workload that keeps
                      OOMing after it was "fixed" (0.25 → 0.5 → 1.0)
``raise_tier``        raise the risk tier of the next plan, so the next
                      repair of a flaky resource needs a human
``raise_cooldown``    back off longer after repeated failures
``lower_budget``      spend fewer actions per hour on a resource that
                      keeps failing
``quarantine``        stop repairing a resource altogether and hand it to
                      a human (the most conservative adaptation there is)
===================== ====================================================

Layout (under ``AGENT_LEARN_DIR``, default ``$AGENT_AUDIT_DIR/learning``)::

    out-<id>.json    one repair outcome
    lp-<id>.json     one learned profile (per namespace/resource)

The worker writes them on the shared workspace PVC, so the API reads exactly
what the worker learned. Every file is written atomically (temp file +
``os.replace``), like the incident store.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import threading
import time

import audit

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name, "1" if default else "0").strip().lower()
    return value in ("1", "true", "yes", "on")


def _int(name: str, default: int, *, minimum: int = 0,
         maximum: int = 2 ** 31) -> int:
    try:
        value = int(float(os.environ.get(name, str(default)).strip()))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _float(name: str, default: float, *, minimum: float = 0.0,
           maximum: float = 1.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


#: The master switch. Off means the module is inert: no outcomes, no
#: adaptations, and the repair path behaves exactly as it did before.
ENABLED = _flag("AGENT_LEARN_ENABLED", True)

#: How many verified outcomes are needed before anything is adapted. One
#: failure is noise; three is a pattern.
MIN_SAMPLES = _int("AGENT_LEARN_MIN_SAMPLES", 3, minimum=1, maximum=100)

#: The hard cap on the memory bump: the limit may at most double per attempt.
MAX_STEP = _float("AGENT_LEARN_MAX_STEP", 1.0, minimum=0.1, maximum=1.0)

#: The smallest bump the platform uses (the playbook default is 0.25).
MIN_STEP = 0.25

#: The hard cap on a learned cooldown (6 hours).
COOLDOWN_MAX = _int("AGENT_LEARN_COOLDOWN_MAX", 21600, minimum=60,
                    maximum=604800)

#: The floor of a learned per-resource action budget (per hour).
BUDGET_MIN = _int("AGENT_LEARN_BUDGET_MIN", 1, minimum=1, maximum=1000)

#: How many consecutive failures quarantine a resource.
MAX_ESCALATIONS = _int("AGENT_LEARN_MAX_ESCALATIONS", 3, minimum=1,
                       maximum=20)

#: How many outcome records are kept.
MAX_OUTCOMES = _int("AGENT_LEARN_MAX_OUTCOMES", 500, minimum=10, maximum=5000)

#: The adaptations learning may apply. Anything else is refused.
ADAPTATIONS = (
    "raise_oom_step",
    "raise_tier",
    "raise_cooldown",
    "lower_budget",
    "quarantine",
)

#: The base cooldown and budget a learned profile starts from.
BASE_COOLDOWN = _int("AGENT_OPS_COOLDOWN", 1800, minimum=0, maximum=604800)
BASE_BUDGET = _int("AGENT_OPS_MAX_ACTIONS_PER_HOUR", 6, minimum=1, maximum=1000)

#: The cause whose memory bump may be learned upwards.
OOM_CAUSE = "oom"

_LOCK = threading.RLock()


class LearningError(RuntimeError):
    """The learning store is unavailable (disk full, bad path …)."""


def default_dir() -> str:
    """Where the outcomes and the profiles live."""
    configured = os.environ.get("AGENT_LEARN_DIR", "").strip()
    if configured:
        return configured
    audit_dir = os.environ.get("AGENT_AUDIT_DIR", "").strip()
    if not audit_dir:
        audit_dir = os.path.join(os.getcwd(), ".ops")
    return os.path.join(audit_dir, "learning")


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or time.time()))


def profile_id(namespace: str, resource: str, cause: str = "") -> str:
    """A stable id for one (resource, cause) profile.

    The cause is part of the key on purpose: a workload that OOMs and a
    workload that crash-loops are two different problems, and the platform
    learns something different about each of them.
    """
    raw = (f"{(namespace or '').strip()}|{(resource or '').strip()}|"
           f"{(cause or '').strip().lower()}").encode("utf-8")
    return "lp-" + hashlib.sha1(raw).hexdigest()[:12]


def outcome_id() -> str:
    import uuid
    return "out-" + uuid.uuid4().hex[:12]


def clamp_step(value) -> float:
    """A learned step is always inside [MIN_STEP, MAX_STEP]."""
    try:
        step = float(value)
    except (TypeError, ValueError):
        return MIN_STEP
    return max(MIN_STEP, min(MAX_STEP, step))


def clamp_cooldown(value, base: int = BASE_COOLDOWN) -> int:
    """A learned cooldown never drops below the base and never exceeds the cap."""
    try:
        cooldown = int(value)
    except (TypeError, ValueError):
        cooldown = base
    return max(int(base), min(int(COOLDOWN_MAX), cooldown))


def clamp_budget(value, base: int = BASE_BUDGET) -> int:
    """A learned budget never exceeds the base and never drops below the floor."""
    try:
        budget = int(value)
    except (TypeError, ValueError):
        budget = base
    return max(int(BUDGET_MIN), min(int(base), budget))


def clamp_tier(value, base: int) -> int:
    """A learned tier may only raise the base tier (never lower it)."""
    try:
        tier = int(value)
    except (TypeError, ValueError):
        tier = base
    return max(int(base), min(3, tier))


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #

class LearningStore:
    """Outcomes and profiles on disk (atomic writes, no external service)."""

    def __init__(self, directory: str | None = None, max_outcomes: int | None = None):
        self.directory = pathlib.Path(directory or default_dir())
        self.max_outcomes = max(10, int(max_outcomes or MAX_OUTCOMES))
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LearningError(
                f"cannot create learning dir {self.directory}: {exc}") from exc

    # -- files ------------------------------------------------------------- #
    def _path(self, kind: str, identifier: str) -> pathlib.Path:
        safe = (identifier or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", safe):
            raise LearningError(f"invalid {kind} id")
        return self.directory / f"{safe}.json"

    def _write(self, record: dict) -> dict:
        record["updated_at"] = round(time.time(), 3)
        record["updated_iso"] = _iso(record["updated_at"])
        path = self._path(record["id"][:3], record["id"])
        tmp = path.with_suffix(f".tmp-{os.getpid()}")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(record, indent=2, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise LearningError(f"cannot write {record['id']}: {exc}") from exc
        return record

    # -- outcomes ---------------------------------------------------------- #
    def save_outcome(self, outcome: dict) -> dict:
        record = dict(outcome or {})
        record.setdefault("id", outcome_id())
        record.setdefault("ts", round(time.time(), 3))
        record["ts_iso"] = _iso(record.get("ts"))
        record = self._write(record)
        self._prune_outcomes()
        return record

    def get_outcome(self, outcome_id: str) -> dict | None:
        try:
            with open(self._path("out", outcome_id), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def outcomes(self, limit: int = 50, namespace: str = "", resource: str = "",
                 cause: str = "") -> list[dict]:
        """Filtered, newest-first outcome list."""
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit = 50
        rows: list[dict] = []
        for path in sorted(self.directory.glob("out-*.json")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rows.append(json.load(fh))
            except (OSError, json.JSONDecodeError):
                continue
        if namespace:
            rows = [r for r in rows if r.get("namespace") == namespace]
        if resource:
            rows = [r for r in rows if r.get("resource") == resource]
        if cause:
            rows = [r for r in rows if r.get("cause") == cause]
        rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
        return rows[:limit]

    def _prune_outcomes(self) -> int:
        """Keep the store bounded; the oldest outcomes go first."""
        paths = sorted(self.directory.glob("out-*.json"),
                       key=lambda p: p.stat().st_mtime)
        removed = 0
        while len(paths) > self.max_outcomes:
            path = paths.pop(0)
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    # -- profiles ---------------------------------------------------------- #
    def get_profile(self, namespace: str, resource: str,
                    cause: str = "") -> dict | None:
        try:
            with open(self._path("lp", profile_id(namespace, resource, cause)),
                      "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def save_profile(self, profile: dict) -> dict:
        record = dict(profile or {})
        record["id"] = profile_id(record.get("namespace", ""),
                                  record.get("resource", ""),
                                  record.get("cause", ""))
        record = self._write(record)
        return record

    def profiles(self) -> list[dict]:
        rows: list[dict] = []
        for path in sorted(self.directory.glob("lp-*.json")):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    rows.append(json.load(fh))
            except (OSError, json.JSONDecodeError):
                continue
        rows.sort(key=lambda r: float(r.get("updated_at") or 0), reverse=True)
        return rows

    # -- reporting --------------------------------------------------------- #
    def stats(self) -> dict:
        """Aggregate outcomes into per-cause statistics."""
        outcomes = self.outcomes(limit=500)
        by_cause: dict[str, dict] = {}
        for row in outcomes:
            cause = str(row.get("cause") or "unknown")
            entry = by_cause.setdefault(cause, {
                "cause": cause, "attempts": 0, "successes": 0, "failures": 0,
                "needs_human": 0, "total_seconds": 0.0,
            })
            entry["attempts"] += 1
            if row.get("ok"):
                entry["successes"] += 1
            else:
                entry["failures"] += 1
            if row.get("needs_human"):
                entry["needs_human"] += 1
            entry["total_seconds"] += float(row.get("duration_seconds") or 0.0)
        for entry in by_cause.values():
            attempts = max(1, int(entry["attempts"]))
            entry["mean_seconds"] = round(entry["total_seconds"] / attempts, 2)
            entry["success_rate"] = round(entry["successes"] / attempts, 3)
        return {
            "outcomes": len(outcomes),
            "profiles": len(self.profiles()),
            "causes": sorted(by_cause.values(), key=lambda e: -e["attempts"]),
            "adaptations": ADAPTATIONS,
            "limits": {
                "min_samples": MIN_SAMPLES,
                "max_step": MAX_STEP,
                "cooldown_max": COOLDOWN_MAX,
                "budget_min": BUDGET_MIN,
                "max_escalations": MAX_ESCALATIONS,
            },
            "enabled": ENABLED,
        }


# --------------------------------------------------------------------------- #
# The adaptation engine
# --------------------------------------------------------------------------- #

def summarise(outcomes: list[dict]) -> dict:
    """The numbers an adaptation decision is made from."""
    rows = list(outcomes or [])
    successes = [r for r in rows if r.get("ok")]
    failures = [r for r in rows if not r.get("ok")]
    # A recurrence is an outcome for a cause that was already repaired
    # successfully before: the same problem came back.
    seen_success = False
    recurrences = 0
    for row in sorted(rows, key=lambda r: float(r.get("ts") or 0)):
        if row.get("ok"):
            seen_success = True
        elif seen_success:
            recurrences += 1
    consecutive = 0
    for row in sorted(rows, key=lambda r: float(r.get("ts") or 0), reverse=True):
        if row.get("ok"):
            break
        consecutive += 1
    return {
        "attempts": len(rows),
        "successes": len(successes),
        "failures": len(failures),
        "consecutive_failures": consecutive,
        "recurrences": recurrences,
    }


def _blank_profile(namespace: str, resource: str, cause: str = "") -> dict:
    return {
        "id": profile_id(namespace, resource, cause),
        "namespace": namespace,
        "resource": resource,
        "cause": cause,
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "consecutive_failures": 0,
        "recurrences": 0,
        "memory_step": MIN_STEP,
        "tier": 0,
        "cooldown_seconds": BASE_COOLDOWN,
        "budget_per_hour": BASE_BUDGET,
        "quarantined": False,
        "escalations": 0,
        "adaptations": [],
        "created_at": round(time.time(), 3),
    }


def plan_adaptations(profile: dict, outcomes: list[dict]) -> list[dict]:
    """What *would* be adapted for this resource. Pure function.

    Returns a list of ``{"name", "from", "to", "reason"}`` entries; the caller
    applies them (audit-gated). The names are always in :data:`ADAPTATIONS`.

    The trigger signals are deliberately simple:

    * ``attempts`` — how often this resource was repaired for this cause.
      The memory bump grows once the platform has tried three times (a single
      failure is noise); the risk tier is raised after twice as many, so a
      flaky resource ends up needing a human.
    * ``consecutive_failures`` — failures in a row. Two thresholds act on it:
      the cooldown grows and the per-resource budget shrinks, and enough
      failures in a row quarantine the resource entirely.
    """
    stats = summarise(outcomes)
    changes: list[dict] = []

    # 1. A workload that keeps OOMing gets a bigger bump each time.
    if (stats["attempts"] >= MIN_SAMPLES
            and profile.get("cause") == OOM_CAUSE
            and float(profile.get("memory_step") or MIN_STEP) < MAX_STEP):
        old = float(profile.get("memory_step") or MIN_STEP)
        new = clamp_step(max(old * 2, MIN_STEP))
        if new > old:
            changes.append({
                "name": "raise_oom_step", "from": old, "to": new,
                "reason": (f"the workload was repaired for an OOM "
                           f"{stats['attempts']} time(s) and kept OOMing; the "
                           f"memory bump grows from {int(old * 100)}% to "
                           f"{int(new * 100)}%"),
            })

    # 2. The same problem keeps coming back: raise the tier so the next
    #    repair needs a human instead of running unattended.
    if stats["attempts"] >= MIN_SAMPLES * 2:
        old = int(profile.get("tier") or 0)
        new = min(3, old + 1)
        if new > old:
            changes.append({
                "name": "raise_tier", "from": old, "to": new,
                "reason": (f"the same fault was repaired "
                           f"{stats['attempts']} time(s); the next repair of "
                           f"this resource needs a human"),
            })

    # 3. Repeated failures: back off for longer.
    if stats["consecutive_failures"] >= MIN_SAMPLES:
        old = int(profile.get("cooldown_seconds") or BASE_COOLDOWN)
        new = clamp_cooldown(old * 2)
        if new > old:
            changes.append({
                "name": "raise_cooldown", "from": old, "to": new,
                "reason": (f"{stats['consecutive_failures']} repairs failed in "
                           f"a row; the cooldown grows to {new}s"),
            })

    # 4. Repeated failures: spend fewer actions on this resource.
    if stats["consecutive_failures"] >= MIN_SAMPLES:
        old = int(profile.get("budget_per_hour") or BASE_BUDGET)
        new = clamp_budget(old - 1)
        if new < old:
            changes.append({
                "name": "lower_budget", "from": old, "to": new,
                "reason": (f"{stats['consecutive_failures']} repairs failed in "
                           f"a row; at most {new} action(s)/hour for this "
                           f"resource"),
            })

    # 5. Enough failures: stop repairing this resource entirely.
    if (stats["consecutive_failures"] >= MAX_ESCALATIONS
            and not profile.get("quarantined")):
        changes.append({
            "name": "quarantine", "from": False, "to": True,
            "reason": (f"{stats['consecutive_failures']} repairs failed in a "
                       f"row; the resource is handed to a human and is not "
                       f"repaired automatically any more"),
        })

    for change in changes:
        if change["name"] not in ADAPTATIONS:
            raise LearningError(f"unknown adaptation: {change['name']}")
    return changes


def apply_change(profile: dict, change: dict) -> dict:
    """Apply one adaptation to a profile. The values are clamped again here."""
    name = str(change.get("name") or "")
    if name not in ADAPTATIONS:
        raise LearningError(f"unknown adaptation: {name}")
    if name == "raise_oom_step":
        profile["memory_step"] = clamp_step(change.get("to"))
    elif name == "raise_tier":
        profile["tier"] = clamp_tier(change.get("to"),
                                     int(profile.get("tier") or 0))
    elif name == "raise_cooldown":
        profile["cooldown_seconds"] = clamp_cooldown(change.get("to"))
    elif name == "lower_budget":
        profile["budget_per_hour"] = clamp_budget(change.get("to"))
    elif name == "quarantine":
        profile["quarantined"] = True
    profile["escalations"] = int(profile.get("escalations") or 0) + 1
    history = profile.setdefault("adaptations", [])
    history.append({
        "ts": round(time.time(), 3),
        "name": name,
        "from": change.get("from"),
        "to": change.get("to"),
        "reason": str(change.get("reason") or "")[:300],
    })
    # The history is bounded: the profile must stay readable.
    if len(history) > 50:
        del history[:-50]
    return profile


# --------------------------------------------------------------------------- #
# The service layer (what the controller calls)
# --------------------------------------------------------------------------- #

class NullStore:
    """A store that keeps nothing.

    It is used when the learning directory cannot be created (a pod without
    the workspace volume, for example). Learning then behaves as if it were
    switched off instead of breaking the repair path.
    """

    def get_profile(self, namespace: str, resource: str,
                    cause: str = "") -> dict | None:
        return None

    def save_profile(self, profile: dict) -> dict:
        return dict(profile or {})

    def profiles(self) -> list[dict]:
        return []

    def outcomes(self, limit: int = 50, namespace: str = "", resource: str = "",
                 cause: str = "") -> list[dict]:
        return []

    def save_outcome(self, outcome: dict) -> dict:
        return dict(outcome or {})


class LearningService:
    """Records outcomes and applies the adaptations — audit-gated.

    Every adaptation is written to the audit log **before** it is applied, and
    the adaptation is refused when the record cannot be written. That keeps
    "the platform changed its mind" as auditable as "the platform changed the
    cluster".
    """

    def __init__(self, store: LearningStore | None = None,
                 enabled: bool | None = None):
        if store is None:
            try:
                store = LearningStore()
            except LearningError as exc:
                # No learning directory: the platform keeps its old behaviour
                # instead of failing to start.
                print(f"[learning] store unavailable, learning is inert: {exc}",
                      flush=True)
                store = NullStore()
                enabled = False
        self.store = store
        self.enabled = ENABLED if enabled is None else bool(enabled)

    # -- reads ------------------------------------------------------------- #
    def profile(self, namespace: str, resource: str,
                cause: str = "") -> dict:
        """The learned profile of a resource (a blank one when unknown)."""
        profile = self.store.get_profile(namespace, resource, cause)
        if not profile:
            return _blank_profile(namespace, resource, cause)
        return profile

    def learned_tier(self, namespace: str, resource: str, base: int,
                     cause: str = "") -> int:
        """The effective tier: the learned tier may only raise the base."""
        return clamp_tier(self.profile(namespace, resource, cause).get("tier"),
                          base)

    def learned_step(self, namespace: str, resource: str,
                     cause: str = "") -> float:
        return clamp_step(self.profile(namespace, resource, cause).get("memory_step"))

    def learned_cooldown(self, namespace: str, resource: str,
                         base: int = BASE_COOLDOWN, cause: str = "") -> int:
        profile = self.profile(namespace, resource, cause)
        if not profile.get("cooldown_seconds"):
            return int(base)
        return clamp_cooldown(profile.get("cooldown_seconds"), base)

    def learned_budget(self, namespace: str, resource: str,
                       base: int = BASE_BUDGET, cause: str = "") -> int:
        profile = self.profile(namespace, resource, cause)
        if not profile.get("budget_per_hour"):
            return int(base)
        return clamp_budget(profile.get("budget_per_hour"), base)

    def quarantined(self, namespace: str, resource: str,
                    cause: str = "") -> bool:
        """True when the resource was handed to a human after repeated failures.

        The quarantine is stored on the *cause* profile, but a resource that
        was quarantined for one cause is not repaired for another cause
        either: the platform has already decided it needs a human.
        """
        profile = self.profile(namespace, resource, cause)
        if profile.get("quarantined"):
            return True
        for known in self.store.profiles():
            if (known.get("namespace") == namespace
                    and known.get("resource") == resource
                    and known.get("quarantined")):
                return True
        return False

    def recent_actions(self, namespace: str, resource: str,
                       window: int = 3600) -> int:
        """How many repairs this resource got in the last ``window`` seconds.

        This is the per-resource budget: the global hourly budget stops the
        platform from chewing through the cluster, this one stops it from
        chewing through a single workload.
        """
        try:
            window = max(1, int(window))
        except (TypeError, ValueError):
            window = 3600
        cutoff = time.time() - window
        count = 0
        for row in self.store.outcomes(limit=500, namespace=namespace,
                                       resource=resource):
            try:
                if float(row.get("ts") or 0) >= cutoff:
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    # -- writes ------------------------------------------------------------ #
    def record_outcome(self, outcome: dict) -> dict | None:
        """Store one repair outcome. Returns None when learning is off."""
        if not self.enabled:
            return None
        try:
            return self.store.save_outcome(dict(outcome or {}))
        except LearningError:
            # A learning failure must never break the repair path.
            return None

    def adapt(self, namespace: str, resource: str, cause: str = "",
              policy_mode: str = "safe-auto", actor: str = "worker") -> list[dict]:
        """Apply the adaptations the outcomes justify. Returns what changed.

        The policy is respected: in ``disabled`` mode nothing is adapted (the
        off switch stops the whole platform), and in ``observe-only`` mode the
        outcomes are recorded but nothing is applied.
        """
        if not self.enabled:
            return []
        mode = str(policy_mode or "").strip().lower()
        if mode in ("disabled", "observe-only"):
            return []

        cause = str(cause or "").strip().lower()
        outcomes = self.store.outcomes(limit=200, namespace=namespace,
                                       resource=resource, cause=cause)
        if not outcomes:
            return []
        profile = self.profile(namespace, resource, cause)
        profile["cause"] = cause
        stats = summarise(outcomes)
        profile.update({
            "attempts": stats["attempts"],
            "successes": stats["successes"],
            "failures": stats["failures"],
            "consecutive_failures": stats["consecutive_failures"],
            "recurrences": stats["recurrences"],
        })

        changes = plan_adaptations(profile, outcomes)
        applied: list[dict] = []
        for change in changes:
            audit_id = ""
            try:
                # Write-ahead: no audit record, no adaptation.
                audit_id = audit.begin(
                    "learn_adapt",
                    {"namespace": namespace, "resource": resource,
                     "cause": cause, "adaptation": change["name"],
                     "from": change.get("from"), "to": change.get("to")},
                    plan=f"learning: {change['name']} — {change.get('reason', '')}",
                    actor=actor,
                    evidence=(f"{stats['attempts']} attempts, "
                              f"{stats['successes']} successes, "
                              f"{stats['failures']} failures, "
                              f"{stats['recurrences']} recurrence(s)"),
                )
            except audit.AuditError:
                # Fail closed: the platform keeps its old behaviour.
                return applied
            try:
                apply_change(profile, change)
                self.store.save_profile(profile)
            except LearningError:
                return applied
            applied.append(change)
            try:
                audit.finish(audit_id, status="ok", actor=actor,
                             result=f"{change['name']}: {change.get('from')} -> "
                                    f"{change.get('to')}")
            except audit.AuditError:
                pass
        return applied
