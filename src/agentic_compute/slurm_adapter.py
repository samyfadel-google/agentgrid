from __future__ import annotations

import os
import time
from typing import Any
import requests

import re

from .models import (
    Action,
    CandidateAllocation,
    ClusterState,
    Objective,
    RuntimeSnapshot,
    WorkloadState,
)
from .runtime import RuntimeAdapter


# Cluster configuration for GCP Slurm deployment (hpcslurm)
SUPPORTED_SLURM_PARTITIONS = {
    "debug": {"machine_type": "n2-standard-2", "provisioning": "100% Standard", "max_cpus": 4},
    "compute": {"machine_type": "c2-standard-60", "provisioning": "100% Standard", "max_cpus": 600},
    "h3": {"machine_type": "h3-standard-88", "provisioning": "100% Standard", "max_cpus": 1760},
}
SUPPORTED_SLURM_MACHINE_TYPES = {"n2-standard-2", "c2-standard-60", "h3-standard-88"}
SUPPORTED_SLURM_PROVISIONING = {"100% Standard", "STANDARD"}


#: Provisioning-model spellings that all mean the same thing on this cluster.
#: Operators, plan engines and the Slurm partitions each write it differently;
#: comparing the raw strings produced spurious "mismatch" verdicts.
_SLURM_PROVISIONING_ALIASES: dict[str, str] = {
    "standard": "100% Standard",
    "100% standard": "100% Standard",
    "100%standard": "100% Standard",
    "on-demand": "100% Standard",
    "ondemand": "100% Standard",
    "spot": "100% Spot",
    "100% spot": "100% Spot",
    "100%spot": "100% Spot",
    "preemptible": "100% Spot",
    "80% spot / 20% standard": "80% Spot / 20% Standard",
    "80% spot/20% standard": "80% Spot / 20% Standard",
    "hedged": "80% Spot / 20% Standard",
}


def normalize_slurm_provisioning(value: str | None) -> str | None:
    """Return the canonical spelling of a provisioning model.

    ``None`` and the empty string mean "unspecified" and are passed through.
    A genuinely unrecognised value raises :class:`ValueError` so a typo such as
    ``BANANA`` is refused rather than stored and later compared against real
    telemetry.
    """
    if value is None:
        return None
    key = str(value).strip()
    if key == "":
        return None
    canonical = _SLURM_PROVISIONING_ALIASES.get(key.lower())
    if canonical is None:
        raise ValueError(
            f"unknown provisioning model '{value}'; supported values are "
            f"{', '.join(sorted(set(_SLURM_PROVISIONING_ALIASES.values())))}"
        )
    return canonical


def generate_candidate_cpus(total_cpu: int) -> list[int]:
    """Generate elastic candidate CPU allocations up to cluster capacity."""
    env_override = os.getenv("CANDIDATE_CPUS")
    if env_override:
        try:
            return sorted([int(x.strip()) for x in env_override.split(",") if x.strip()])
        except Exception:
            pass
    standard_steps = [2, 4, 8, 16, 32, 48, 64, 96, 128, 192, 256, 384, 512, 1024]
    candidates = [c for c in standard_steps if c <= total_cpu]
    if not candidates:
        return [max(1, total_cpu)]
    if total_cpu not in candidates and total_cpu > candidates[0]:
        candidates.append(total_cpu)
        candidates.sort()
    return candidates


