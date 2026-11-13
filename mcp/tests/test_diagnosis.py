"""Tests for the Diagnosis Engine (deterministic rules + advisory LLM)."""
from __future__ import annotations

import pytest

import diagnosis


def _document(**overrides) -> dict:
    """A minimal evidence document with everything available."""
    document = {
        "resource": {"kind": "Pod", "name": "api-1", "namespace": "auton-apps",
                     "uid": "uid-1"},
        "problem": "CrashLoopBackOff",
        "phase": "Running",
        "restart_count": 12,
        "container": {
            "name": "api",
            "restart_count": 12,
            "last_termination_reason": "Error",
            "exit_code": 1,
            "state": "waiting",
            "ready": False,
            "image": "app:1",
            "resources": {"memory_request": "256Mi", "memory_limit": "512Mi",
                          "cpu_request": "100m", "cpu_limit": "500m"},
            "probes": {"liveness": {"configured": False},
                       "readiness": {"configured": False},
                       "startup": {"configured": False}},
            "image_pull_secret_configured": False,
        },
        "resources": {"memory_request": "256Mi", "memory_limit": "512Mi",
                      "cpu_request": "100m", "cpu_limit": "500m"},
        "owner": {"kind": "Deployment", "name": "api"},
        "probes": {},
        "node": {"name": "node-1", "available": True, "allocatable": {},
                 "capacity": {}, "conditions": [], "pressure": {}},
        "node_metrics": {},
        "events": [],
        "logs": "",
        "previous_logs": "",
        "deployment": {},
        "evidence": {"pod_available": True, "logs_available": True,
                     "previous_logs_available": True,
                     "events_available": True, "node_available": True,
                     "node_metrics_available": True,
                     "owner_available": True},
        "errors": [],
    }
    document.update(overrides)
    return document


def _container(**overrides) -> dict:
    container = _document()["container"]
    container.update(overrides)
    return container


# --------------------------------------------------------------------------- #
# The output contract
# --------------------------------------------------------------------------- #

def test_result_has_the_required_shape():
    result = diagnosis.diagnose(_document())
    for key in ("root_cause", "confidence", "severity", "evidence",
                "recommended_action", "requires_human"):
        assert key in result
    assert result["root_cause"] in diagnosis.ROOT_CAUSES
    assert result["recommended_action"] in diagnosis.ACTIONS
    assert 0.0 <= result["confidence"] <= 1.0


def test_no_evidence_is_unknown_and_needs_a_human():
    result = diagnosis.diagnose({})
    assert result["root_cause"] == diagnosis.UNKNOWN
    assert result["confidence"] == 0.0
    assert result["recommended_action"] == diagnosis.NONE
    assert result["requires_human"] is True


# --------------------------------------------------------------------------- #
# Root cause discrimination
# --------------------------------------------------------------------------- #

def test_oomkill_is_recognised():
    document = _document(problem="OOMKilled",
                         container=_container(last_termination_reason="OOMKilled",
                                              exit_code=137))
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.OOM_KILLED
    assert result["recommended_action"] == diagnosis.INCREASE_MEMORY_LIMIT
    assert result["requires_human"] is False
    assert result["confidence"] >= 0.9
    assert any("137" in item for item in result["evidence"])
    assert any("512Mi" in item for item in result["evidence"])


def test_exit_code_137_alone_is_enough_for_an_oom():
    document = _document(container=_container(last_termination_reason="",
                                              exit_code=137))
    assert diagnosis.diagnose(document)["root_cause"] == diagnosis.OOM_KILLED


def test_a_database_connection_failure_is_not_an_oom():
    document = _document(
        logs="psycopg2 OperationalError: connection refused\n"
             "could not connect to server: Connection refused")
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.DATABASE_CONNECTION_FAILURE
    assert result["requires_human"] is True
    assert result["recommended_action"] == diagnosis.REPORT_ONLY


