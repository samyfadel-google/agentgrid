from __future__ import annotations

import time

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClusterState(BaseModel):
    current_time_minutes: float = Field(ge=0)
    total_cpu: int = Field(gt=0)
    free_cpu: int = Field(ge=0)
    total_gpu: int = Field(ge=0, default=0)
    free_gpu: int = Field(ge=0, default=0)


class WorkloadState(BaseModel):
    id: str
    kind: str
    remaining_work_units: float = Field(ge=0)
    allocated_cpu: int = Field(gt=0)
    allocated_gpu: int = Field(ge=0, default=0)
    estimated_remaining_minutes: float = Field(ge=0)
    accrued_cost_eur: float = Field(ge=0)
    done: bool = False
    status: str = "RUNNING"
    failed: bool = False
    machine_type: str | None = None
    provisioning_mix: str | None = None
    # Requested vs Observed configuration separation
    requested_cpu: int | None = None
    requested_machine_type: str | None = None
    requested_provisioning_mix: str | None = None
    observed_cpu: int | None = None
    observed_machine_type: str | None = None
    observed_provisioning_mix: str | None = None
    verification_status: Literal[
        "verified", "pending_verification", "unverified", "mismatch", "unsupported", "failed"
    ] = "unverified"
    verification_detail: str | None = None
    cost_basis: str = "unverified"
    #: False when no actual job stands behind this record. The Slurm adapter
    #: keeps a placeholder (job "1", PENDING) until it submits a job or finds
    #: the one it was told to track, and that placeholder used to be shown,
    #: and diagnosed, as a real job waiting on the cluster.
    job_found: bool = True

    @model_validator(mode="after")
    def sync_observed(self) -> "WorkloadState":
        if self.verification_status in ("verified", "mismatch"):
            if self.observed_cpu is None:
                self.observed_cpu = self.allocated_cpu
            if self.observed_machine_type is None and self.machine_type is not None:
                self.observed_machine_type = self.machine_type
            if self.observed_provisioning_mix is None and self.provisioning_mix is not None:
                self.observed_provisioning_mix = self.provisioning_mix
        return self


class Objective(BaseModel):
    deadline_at_minutes: float = Field(gt=0)
    minimize_cost: bool = True
    max_cost_eur: float | None = Field(default=None, gt=0)


class CandidateAllocation(BaseModel):
    cpu: int = Field(gt=0)
    estimated_remaining_minutes: float = Field(ge=0)
    projected_finish_at_minutes: float = Field(ge=0)
    projected_total_cost_eur: float = Field(ge=0)
    meets_deadline: bool
    within_budget: bool
    machine_type: str | None = None
    rank: int = 1
    provisioning_mix: str | None = None
    obtainability_score: float | None = None


class RuntimeSnapshot(BaseModel):
    cluster: ClusterState
    workload: WorkloadState
    objective: Objective
    candidate_allocations: list[CandidateAllocation]


class Action(BaseModel):
    action: Literal["resize_workload", "noop"]
    workload_id: str
    cpu: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=1000)
    machine_type: str | None = None
    provisioning_model: str | None = None

    @model_validator(mode="after")
    def validate_cpu_for_resize(self) -> "Action":
        if self.action == "resize_workload" and self.cpu is None:
            raise ValueError("cpu is required for resize_workload")
        return self



class WorkloadProfile(BaseModel):
    """Profile of a computation, its resource requirements, constraints, and resilience capabilities.

    Unknown keys are rejected. Pydantic's default is to drop them silently, which
    turned a plausible-but-wrong key such as ``budget_eur`` or ``cpu_count`` into
    a *dropped constraint*: the caller declared a budget, the engine never saw
    one, and the answer came back "feasible". A rejected request is honest; a
    silently ignored constraint is not.
    """

    model_config = ConfigDict(extra="forbid")

    workload_id: str
    name: str = "compute-workload"
    command: str | None = None
    script: str | None = None
    input_data_uri: str | None = None
    output_data_uri: str | None = None
    
    # Hardware requests
    cpu_requested: int | None = None
    gpu_requested: int = 0
    memory_mb_requested: int | None = None
    nodes_requested: int = 1
    tasks_requested: int = 1
    hardware_constraints: list[str] = Field(default_factory=list)
    
    # Financial & Time SLAs
    budget_amount: float | None = None
    budget_currency: str = "EUR"
    deadline_minutes_from_start: float | None = None
    deadline_iso: str | None = None
    timezone: str = "UTC"
    allowed_regions: list[str] = Field(default_factory=lambda: ["us-central1"])
    allowed_zones: list[str] = Field(default_factory=list)
    
    # Performance estimation
    estimated_duration_minutes: float | None = None
    estimation_source: str | None = None  # "historical_average", "user_specified", "dry_run", "unknown"
    
    # Elasticity, Interruption & Checkpoint
    is_parallelizable: bool = True
    is_interruptible: bool = True
    supports_checkpointing: bool = False
    checkpoint_interval_minutes: float | None = None
    checkpoint_location: str | None = None
    
    # Strict preferences & limits
    allow_spot: bool = True
    allow_fallback_to_standard: bool = True
    allow_zone_change: bool = True
    allow_region_change: bool = False  # Strongly coupled workloads must not be arbitrarily split across regions
    max_retries: int = 3
    max_cost_limit_eur: float | None = None
    max_wait_minutes: float | None = None