class SlurmRuntime(RuntimeAdapter):
    """RuntimeAdapter connecting to a real Slurm cluster via slurmrestd on GCP with dynamic lifecycle tracking."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        jwt_token: str | None = None,
        job_id: str | None = None,
        user_name: str | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("SLURM_REST_URL", "http://10.0.0.4:6842/slurm/v0.0.41")
        ).rstrip("/")
        self.jwt_token = jwt_token or os.getenv("SLURM_JWT_TOKEN", "")
        self.user_name = user_name or os.getenv("SLURM_USER", "slurm")
        self.total_cpu = int(os.getenv("SLURM_TOTAL_CPU", "128"))
        self.cpu_cost_per_hour_eur = float(os.getenv("CPU_COST_PER_HOUR_EUR", "0.05"))
        self.deadline_minutes = float(os.getenv("DEADLINE_MINUTES", "30.0"))
        self.max_cost_eur = float(os.getenv("MAX_COST_EUR", "10.0"))
        self.minimize_cost = os.getenv("MINIMIZE_COST", "true").lower() in ("true", "1", "yes")
        self.baseline_workload_duration_minutes = float(os.getenv("BASELINE_WORKLOAD_DURATION_MINUTES", "25.0"))
        self.active_job_id = job_id or os.getenv("SLURM_JOB_ID") or "1"
        self.verification_timeout_seconds = float(
            os.getenv("SLURM_VERIFICATION_TIMEOUT_SECS", "60.0")
        )
        super().__init__()
        self.control_mode = "delegation"
        self.reset()

    def _check_verification_timeout(self) -> None:
        """Enforce explicit deadline on pending Slurm verification."""
        if (
            self.verification_status == "pending_verification"
            and self.pending_verification_start_time is not None
            and (time.time() - self.pending_verification_start_time) > self.verification_timeout_seconds
        ):
            self.verification_status = "failed"
            self.verification_detail = (
                f"Verification timed out after {self.verification_timeout_seconds:.1f}s "
                f"waiting for authoritative Slurm telemetry"
            )
            if self.last_slurm_action and self.last_slurm_action.get("status") == "pending_verification":
                self.last_slurm_action["status"] = "failed"
                self.last_slurm_action["verification_status"] = "failed"
                self.last_slurm_action["error"] = self.verification_detail
            self.requested_cpu = None
            self.requested_machine_type = None
            self.requested_provisioning_mix = None
            self.pending_verification_start_time = None

    def _is_mock_mode(self) -> bool:
        """True when the caller explicitly asked for offline/mock behaviour.

        Outside mock mode, an unreachable controller is an error: substituting
        locally simulated values for real telemetry is exactly the silent
        fallback the audit flagged.
        """
        return os.getenv("MOCK_SLURM", "").lower() in ("true", "1", "yes") or str(
            self.active_job_id
        ).startswith("mock-")

    def _reject(self, action_name: str, detail: str, cpu: int | None = None) -> None:
        """Record a refusal and clear any pending request state."""
        self.verification_status = "unsupported"
        self.verification_detail = detail
        self.requested_cpu = None
        self.requested_machine_type = None
        self.requested_provisioning_mix = None
        self.pending_verification_start_time = None
        self.last_slurm_action = {
            "action": action_name,
            "job_id": self.active_job_id,
            "requested_cpu": cpu,
            "observed_cpu": self.observed_cpu,
            "observed_machine_type": self.observed_machine_type,
            "observed_provisioning_mix": self.observed_provisioning_mix,
            "status": "unsupported",
            "verification_status": "unsupported",
            "error": detail,
            "timestamp": time.time(),
        }

    def _validate_cluster_capabilities(
        self,
        machine_type: str | None,
        provisioning_model: str | None,
        action_name: str = "resize_workload",
        cpu: int | None = None,
    ) -> None:
        """Shared capability validation for job submission and workload resizing."""
        # 0. Capacity: a request the cluster physically cannot satisfy is refused
        #    here, before any API call. This used to be accepted and forwarded.
        if cpu is not None:
            try:
                cpu_int = int(cpu)
            except (TypeError, ValueError):
                cpu_int = None
            if cpu_int is not None:
                if cpu_int <= 0:
                    detail = (
                        f"Invalid CPU request ({cpu_int}) for {action_name}: a workload "
                        f"needs at least 1 CPU."
                    )
                    self._reject(action_name, detail, cpu=cpu_int)
                    raise RuntimeError(detail)
                if cpu_int > self.total_cpu:
                    detail = (
                        f"Requested {cpu_int} CPUs but the cluster only has {self.total_cpu} "
                        f"in total; {action_name} refused before contacting the controller."
                    )
                    self._reject(action_name, detail, cpu=cpu_int)
                    raise RuntimeError(detail)

        # 1. Provisioning model: normalise the spelling first so 'STANDARD' and
        #    '100% Standard' are treated as the same thing, and reject anything
        #    genuinely unknown instead of storing it.
        if provisioning_model is not None and str(provisioning_model).strip() != "":
            try:
                provisioning_model = normalize_slurm_provisioning(provisioning_model)
            except ValueError as exc:
                detail = f"Unsupported configuration on Slurm cluster: {exc}"
                self._reject(action_name, detail, cpu=cpu)
                raise RuntimeError(detail) from exc

        is_spot_requested = provisioning_model and (
            "spot" in provisioning_model.lower() or "80%" in provisioning_model
        )
        is_unsupported_machine = (
            machine_type is not None
            and machine_type not in SUPPORTED_SLURM_MACHINE_TYPES
        )

        if is_spot_requested or is_unsupported_machine:
            reason_parts = []
            if is_spot_requested:
                reason_parts.append(
                    f"provisioning mix '{provisioning_model}' is unsupported on this Slurm cluster "
                    f"(cluster only supports STANDARD on-demand nodesets: debug, compute, h3)"
                )
            if is_unsupported_machine:
                reason_parts.append(
                    f"machine type '{machine_type}' is unsupported "
                    f"(cluster only supports: {', '.join(sorted(SUPPORTED_SLURM_MACHINE_TYPES))})"
                )
            detail = "Unsupported configuration on Slurm cluster: " + "; ".join(reason_parts)
            self.verification_status = "unsupported"
            self.verification_detail = detail
            self.requested_cpu = None
            self.requested_machine_type = None
            self.requested_provisioning_mix = None
            self.pending_verification_start_time = None
            self.last_slurm_action = {
                "action": action_name,
                "job_id": self.active_job_id,
                "requested_cpu": cpu,
                "requested_machine_type": machine_type,
                "requested_provisioning_mix": provisioning_model,
                "observed_cpu": self.observed_cpu,
                "observed_machine_type": self.observed_machine_type,
                "observed_provisioning_mix": self.observed_provisioning_mix,
                "status": "unsupported",
                "verification_status": "unsupported",
                "error": detail,
                "timestamp": time.time(),
            }
            raise RuntimeError(detail)

    def reset(self) -> None:
        self.start_time = time.time()
        self.elapsed_minutes = 0.0
        self.allocated_cpu = int(os.getenv("INITIAL_WORKLOAD_CPU", "4"))
        self.allocated_gpu = 0
        self.remaining_work_units = 100.0
        self.accrued_cost_eur = 0.0
        #: Elapsed minutes that could not be priced because no allocation was
        #: observed. Kept visible instead of being billed at a local default.
        self.unpriced_elapsed_minutes = 0.0
        #: How free_cpu was derived: "node_telemetry" or "own_allocation_only".
        self.free_cpu_basis = "unknown"
        self.job_done = False
        self.job_status = "PENDING"
        self.job_failed = False
        self.is_real_slurm_job = False
        self.last_slurm_action = None
        self.machine_type: str | None = None
        self.provisioning_mix: str | None = None
        self.requested_cpu: int | None = None
        self.requested_machine_type: str | None = None
        self.requested_provisioning_mix: str | None = None
        self.observed_cpu: int | None = None
        self.observed_machine_type: str | None = None
        self.observed_provisioning_mix: str | None = None
        self.verification_status: str = "unverified"
        self.verification_detail: str | None = "Awaiting initial Slurm telemetry"
        self.cost_basis: str = "unverified"
        self.last_slurm_elapsed_secs: float | None = None
        self.pending_verification_start_time: float | None = None

    def configure_objective(
        self,
        deadline_minutes: float | None = None,
        max_cost_eur: float | None = None,
        minimize_cost: bool | None = None,
    ) -> None:
        """Dynamically configure workload objective constraints."""
        if deadline_minutes is not None:
            self.deadline_minutes = float(deadline_minutes)
        if max_cost_eur is not None:
            self.max_cost_eur = float(max_cost_eur)
        if minimize_cost is not None:
            self.minimize_cost = bool(minimize_cost)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.jwt_token:
            headers["X-SLURM-USER-TOKEN"] = self.jwt_token
            headers["X-SLURM-USER-NAME"] = self.user_name
        return headers

    def _controller_reachable(self) -> bool:
        """True when the Slurm controller answers at all.

        Distinguishes "the cluster says there is no such job" from "we cannot
        talk to the cluster". Only the first is safe to report as state.
        """
        try:
            resp = requests.get(f"{self.base_url}/ping", headers=self._headers(), timeout=2.0)
            if resp.status_code < 500:
                return True
        except Exception:
            pass
        try:
            resp = requests.get(f"{self.base_url}/nodes", headers=self._headers(), timeout=2.0)
            return resp.status_code < 500
        except Exception:
            return False

    def _get_nodes(self) -> dict[str, Any]:
        try:
            resp = requests.get(f"{self.base_url}/nodes", headers=self._headers(), timeout=2.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"nodes": []}

    def _get_job(self) -> dict[str, Any]:
        # Note: no name-less discovery here. There used to be two
        # _discover_active_job definitions, the second shadowing the first, so
        # this call resolved to the name-matching variant invoked with
        # ``name=None`` -- which matched any job whose name was absent. Job
        # identity is never guessed: the caller must know which job it means.
        pass
        try:
            resp = requests.get(
                f"{self.base_url}/job/{self.active_job_id}",
                headers=self._headers(),
                timeout=2.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"jobs": []}

    def _discover_active_job(self, name: str | None = None) -> str | None:
        """Find the job created under a specific submission name.

        Used to resolve an ambiguous submission timeout. ``name`` is mandatory
        in practice: without it there is no identity to match, and returning
        "some active job" is how a timeout used to be reported as a success on
        a job the caller never submitted.

        ``job_state`` is a list in recent slurmrestd versions and a bare string
        in older ones; both are handled.
        """
        if not name:
            return None
        try:
            resp = requests.get(f"{self.base_url}/jobs", headers=self._headers(), timeout=5.0)
            if resp.status_code != 200:
                return None
            data = resp.json()
            for j in data.get("jobs", []):
                if j.get("name") != name:
                    continue
                state = j.get("job_state")
                state_str = state[0] if isinstance(state, list) and state else str(state)
                if state_str in ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING"):
                    return str(j.get("job_id"))
        except Exception:
            return None
        return None

    def submit_job(
        self,
        name: str = "agentgrid-workload",
        cpu: int | None = None,
        gpu: int = 0,
        partition: str | None = None,
        memory_mb: int | None = None,
        script: str | None = None,
        machine_type: str | None = None,
        provisioning_model: str | None = None,
        approved_by_operator: bool = False,
    ) -> dict[str, Any]:
        """Submit or initialize a new workload on the Slurm cluster with elastic parameters."""
        allocated_cpu = cpu if cpu is not None else int(os.getenv("INITIAL_WORKLOAD_CPU", "4"))
        req_mtype = machine_type or self.machine_type
        req_pmix = provisioning_model or self.provisioning_mix or "100% Standard"

        # 1. Shared validation of cluster capabilities: reject unsupported machine types & Spot
        self._validate_cluster_capabilities(
            machine_type=req_mtype,
            provisioning_model=req_pmix,
            action_name="submit_job",
            cpu=allocated_cpu,
        )

        self.check_execution_permission(
            {
                "action": "submit_job",
                "workload_id": name,
                "cpu": allocated_cpu,
                "machine_type": req_mtype,
                "provisioning_model": req_pmix,
            },
            approved_by_operator=approved_by_operator,
        )

        # 2. Determine target partition
        target_partition = partition
        if not target_partition and req_mtype:
            for part_name, part_info in SUPPORTED_SLURM_PARTITIONS.items():
                if part_info["machine_type"] == req_mtype:
                    target_partition = part_name
                    break
        if not target_partition:
            if allocated_cpu <= 4:
                target_partition = "debug"
            elif allocated_cpu <= 600:
                target_partition = "compute"
            else:
                target_partition = "h3"

        part_mtype = SUPPORTED_SLURM_PARTITIONS.get(target_partition, {}).get("machine_type") if target_partition else None
        comment_str = f"machine_type={req_mtype or part_mtype or 'unknown'};provisioning_model={req_pmix}"

        # slurmstepd exec()s the batch script, so it needs an interpreter line.
        # Callers such as the dashboard send one command ("sleep 30"), not a
        # script, and without "#!" the job fails on the node with "Exec format
        # error" (sbatch refuses such a script up front; slurmrestd does not).
        if script and not script.lstrip().startswith("#!"):
            script = f"#!/bin/bash\n{script.strip()}\n"

        payload_job: dict[str, Any] = {
            "name": name,
            "tasks": 1,
            "cpus_per_task": allocated_cpu,
            "current_working_directory": "/tmp",
            "environment": ["PATH=/bin:/usr/bin:/usr/local/bin"],
            "script": script or "#!/bin/bash\nsleep 3600\n",
            "partition": target_partition,
            "comment": comment_str,
            "admin_comment": comment_str,
        }
        # No `features` / `constraints`: each partition maps to exactly one
        # machine type, so the partition already pins the hardware, and a
        # machine-type name is not a node feature on this cluster. Sending it
        # made slurmctld reject every submission with ESLURM_INVALID_FEATURE
        # (2029 "Invalid feature specification") on Slurm 25.11.8 / data_parser
        # v0.0.41, where `features` is not even a job_desc field (it is ignored
        # with a warning). The requested type is still recorded in `comment`.
        if memory_mb:
            payload_job["memory_per_node"] = memory_mb
        if gpu > 0:
            payload_job["tres_per_task"] = f"gres/gpu:{gpu}"

        payload = {"job": payload_job}
        submitted_id = None
        try:
            resp = requests.post(
                f"{self.base_url}/job/submit",
                headers=self._headers(),
                json=payload,
                timeout=5.0,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                submitted_id = str(data.get("job_id") or data.get("job_submit_response_msg", {}).get("job_id", ""))
            else:
                if os.getenv("MOCK_SLURM", "").lower() in ("true", "1"):
                    submitted_id = f"mock-{int(time.time()) % 100000}"
                else:
                    self.requested_cpu = None
                    self.requested_machine_type = None
                    self.requested_provisioning_mix = None
                    self.pending_verification_start_time = None
                    raise RuntimeError(
                        f"Slurm submission rejected (HTTP {resp.status_code}): {resp.text}"
                    )
        except requests.RequestException as exc:
            discovered = self._discover_active_job(name=name)
            if discovered:
                submitted_id = discovered
            elif os.getenv("MOCK_SLURM", "").lower() in ("true", "1"):
                submitted_id = f"mock-{int(time.time()) % 100000}"
            else:
                self.requested_cpu = None
                self.requested_machine_type = None
                self.requested_provisioning_mix = None
                self.pending_verification_start_time = None
                raise RuntimeError(f"Could not connect to Slurm cluster at {self.base_url}: {exc}") from exc

        if not submitted_id:
            self.requested_cpu = None
            self.requested_machine_type = None
            self.requested_provisioning_mix = None
            self.pending_verification_start_time = None
            raise RuntimeError(f"Slurm did not return a valid job_id from {self.base_url}/job/submit")

        self.active_job_id = submitted_id
        self.allocated_cpu = allocated_cpu
        self.allocated_gpu = gpu
        self.requested_cpu = allocated_cpu
        self.requested_machine_type = req_mtype
        self.requested_provisioning_mix = req_pmix
        # Keep observed machine type and provisioning unknown until telemetry observation
        self.machine_type = None
        self.provisioning_mix = None
        self.observed_cpu = None
        self.observed_machine_type = None
        self.observed_provisioning_mix = None
        self.verification_status = "pending_verification"
        self.pending_verification_start_time = time.time()
        self.verification_detail = (
            f"Job {submitted_id} submitted to partition '{target_partition}'; pending verification from Slurm telemetry"
        )
        self.cost_basis = "unverified"
        self.remaining_work_units = 100.0
        self.elapsed_minutes = 0.0
        self.accrued_cost_eur = 0.0
        self.job_done = False
        self.job_status = "PENDING"
        self.job_failed = False
        self.last_slurm_elapsed_secs = None
        self.is_real_slurm_job = not submitted_id.startswith("mock-")

        self.last_slurm_action = {
            "action": "submit_job",
            "job_id": self.active_job_id,
            "requested_cpu": allocated_cpu,
            "requested_machine_type": req_mtype,
            "requested_provisioning_mix": req_pmix,
            "observed_cpu": None,
            "observed_machine_type": None,
            "observed_provisioning_mix": None,
            "status": "pending_verification",
            "verification_status": "pending_verification",
            "timestamp": self.pending_verification_start_time,
        }

        return {
            "job_id": self.active_job_id,
            "allocated_cpu": self.allocated_cpu,
            "allocated_gpu": self.allocated_gpu,
            "partition": target_partition,
            "machine_type": self.machine_type,
            "requested_machine_type": self.requested_machine_type,
            "provisioning_mix": self.provisioning_mix,
            "requested_provisioning_mix": self.requested_provisioning_mix,
            "verification_status": self.verification_status,
            "is_real_slurm_job": self.is_real_slurm_job,
        }

    def _get_cost_factor(self) -> float:
        if not self.provisioning_mix:
            return 1.0
        if "80% Spot" in self.provisioning_mix:
            return 0.48
        elif "Spot" in self.provisioning_mix or "SPOT" in self.provisioning_mix:
            return 0.35
        else:
            return 1.0

    def _sync_job_state(self, job: dict[str, Any]) -> None:
        """Synchronize local runtime state with authoritative Slurm job telemetry."""
        # Enforce timeout deadline if currently pending verification
        self._check_verification_timeout()

        # 1. Update allocated CPU from Slurm job resources if genuinely allocated (> 0).
        # Never treat cpus_per_task or stale local values as proof of actual allocated resources.
        res = job.get("job_resources")
        has_genuine_allocation = False
        if isinstance(res, dict) and res.get("allocated_cpus") is not None:
            try:
                cpus_val = int(res["allocated_cpus"])
                if cpus_val > 0:
                    self.allocated_cpu = cpus_val
                    self.observed_cpu = cpus_val
                    has_genuine_allocation = True
            except (ValueError, TypeError):
                pass

        if not has_genuine_allocation:
            self.observed_cpu = None

        # 2. Check partition to independently observe machine_type and provisioning
        job_part = job.get("partition")
        observed_machine_type = None
        observed_provisioning = None
        if job_part and job_part in SUPPORTED_SLURM_PARTITIONS:
            observed_machine_type = SUPPORTED_SLURM_PARTITIONS[job_part]["machine_type"]
            observed_provisioning = SUPPORTED_SLURM_PARTITIONS[job_part]["provisioning"]

        if has_genuine_allocation and observed_machine_type:
            self.machine_type = observed_machine_type
            self.observed_machine_type = observed_machine_type
            self.provisioning_mix = observed_provisioning
            self.observed_provisioning_mix = observed_provisioning
        elif has_genuine_allocation:
            self.observed_machine_type = None
            self.observed_provisioning_mix = None
        else:
            self.observed_machine_type = None
            self.observed_provisioning_mix = None

        # 3. Independent verification check against every requested property
        if self.verification_status == "pending_verification":
            if not has_genuine_allocation:
                # Allocation is not yet genuinely observed from Slurm controller
                self.verification_detail = (
                    f"Job {job.get('job_id', self.active_job_id)} has no allocated resources yet; "
                    f"awaiting node allocation from Slurm controller"
                )
                self.cost_basis = "unverified"
            else:
                self.cost_basis = "observed_allocation"
                mismatches = []
                if self.requested_cpu is not None and self.allocated_cpu != self.requested_cpu:
                    mismatches.append(
                        f"CPU mismatch (requested {self.requested_cpu}, observed {self.allocated_cpu})"
                    )
                if (
                    self.requested_machine_type is not None
                    and self.machine_type != self.requested_machine_type
                ):
                    mismatches.append(
                        f"machine type mismatch (requested '{self.requested_machine_type}', "
                        f"observed '{self.machine_type}' from partition '{job_part}')"
                    )
                if self.requested_provisioning_mix is not None:
                    # Compare canonical forms: "STANDARD" and "100% Standard"
                    # are the same allocation, not a mismatch.
                    def _canon(value):
                        try:
                            return normalize_slurm_provisioning(value)
                        except ValueError:
                            return value

                    if _canon(self.provisioning_mix) != _canon(self.requested_provisioning_mix):
                        mismatches.append(
                            f"provisioning mix mismatch (requested '{self.requested_provisioning_mix}', "
                            f"observed '{self.provisioning_mix}')"
                        )

                if not mismatches:
                    self.verification_status = "verified"
                    self.verification_detail = (
                        f"Observed authoritative allocation from Slurm controller: "
                        f"{self.allocated_cpu} CPUs, {self.machine_type}, {self.provisioning_mix} (partition: {job_part})"
                    )
                    if self.last_slurm_action and self.last_slurm_action.get("status") == "pending_verification":
                        self.last_slurm_action["status"] = "applied"
                        self.last_slurm_action["verification_status"] = "verified"
                        self.last_slurm_action["observed_cpu"] = self.allocated_cpu
                        self.last_slurm_action["observed_machine_type"] = self.machine_type
                        self.last_slurm_action["observed_provisioning_mix"] = self.provisioning_mix
                    self.requested_cpu = None
                    self.requested_machine_type = None
                    self.requested_provisioning_mix = None
                    self.pending_verification_start_time = None
                else:
                    self.verification_status = "mismatch"
                    detail = "Allocation mismatch: " + "; ".join(mismatches)
                    self.verification_detail = detail
                    if self.last_slurm_action and self.last_slurm_action.get("status") == "pending_verification":
                        self.last_slurm_action["status"] = "mismatch"
                        self.last_slurm_action["verification_status"] = "mismatch"
                        self.last_slurm_action["observed_cpu"] = self.allocated_cpu
                        self.last_slurm_action["observed_machine_type"] = self.machine_type
                        self.last_slurm_action["observed_provisioning_mix"] = self.provisioning_mix
                        self.last_slurm_action["error"] = detail
                    self.requested_cpu = None
                    self.requested_machine_type = None
                    self.requested_provisioning_mix = None
                    self.pending_verification_start_time = None
        elif self.verification_status in ("failed", "unsupported", "mismatch"):
            # A rejected, unsupported, or mismatched command cannot become "applied"
            if has_genuine_allocation:
                self.cost_basis = "observed_allocation"
        elif self.verification_status in ("unverified", "verified"):
            if has_genuine_allocation and observed_machine_type:
                self.verification_status = "verified"
                self.verification_detail = (
                    f"Observed authoritative allocation from Slurm controller: "
                    f"{self.allocated_cpu} CPUs, {self.machine_type}, {self.provisioning_mix} (partition: {job_part})"
                )
                self.cost_basis = "observed_allocation"
            else:
                self.verification_status = "unverified"
                self.verification_detail = "Awaiting authoritative Slurm allocation telemetry"
                self.cost_basis = "unverified"

        # 2. Synchronize elapsed time and accrue cost for delta elapsed seconds
        time_info = job.get("time", {})
        slurm_elapsed_secs = time_info.get("elapsed", 0)
        if isinstance(slurm_elapsed_secs, (int, float)) and slurm_elapsed_secs >= 0:
            if self.last_slurm_elapsed_secs is None:
                delta_secs = float(slurm_elapsed_secs)
            else:
                delta_secs = max(0.0, float(slurm_elapsed_secs) - self.last_slurm_elapsed_secs)
            self.last_slurm_elapsed_secs = float(slurm_elapsed_secs)
            self.elapsed_minutes = float(slurm_elapsed_secs) / 60.0

            cost_factor = self._get_cost_factor()
            if delta_secs > 0:
                if has_genuine_allocation and self.observed_cpu:
                    self.accrued_cost_eur += (
                        self.observed_cpu
                        * self.cpu_cost_per_hour_eur
                        * cost_factor
                        * (delta_secs / 3600.0)
                    )
                else:
                    # No allocation was observed, so there is no basis for a
                    # price. Booking elapsed time against a local default CPU
                    # count invented a cost for compute nobody confirmed. The
                    # time is recorded separately so the gap stays visible.
                    self.unpriced_elapsed_minutes += delta_secs / 60.0

        # 3. Synchronize lifecycle status
        state = job.get("job_state", ["RUNNING"])
        state_str = state[0] if isinstance(state, list) and state else str(state)
        self.job_status = state_str

        if state_str == "COMPLETED":
            self.job_done = True
            self.job_failed = False
            self.remaining_work_units = 0.0
        elif state_str in ("FAILED", "TIMEOUT", "CANCELLED", "BOOT_FAIL", "NODE_FAIL", "DEADLINE"):
            self.job_done = True
            self.job_failed = True
            # Do NOT reset remaining_work_units to 0.0 on failure: preserves unfinished work indicator
        else:
            # RUNNING or PENDING
            self.job_done = False
            self.job_failed = False
            expected_total_secs = (
                self.baseline_workload_duration_minutes * 60.0 * (16.0 / max(1, self.allocated_cpu))
            )
            elapsed = float(slurm_elapsed_secs) if isinstance(slurm_elapsed_secs, (int, float)) else 0.0
            progress = min(1.0, elapsed / max(1.0, expected_total_secs))
            self.remaining_work_units = max(0.1, round(100.0 * (1.0 - progress), 2))

    def snapshot(self) -> RuntimeSnapshot:
        self._check_verification_timeout()
        # Check real job if active
        job_info = self._get_job()
        if "jobs" in job_info and job_info["jobs"]:
            self.is_real_slurm_job = True
            self._sync_job_state(job_info["jobs"][0])
        elif not self._is_mock_mode() and not self._controller_reachable():
            # ``tick`` already refused to simulate a silently unreachable
            # cluster; ``snapshot`` used to return locally invented numbers,
            # which is the same fault in read-only clothing.
            raise RuntimeError(
                f"Slurm cluster is unreachable at {self.base_url}: cannot read cluster "
                f"state for job {self.active_job_id}, and MOCK_SLURM is not enabled. "
                f"No locally simulated values are substituted."
            )

        # Dynamic discovery of cluster nodes, total CPU, and GPUs from Slurm
        nodes_info = self._get_nodes()
        nodes_list = [n for n in nodes_info.get("nodes", []) if isinstance(n, dict)]
        cluster_total_cpu = (
            sum(n.get("cpus", 0) for n in nodes_list)
            if nodes_list
            else self.total_cpu
        )

        # Free CPU must account for *every* job on the cluster. Subtracting only
        # our own allocation reported a fully busy 128-CPU cluster as having 124
        # CPUs available. slurmrestd exposes per-node ``idle_cpus`` and
        # ``alloc_cpus``; use them when present and say so when they are not.
        cluster_free_cpu: int | None = None
        if nodes_list:
            if all(n.get("idle_cpus") is not None for n in nodes_list):
                cluster_free_cpu = sum(int(n.get("idle_cpus", 0)) for n in nodes_list)
            elif all(n.get("alloc_cpus") is not None for n in nodes_list):
                cluster_free_cpu = sum(
                    max(0, int(n.get("cpus", 0)) - int(n.get("alloc_cpus", 0)))
                    for n in nodes_list
                )
        if cluster_free_cpu is None:
            # No per-node occupancy telemetry: the only defensible statement is
            # what our own job holds, so report that and nothing better.
            cluster_free_cpu = max(0, cluster_total_cpu - self.allocated_cpu)
            self.free_cpu_basis = "own_allocation_only"
        else:
            cluster_free_cpu = max(0, min(cluster_free_cpu, cluster_total_cpu))
            self.free_cpu_basis = "node_telemetry"

        cluster_total_gpu = 0
        for n in nodes_list:
            if isinstance(n, dict):
                gres = str(n.get("gres", "") or n.get("tres", ""))
                if "gpu" in gres.lower():
                    match = re.search(r'gpu(?::[^:]*)?:(\d+)', gres, re.IGNORECASE)
                    if match:
                        cluster_total_gpu += int(match.group(1))

        # Dynamic candidate allocations scaled from current allocation
        candidate_cpu_steps = generate_candidate_cpus(cluster_total_cpu)
        candidates = []
        curr_cpu = max(1, self.allocated_cpu)
        if self.job_done:
            curr_remaining = 0.0
        else:
            # Case 4.1 fix: decoupled from deadline_minutes; based on intrinsic baseline duration
            curr_remaining = max(
                0.0,
                self.baseline_workload_duration_minutes * (self.remaining_work_units / 100.0) * (16.0 / curr_cpu),
            )

        for cpu in candidate_cpu_steps:
            speedup = cpu / curr_cpu
            est_remaining = round(max(0.0, curr_remaining / speedup), 2) if not self.job_done else 0.0
            finish_at = round(self.elapsed_minutes + est_remaining, 2)

            # Dynamic Slack Ratio S = (Deadline - CurrentElapsed) / ETA
            slack_time = max(0.0, self.deadline_minutes - self.elapsed_minutes)
            slack_ratio = (slack_time / est_remaining) if est_remaining > 0 else 99.0

            # Cluster candidate allocations matching real Slurm partitions (debug, compute, h3)
            pmix = "100% Standard"
            cost_factor = 1.0

            if cpu <= 4:
                mtype = "n2-standard-2"
                rank = 2
                obtainability = 0.95
            elif cpu <= 600:
                mtype = "c2-standard-60"
                rank = 1
                obtainability = 0.90
            else:
                mtype = "h3-standard-88"
                rank = 1
                obtainability = 0.85

            est_cost = round(
                self.accrued_cost_eur + (cpu * self.cpu_cost_per_hour_eur * cost_factor * (est_remaining / 60.0)),
                4,
            )
            candidates.append(
                CandidateAllocation(
                    cpu=cpu,
                    estimated_remaining_minutes=est_remaining,
                    projected_finish_at_minutes=finish_at,
                    projected_total_cost_eur=est_cost,
                    meets_deadline=finish_at <= self.deadline_minutes,
                    within_budget=est_cost <= self.max_cost_eur,
                    machine_type=mtype,
                    rank=rank,
                    provisioning_mix=pmix,
                    obtainability_score=obtainability,
                )
            )

        return RuntimeSnapshot(
            cluster=ClusterState(
                current_time_minutes=round(self.elapsed_minutes, 2),
                total_cpu=cluster_total_cpu,
                free_cpu=cluster_free_cpu,
                total_gpu=cluster_total_gpu,
                free_gpu=max(0, cluster_total_gpu - self.allocated_gpu),
            ),
            workload=WorkloadState(
                id=self.active_job_id,
                kind="slurm-batch",
                remaining_work_units=round(self.remaining_work_units, 2),
                allocated_cpu=self.allocated_cpu,
                allocated_gpu=self.allocated_gpu,
                estimated_remaining_minutes=round(curr_remaining, 2),
                accrued_cost_eur=round(self.accrued_cost_eur, 4),
                done=self.job_done,
                status=self.job_status,
                failed=self.job_failed,
                machine_type=self.machine_type,
                provisioning_mix=self.provisioning_mix,
                requested_cpu=self.requested_cpu,
                requested_machine_type=self.requested_machine_type,
                requested_provisioning_mix=self.requested_provisioning_mix,
                observed_cpu=self.observed_cpu,
                observed_machine_type=self.observed_machine_type,
                observed_provisioning_mix=self.observed_provisioning_mix,
                verification_status=self.verification_status,
                verification_detail=self.verification_detail,
                cost_basis=self.cost_basis,
                # Found on the controller, or submitted by this process
                # (mock submissions carry a "mock-" id). Otherwise the record
                # is the placeholder set by reset(), not a job.
                job_found=self.is_real_slurm_job or str(self.active_job_id).startswith("mock-"),
            ),
            objective=Objective(
                deadline_at_minutes=self.deadline_minutes,
                minimize_cost=self.minimize_cost,
                max_cost_eur=self.max_cost_eur,
            ),
            candidate_allocations=candidates,
        )

    def apply(self, action: Action, approved_by_operator: bool = False) -> None:
        if action.action == "noop":
            return
        if not action.cpu:
            return

        # Identity first: a request aimed at another workload must be refused
        # before any permission check, any validation and any API call. The
        # adapter previously ignored action.workload_id and mutated whatever job
        # it happened to track, so a typo silently resized the wrong thing.
        if action.workload_id and str(action.workload_id) != str(self.active_job_id):
            detail = (
                f"Refusing '{action.action}': it targets workload "
                f"'{action.workload_id}' but this runtime tracks job "
                f"'{self.active_job_id}'. No mutation was attempted."
            )
            raise ValueError(detail)

        self.check_execution_permission(action, approved_by_operator=approved_by_operator)

        target_cpu = action.cpu
        target_machine_type = action.machine_type or self.machine_type
        target_provisioning_mix = action.provisioning_model or self.provisioning_mix or "100% Standard"

        if not target_machine_type:
            if target_cpu <= 4:
                target_machine_type = "n2-standard-2"
            elif target_cpu <= 600:
                target_machine_type = "c2-standard-60"
            else:
                target_machine_type = "h3-standard-88"

        # 1. Validation of cluster capabilities (shared with submit_job):
        self._validate_cluster_capabilities(
            machine_type=action.machine_type or target_machine_type,
            provisioning_model=action.provisioning_model or target_provisioning_mix,
            action_name=action.action,
            cpu=target_cpu,
        )

        # 2. Check active workload state:
        # Running Slurm jobs cannot be hot-resized without interruption
        if self.job_status in ("RUNNING", "CONFIGURING"):
            detail = (
                f"Hot-resizing an active running Slurm job ({self.job_status}) is not supported by Slurm "
                f"without interruption (Job is no longer pending execution); checkpoint/requeue or migration required."
            )
            self.verification_status = "unsupported"
            self.verification_detail = detail
            self.requested_cpu = None
            self.requested_machine_type = None
            self.requested_provisioning_mix = None
            self.pending_verification_start_time = None
            self.last_slurm_action = {
                "action": action.action,
                "job_id": self.active_job_id,
                "requested_cpu": target_cpu,
                "requested_machine_type": target_machine_type,
                "requested_provisioning_mix": target_provisioning_mix,
                "observed_cpu": self.observed_cpu,
                "observed_machine_type": self.observed_machine_type,
                "observed_provisioning_mix": self.observed_provisioning_mix,
                "status": "unsupported",
                "verification_status": "unsupported",
                "error": detail,
                "timestamp": time.time(),
            }
            raise RuntimeError(detail)

        # 3. Job is PENDING: Submit resize to Slurm API.
        #    Store the canonical provisioning spelling so the later comparison
        #    against partition telemetry cannot produce a spurious mismatch.
        self.requested_cpu = target_cpu
        self.requested_machine_type = target_machine_type
        self.requested_provisioning_mix = (
            normalize_slurm_provisioning(target_provisioning_mix) or target_provisioning_mix
        )
        target_provisioning_mix = self.requested_provisioning_mix
        self.verification_status = "pending_verification"
        self.pending_verification_start_time = time.time()
        self.verification_detail = (
            f"Resize requested ({target_cpu} CPUs, {target_machine_type}, {target_provisioning_mix}); "
            f"awaiting Slurm controller confirmation"
        )

        job_patch: dict[str, Any] = {"cpus_per_task": target_cpu}
        # The partition pins the machine type (see submit_job); a machine-type
        # constraint is rejected by slurmctld as an invalid feature.

        for part_name, part_info in SUPPORTED_SLURM_PARTITIONS.items():
            if part_info["machine_type"] == target_machine_type:
                job_patch["partition"] = part_name
                break

        comment_str = f"machine_type={target_machine_type};provisioning_model={target_provisioning_mix}"
        job_patch["comment"] = comment_str
        job_patch["admin_comment"] = comment_str

        resp = None
        status_code = None
        error_msg = None
        try:
            resp = requests.post(
                f"{self.base_url}/job/{self.active_job_id}",
                headers=self._headers(),
                json={"job": job_patch},
                timeout=5.0,
            )
            status_code = resp.status_code
        except Exception as e:
            error_msg = str(e)

        if resp is not None and resp.status_code in (200, 201):
            # HTTP 200 confirms Slurm received the request.
            # Observed allocated_cpu and metadata remain unchanged until confirmed by _sync_job_state
            self.last_slurm_action = {
                "action": action.action,
                "job_id": self.active_job_id,
                "requested_cpu": target_cpu,
                "requested_machine_type": target_machine_type,
                "requested_provisioning_mix": target_provisioning_mix,
                "observed_cpu": self.observed_cpu,
                "observed_machine_type": self.observed_machine_type,
                "observed_provisioning_mix": self.observed_provisioning_mix,
                "status_code": status_code,
                "status": "pending_verification",
                "verification_status": "pending_verification",
                "timestamp": self.pending_verification_start_time,
            }
        elif os.getenv("MOCK_SLURM", "").lower() in ("true", "1") or str(self.active_job_id).startswith("mock-"):
            self.last_slurm_action = {
                "action": action.action,
                "job_id": self.active_job_id,
                "requested_cpu": target_cpu,
                "requested_machine_type": target_machine_type,
                "requested_provisioning_mix": target_provisioning_mix,
                "observed_cpu": self.observed_cpu,
                "observed_machine_type": self.observed_machine_type,
                "observed_provisioning_mix": self.observed_provisioning_mix,
                "status_code": status_code or 200,
                "status": "pending_verification",
                "verification_status": "pending_verification",
                "simulated": True,
                "timestamp": self.pending_verification_start_time,
            }
        else:
            detail = resp.text if resp is not None else error_msg
            self.verification_status = "failed"
            self.verification_detail = f"Slurm rejected CPU resize to {target_cpu}: {detail}"
            self.requested_cpu = None
            self.requested_machine_type = None
            self.requested_provisioning_mix = None
            self.pending_verification_start_time = None
            self.last_slurm_action = {
                "action": action.action,
                "job_id": self.active_job_id,
                "requested_cpu": target_cpu,
                "requested_machine_type": target_machine_type,
                "requested_provisioning_mix": target_provisioning_mix,
                "observed_cpu": self.observed_cpu,
                "observed_machine_type": self.observed_machine_type,
                "observed_provisioning_mix": self.observed_provisioning_mix,
                "status_code": status_code,
                "status": "failed",
                "verification_status": "failed",
                "error": detail,
                "timestamp": time.time(),
            }
            raise RuntimeError(
                f"Slurm rejected CPU resize to {target_cpu} for job {self.active_job_id} (HTTP {status_code}): {detail}"
            )

    def tick(self, minutes: float) -> None:
        self._check_verification_timeout()
        if self.job_done:
            return

        cost_factor = self._get_cost_factor()

        # 1. Query real Slurm status first as primary source of truth
        job_info = self._get_job()
        if "jobs" in job_info and job_info["jobs"]:
            self.is_real_slurm_job = True
            self._sync_job_state(job_info["jobs"][0])
            return

        # Case 4.3 fix: If Slurm is inaccessible, never silently simulate completion unless explicit mock mode
        is_mock_enabled = os.getenv("MOCK_SLURM", "").lower() in ("true", "1", "yes") or str(self.active_job_id).startswith("mock-")
        if not is_mock_enabled:
            raise RuntimeError(
                f"Slurm cluster is unreachable at {self.base_url} (no active job {self.active_job_id} found) and MOCK_SLURM is not enabled."
            )

        # Fallback for mock/test runs with explicit mock mode
        self.elapsed_minutes += minutes
        self.accrued_cost_eur += (
            self.allocated_cpu * self.cpu_cost_per_hour_eur * cost_factor * (minutes / 60.0)
        )
        speedup = self.allocated_cpu / 16.0
        work_completed = (minutes / self.baseline_workload_duration_minutes) * 100.0 * speedup
        self.remaining_work_units = max(0.0, self.remaining_work_units - work_completed)
        if self.remaining_work_units <= 0.0:
            self.job_done = True
            self.job_status = "COMPLETED"
            self.job_failed = False
            self.remaining_work_units = 0.0

    def is_done(self) -> bool:
        return self.job_done