def test_a_missing_environment_variable_is_its_own_cause():
    document = _document(
        logs="RuntimeError: environment variable DATABASE_URL not set")
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.MISSING_ENVIRONMENT_VARIABLE
    assert result["requires_human"] is True


def test_a_missing_dependency_is_its_own_cause():
    document = _document(logs="ModuleNotFoundError: No module named 'requests'")
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.MISSING_DEPENDENCY


def test_a_permission_error_is_its_own_cause():
    document = _document(logs="PermissionError: [Errno 13] Permission denied: "
                              "'/data/config'")
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.PERMISSION_DENIED


def test_a_missing_mounted_file_is_its_own_cause():
    document = _document(logs="open /etc/config/app.yaml: no such file or "
                              "directory")
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.MISSING_MOUNTED_FILE


def test_an_invalid_configuration_is_its_own_cause():
    document = _document(logs="yaml: unmarshal errors:\n  line 3: "
                              "invalid value: foo")
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.INVALID_CONFIGURATION


def test_a_liveness_probe_failure_comes_from_the_events():
    document = _document(events=[{
        "reason": "Unhealthy", "message": "Liveness probe failed: "
                                          "HTTP probe failed with statuscode: 503",
        "type": "Warning", "count": 4, "last_timestamp": ""}])
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.LIVENESS_PROBE_FAILURE
    assert result["requires_human"] is True


def test_a_config_error_comes_from_the_events():
    document = _document(events=[{
        "reason": "Failed", "message": "configmap \"app-config\" not found",
        "type": "Warning", "count": 1, "last_timestamp": ""}])
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.INVALID_CONFIGURATION


def test_an_image_pull_failure_is_not_an_application_crash():
    document = _document(problem="ImagePullBackOff", events=[{
        "reason": "Failed", "message": "Failed to pull image \"app:9\": "
                                        "manifest unknown",
        "type": "Warning", "count": 3, "last_timestamp": ""}])
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.IMAGE_PULL_FAILURE
    assert result["requires_human"] is True


def test_an_insufficient_resource_problem_is_its_own_cause():
    document = _document(problem="Pending", events=[{
        "reason": "FailedScheduling",
        "message": "0/1 nodes are available: insufficient memory.",
        "type": "Warning", "count": 1, "last_timestamp": ""}])
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.INSUFFICIENT_RESOURCES
    assert result["requires_human"] is True


def test_a_failed_rollout_points_at_a_rollback():
    document = _document(problem="", deployment={
        "name": "api", "available": True,
        "progressing_reason": "ProgressDeadlineExceeded",
        "desired_replicas": 1, "available_replicas": 0})
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.ROLLOUT_FAILED
    assert result["recommended_action"] == diagnosis.ROLLBACK_DEPLOYMENT


def test_node_not_ready_is_reported():
    document = _document()
    document["node"] = {
        "name": "node-1", "available": True, "allocatable": {}, "capacity": {},
        "pressure": {},
        "conditions": [{"type": "Ready", "status": "False",
                        "reason": "KubeletNotReady", "message": ""}],
    }
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.NODE_NOT_READY


def test_node_pressure_is_reported():
    document = _document()
    document["node"]["pressure"] = {"MemoryPressure": "True"}
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.INSUFFICIENT_RESOURCES


def test_a_plain_crash_is_an_application_crash():
    document = _document(problem="CrashLoopBackOff",
                         container=_container(last_termination_reason="Error",
                                              exit_code=1))
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.APPLICATION_CRASH
    assert result["recommended_action"] == diagnosis.RESTART_POD


def test_insufficient_evidence_is_unknown_not_a_guess():
    document = _document(problem="", logs="", previous_logs="",
                         container=_container(last_termination_reason="",
                                              exit_code=None))
    document["evidence"] = {"pod_available": False, "logs_available": False,
                             "events_available": False,
                             "node_available": False}
    result = diagnosis.diagnose(document)
    assert result["root_cause"] == diagnosis.UNKNOWN
    assert result["confidence"] == 0.0
    assert result["requires_human"] is True
    assert result["recommended_action"] == diagnosis.NONE