class ExecutionAttempt(BaseModel):
    """Represents a specific physical execution attempt of a workload."""
    attempt_id: str
    workload_id: str
    attempt_number: int = 1
    job_id: str | None = None
    status: str = "PENDING"  # PENDING, RUNNING, COMPLETED, FAILED, PREEMPTED, CANCELLED
    started_at: float | None = None
    ended_at: float | None = None
    elapsed_minutes: float = 0.0
    allocated_machine_type: str | None = None
    allocated_cpu: int = 0
    allocated_gpu: int = 0
    provisioning_mode: str = "100% Standard"
    region: str = "us-central1"
    zone: str | None = None
    checkpoint_recovered_from: str | None = None
    failure_reason: str | None = None
    cost_calculated_eur: float = 0.0
    cost_status: Literal["estimated", "calculated_from_usage", "reconciled_billed"] = "calculated_from_usage"
    events: list[dict[str, Any]] = Field(default_factory=list)


class CapacityCandidate(BaseModel):
    """Candidate compute capacity identified across catalog, quotas, and availability signals."""
    machine_type: str
    quantity: int = 1
    cpu_count: int
    memory_gb: float
    region: str
    zone: str | None = None
    provisioning_model: str = "SPOT"  # SPOT, STANDARD
    compatibility: Literal["COMPATIBLE", "INCOMPATIBLE", "UNKNOWN"] = "COMPATIBLE"
    compatibility_details: str | None = None
    
    quota_status: Literal["QUOTA_AVAILABLE", "QUOTA_EXCEEDED", "QUOTA_UNKNOWN"] = "QUOTA_AVAILABLE"
    quota_limit: int | None = None
    quota_usage: int | None = None
    # Which project the quota figures came from, and whether the operator chose
    # it. Without this, a figure read from the built-in default project read as
    # if it were the operator's own quota.
    quota_project: str | None = None
    quota_project_source: str | None = None
    # Which quota the figures were read from (e.g. CPUS, PREEMPTIBLE_CPUS): a
    # Spot row checked against the standard CPUS quota must not read as if it
    # had been checked against a Spot quota.
    quota_metric: str | None = None
    
    capacity_signal: Literal["HIGH", "MEDIUM", "LOW", "UNAVAILABLE", "SIMULATED", "UNKNOWN"] = "HIGH"
    obtainability_score: float | None = None
    preemption_risk: str | None = None
    estimated_uptime_minutes: float | None = None
    
    data_provenance: Literal["gcp_live_api", "simulated_demo", "unavailable", "unknown"] = "gcp_live_api"
    timestamp: float = Field(default_factory=time.time)
    state_stage: Literal["catalog_proposed", "quota_authorized", "capacity_estimated", "actually_allocated"] = "catalog_proposed"


class DiagnosticItem(BaseModel):
    """Structured diagnostic finding for workload blockers and execution impediments."""
    category: Literal[
        "resource_waiting",
        "priority",
        "dependencies",
        "quota",
        "capacity_shortage",
        "incompatible_configuration",
        "application_error",
        "unknown",
    ]
    observed_facts: str
    source: str  # "slurm_controller", "gcp_capacity_advisor", "gcp_compute_api", "workload_log"
    timestamp: float = Field(default_factory=time.time)
    confirmed: bool = True  # True if confirmed by facts, False if unverified hypothesis
    hypothesis_details: str | None = None
    possible_actions: list[dict[str, str]] = Field(default_factory=list)  # list of {"action": "...", "consequences": "..."}


