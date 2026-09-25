"""Facts the operator reads must be facts someone actually reported.

Both defects below showed up on the dashboard during a real-browser pass
against the live project, and both presented an invented fact as observed.
"""

import pytest

from agentic_compute import capacity_advisor as ca
from agentic_compute.diagnostics import diagnose_blockers


# ---------------------------------------------------------------------------
# Diagnosis: the Slurm state reason is Slurm's, never ours
# ---------------------------------------------------------------------------


def test_a_pending_job_without_a_reason_is_not_given_one():
    """Nobody reported a reason: the finding must not read "Resources", confirmed."""
    item = diagnose_blockers(job_state="PENDING")[0]

    assert item["category"] == "resource_waiting"
    assert "Resources" not in item["observed_facts"], item["observed_facts"]
    assert "No Slurm state reason was reported" in item["observed_facts"]
    # Capacity is still the likely explanation, but only as a hypothesis.
    assert item["confirmed"] is False


@pytest.mark.parametrize("reason", ["Resources", "ReqNodeNotAvail"])
def test_slurm_saying_the_job_waits_for_nodes_is_still_confirmed(reason):
    item = diagnose_blockers(job_state="PENDING", state_reason=reason)[0]

    assert item["category"] == "resource_waiting"
    assert item["confirmed"] is True
    assert f"Slurm state reason: {reason}" in item["observed_facts"]


@pytest.mark.parametrize("reason", ["None", "WaitingForScheduling"])
def test_a_reason_that_explains_nothing_yet_stays_a_hypothesis(reason):
    """Slurm has not evaluated the job yet: its reason is shown as reported."""
    item = diagnose_blockers(job_state="PENDING", state_reason=reason)[0]

    assert item["confirmed"] is False
    assert f"Slurm state reason: {reason}" in item["observed_facts"]
    assert "waiting for available cluster capacity" not in item["observed_facts"]


# ---------------------------------------------------------------------------
# Quota: a zero preemptible quota is not a ban on Spot
# ---------------------------------------------------------------------------

# What `regions.get` returned for dubai-489009 / us-central1 on 2026-09-25.
_LIVE_US_CENTRAL1 = {
    "CPUS": {"limit": 200.0, "usage": 0.0},
    "PREEMPTIBLE_CPUS": {"limit": 0.0, "usage": 0.0},
    "N2_CPUS": {"limit": 200.0, "usage": 4.0},
    "C2_CPUS": {"limit": 100.0, "usage": 4.0},
}


@pytest.fixture()
def live_quotas(monkeypatch):
    def install(quotas):
        monkeypatch.setattr(
            ca,
            "_fetch_regional_quotas",
            lambda project_id, region: {"status": "ok", "quotas": quotas, "warning": None},
        )

    monkeypatch.delenv("GCP_QUOTA_CPU_LIMIT", raising=False)
    monkeypatch.delenv("GCP_PROJECT_QUOTA_CPUS", raising=False)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    return install


def test_spot_runs_on_standard_quota_when_no_preemptible_quota_exists(live_quotas):
    """Compute Engine: without preemptible quota, Spot consumes standard quota.

    https://cloud.google.com/compute/resource-usage#preemptible_quotas
    Reading PREEMPTIBLE_CPUS=0 as a ceiling flagged every Spot request
    "quota exceeded, 0 of 0 used" on a project with 200 standard vCPU free.
    """
    live_quotas(_LIVE_US_CENTRAL1)

    verdict = ca.check_quota_availability(
        project_id="dubai-489009", region="us-central1", cpu_needed=8, provisioning_model="SPOT"
    )

    assert verdict["status"] == "QUOTA_AVAILABLE", verdict
    assert verdict["quota_metric"] == "CPUS"
    assert (verdict["quota_limit"], verdict["quota_usage"]) == (200, 0)
    # The one case the documentation excludes is stated, not hidden.
    assert "PREEMPTIBLE_CPUS limit 0" in verdict["reason"]
    assert "unless preemptible quota was ever requested" in verdict["reason"]


def test_a_granted_preemptible_quota_is_still_the_spot_ceiling(live_quotas):
    """Once granted, Spot can only consume preemptible quota: keep reading it."""
    live_quotas({**_LIVE_US_CENTRAL1, "PREEMPTIBLE_CPUS": {"limit": 16.0, "usage": 12.0}})

    verdict = ca.check_quota_availability(
        project_id="dubai-489009", region="us-central1", cpu_needed=8, provisioning_model="SPOT"
    )

    assert verdict["status"] == "QUOTA_EXCEEDED", verdict
    assert verdict["quota_metric"] == "PREEMPTIBLE_CPUS"
    assert "unless preemptible quota" not in verdict["reason"]