# --------------------------------------------------------------------------- #
# The advisory LLM pass
# --------------------------------------------------------------------------- #

def test_the_model_cannot_invent_a_root_cause():
    base = diagnosis.diagnose(_document())
    proposal = {"root_cause": "DATABASE_CONNECTION_FAILURE", "confidence": 0.99,
                "recommended_action": "RESTART_POD", "requires_human": False}
    assert diagnosis.validate_llm_analysis(proposal, base) is None


def test_the_model_cannot_turn_unknown_into_certainty():
    base = diagnosis.diagnose(_document(problem="", logs="", previous_logs=""))
    proposal = {"root_cause": "OOMKilled", "confidence": 0.99,
                "recommended_action": "INCREASE_MEMORY_LIMIT",
                "requires_human": False}
    assert diagnosis.validate_llm_analysis(proposal, base) is None


def test_the_model_may_confirm_what_the_evidence_shows():
    document = _document(container=_container(last_termination_reason="OOMKilled",
                                              exit_code=137))
    base = diagnosis.diagnose(document)
    proposal = {"root_cause": "OOMKilled", "confidence": 0.98,
                "severity": "high",
                "evidence": ["exit code 137"],
                "recommended_action": "INCREASE_MEMORY_LIMIT",
                "requires_human": False}
    validated = diagnosis.validate_llm_analysis(proposal, base)
    assert validated is not None
    assert validated["root_cause"] == "OOMKilled"
    assert validated["source"] == "llm"


def test_the_model_cannot_lower_the_confidence_below_the_evidence():
    document = _document(container=_container(last_termination_reason="OOMKilled",
                                              exit_code=137))
    base = diagnosis.diagnose(document)
    proposal = {"root_cause": "OOMKilled", "confidence": 0.1}
    validated = diagnosis.validate_llm_analysis(proposal, base)
    assert validated["confidence"] >= base["confidence"]


def test_an_unknown_action_from_the_model_is_rejected():
    document = _document(container=_container(last_termination_reason="OOMKilled",
                                              exit_code=137))
    base = diagnosis.diagnose(document)
    proposal = {"root_cause": "OOMKilled", "confidence": 0.98,
                "recommended_action": "rm -rf /"}
    validated = diagnosis.validate_llm_analysis(proposal, base)
    assert validated["recommended_action"] == diagnosis.INCREASE_MEMORY_LIMIT


def test_llm_analyse_without_a_prompt_keeps_the_deterministic_answer():
    document = _document()
    base = diagnosis.diagnose(document)
    assert diagnosis.llm_analyse(document, base, "") == base


def test_llm_analyse_survives_a_broken_provider(monkeypatch):
    import llm

    async def boom(messages, tools):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(llm, "chat", boom)
    document = _document()
    base = diagnosis.diagnose(document)
    result = diagnosis.llm_analyse(document, base, "please analyse")
    assert result["root_cause"] == base["root_cause"]
    assert result["source"] == "deterministic"


def test_llm_analyse_accepts_a_supported_proposal(monkeypatch):
    import llm

    async def fake(messages, tools):
        return type("Reply", (), {"content":
                                   '{"root_cause": "OOMKilled", '
                                   '"confidence": 0.99, '
                                   '"severity": "high", '
                                   '"evidence": ["exit code 137"], '
                                   '"recommended_action": '
                                   '"INCREASE_MEMORY_LIMIT", '
                                   '"requires_human": false}'})()

    monkeypatch.setattr(llm, "chat", fake)
    document = _document(container=_container(last_termination_reason="OOMKilled",
                                              exit_code=137))
    result = diagnosis.llm_analyse(document, None, "please analyse")
    assert result["source"] == "llm"
    assert result["root_cause"] == "OOMKilled"