class ExecutionPlan(BaseModel):
    """Evaluated execution plan comparing trade-offs between cost, latency, and capacity."""
    plan_id: str
    plan_type: Literal["cost_optimized", "deadline_favored", "balanced_tradeoff", "fallback_alternative"]
    title: str
    machine_type: str
    cpu: int
    gpu: int = 0
    region: str = "us-central1"
    zone: str | None = None
    provisioning_model: str = "100% Spot"
    quantity: int = 1
    
    satisfied_constraints: list[str] = Field(default_factory=list)
    unverified_points: list[str] = Field(default_factory=list)
    
    estimated_cost_eur: float
    cost_scope_included: list[str] = Field(default_factory=lambda: ["vm_compute_hourly"])
    cost_exclusions_known: list[str] = Field(default_factory=lambda: ["network_egress", "persistent_disk_storage"])
    # How the cost figure above was obtained. The plan engine computes this; without
    # the field declared here Pydantic silently dropped it and the API presented a
    # modelled number as if it were a quoted price.
    cost_basis: str = "modelled_flat_rate_hypothesis"
    cost_basis_detail: str | None = None
    # False when the currently attached runtime cannot actually run the plan.
    executable_on_runtime: bool = True

    # The capacity dimension of "cost / delay / capacity". This class claimed to
    # compare capacity and carried no field for it: two plans could differ by 80
    # vCPUs and be presented as equally obtainable. NOT_CHECKED is the honest
    # default -- the verdict is only filled in when a quota source was actually
    # consulted, and QUOTA_UNKNOWN is kept distinct from QUOTA_AVAILABLE.
    capacity_status: Literal[
        "QUOTA_AVAILABLE", "QUOTA_EXCEEDED", "QUOTA_UNKNOWN", "NOT_CHECKED"
    ] = "NOT_CHECKED"
    capacity_detail: str | None = None
    capacity_source: str = "not_checked"
    
    estimated_wait_minutes: float = 0.0
    estimated_prep_minutes: float = 0.0
    estimated_execution_minutes: float = 0.0
    estimated_recovery_minutes: float = 0.0
    total_time_to_result_minutes: float = 0.0
    
    performance_assumptions: str = "Amdahl scaling model based on estimated parallel fraction"
    uncertainty_factors: list[str] = Field(default_factory=list)
    ranking_rationale: str = "Deterministic ranking based on objective trade-offs"
    fallback_plan_id: str | None = None
    fallback_chain: list[str] = Field(default_factory=list)

    # 4D Pareto Frontier, Regional Carbon Telemetry & Young-Daly Resilience
    carbon_emissions_g_co2: float = 0.0
    energy_kwh: float = 0.0
    carbon_intensity_g_per_kwh: float = 0.0
    green_tier: str = "MODERATE_CARBON"
    young_daly_optimal_checkpoint_minutes: float | None = None
    expected_cost_with_preemption_eur: float | None = None
    interruption_risk_score: float = 0.0
    is_pareto_optimal: bool = True
    pareto_rank: int = 1
    utility_scores: dict[str, float] = Field(default_factory=dict)
    carbon_metrics: dict[str, Any] = Field(default_factory=dict)
    resilience_metrics: dict[str, Any] = Field(default_factory=dict)



class DelegationPolicy(BaseModel):
    """Operator-defined guardrail policy for delegated execution mode."""
    max_budget_eur: float = 100.0
    allowed_machine_types: list[str] = Field(default_factory=list)
    allowed_provisioning_models: list[str] = Field(default_factory=list)
    allowed_regions: list[str] = Field(default_factory=list)
    max_retries: int = 3
    auto_approve_if_within_policy: bool = True


class ExecutionHistoryRecord(BaseModel):
    """Historical execution record persisted across application restarts."""
    workload_id: str
    workload_name: str
    profile: WorkloadProfile
    plans_evaluated: list[ExecutionPlan] = Field(default_factory=list)
    approved_plan: ExecutionPlan | None = None
    control_mode: str = "validation"  # "advisory", "validation", "delegation"
    attempts: list[ExecutionAttempt] = Field(default_factory=list)
    final_status: str = "COMPLETED"
    initial_estimated_cost_eur: float = 0.0
    final_calculated_cost_eur: float = 0.0
    cost_comparison_delta_eur: float = 0.0
    initial_estimated_duration_minutes: float = 0.0
    final_actual_duration_minutes: float = 0.0
    duration_comparison_delta_minutes: float = 0.0
    reconciliation_status: Literal["estimated", "calculated_from_usage", "reconciled_billed"] = "calculated_from_usage"
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