def test_standard_requests_are_unaffected(live_quotas):
    live_quotas(_LIVE_US_CENTRAL1)

    verdict = ca.check_quota_availability(
        project_id="dubai-489009", region="us-central1", cpu_needed=8, provisioning_model="STANDARD"
    )

    assert verdict["status"] == "QUOTA_AVAILABLE", verdict
    assert verdict["quota_metric"] == "CPUS"
    assert "preemptible" not in verdict["reason"].lower()


def test_each_capacity_row_names_the_quota_it_was_checked_against(live_quotas, monkeypatch):
    """A Spot row read from CPUS must say so, not pass for a Spot-quota check."""
    live_quotas(_LIVE_US_CENTRAL1)
    monkeypatch.setattr(ca, "resolve_quota_project", lambda project_id=None: ("dubai-489009", "env"))
    monkeypatch.setattr(
        ca,
        "query_capacity_advice",
        lambda **kwargs: {"status": "ok", "is_simulated": False, "recommendations": []},
    )

    rows = ca.search_compatible_capacity(
        profile={
            "workload_id": "wl-quota-metric",
            "cpu_requested": 4,
            "memory_mb_requested": 16384,
            "allow_spot": True,
            "allow_fallback_to_standard": True,
        },
        region="us-central1",
    )

    spot = [r for r in rows if r["provisioning_model"] == "SPOT"]
    assert spot and rows, rows
    assert {r["quota_metric"] for r in rows} == {"CPUS"}, rows
    assert {r["quota_status"] for r in spot} == {"QUOTA_AVAILABLE"}, spot


# ---------------------------------------------------------------------------
# Runtime snapshot: a placeholder is not a job
# ---------------------------------------------------------------------------


class _SlurmResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = ""

    def json(self):
        return self._payload


def _slurm_controller(monkeypatch, known_jobs):
    """Stub slurmrestd: /ping and /nodes answer, /job/<id> knows ``known_jobs``."""
    import requests

    def fake_get(url, *args, **kwargs):
        if url.endswith("/ping"):
            return _SlurmResp(200, {"pings": [{"pinged": "UP"}]})
        if url.endswith("/nodes"):
            return _SlurmResp(200, {"nodes": [{"name": "debug-0", "cpus": 2, "idle_cpus": 2}]})
        job_id = url.rsplit("/job/", 1)[-1]
        if job_id in known_jobs:
            return _SlurmResp(200, {"jobs": [known_jobs[job_id]]})
        # What slurmrestd answers about a job it does not know.
        return _SlurmResp(500, {"jobs": [], "errors": [{"description": "Invalid job id specified"}]})

    monkeypatch.delenv("MOCK_SLURM", raising=False)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", lambda *a, **k: _SlurmResp(200, {"job_id": 4242}))


def test_the_slurm_placeholder_is_not_reported_as_a_job(monkeypatch):
    """Before any submission the adapter tracks job "1", PENDING, which does not exist.

    Production showed "Current job: 1 · pending" and offered to diagnose why it
    was waiting, on a cluster that had no job at all.
    """
    from agentic_compute.slurm_adapter import SlurmRuntime

    _slurm_controller(monkeypatch, known_jobs={})
    workload = SlurmRuntime(base_url="http://slurm.test/slurm/v0.0.41").snapshot().workload

    assert workload.job_found is False, workload


def test_a_job_the_controller_reports_is_a_job(monkeypatch):
    from agentic_compute.slurm_adapter import SlurmRuntime

    _slurm_controller(
        monkeypatch,
        known_jobs={"77": {"job_id": 77, "job_state": ["RUNNING"], "partition": "debug"}},
    )
    workload = SlurmRuntime(base_url="http://slurm.test/slurm/v0.0.41", job_id="77").snapshot().workload

    assert workload.job_found is True, workload
    assert workload.id == "77"


def test_a_job_submitted_here_is_a_job_before_the_controller_lists_it(monkeypatch):
    from agentic_compute.slurm_adapter import SlurmRuntime

    _slurm_controller(monkeypatch, known_jobs={})
    runtime = SlurmRuntime(base_url="http://slurm.test/slurm/v0.0.41")
    runtime.submit_job(name="probe", cpu=2, partition="debug", script="#!/bin/bash\nsleep 30\n")
    workload = runtime.snapshot().workload

    assert workload.job_found is True, workload
    assert workload.id == "4242"
