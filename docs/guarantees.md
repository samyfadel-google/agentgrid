# How AgentGrid keeps its promises

The [README](../README.md) says what AgentGrid guarantees. This page says how each guarantee is enforced, and
which test pins it. Test files live in [`tests/`](../tests).

- [Deterministic and agentic responsibilities](#deterministic-and-agentic-responsibilities)
- [Plan comparison](#plan-comparison)
- [Governance, execution and fallback](#governance-execution-and-fallback)
- [Slurm verification](#slurm-verification)
- [Capacity search](#capacity-search)
- [Blocker diagnosis](#blocker-diagnosis)
- [Lifecycle and resumption](#lifecycle-and-resumption)
- [Cost, history and reconciliation](#cost-history-and-reconciliation)
- [Spot capacity and the Capacity Advisor](#spot-capacity-and-the-capacity-advisor)
- [What is verified, and what is not](#what-is-verified-and-what-is-not)

## Deterministic and agentic responsibilities

AgentGrid never asks a language model to do arithmetic or validation that deterministic software does exactly.
The model never runs raw shell commands or cluster scripts: it acts only through typed, validated tools.

| Responsibility | Deterministic backend (plan engine, runtime, governance) | Gemini agent (ADK, optional) |
| :--- | :--- | :--- |
| Cluster metrics | Reads live CPU and GPU allocations and queue depth | Interprets workload health and bottlenecks |
| Candidate projections | Computes Amdahl speedup, times and costs | Weighs the trade-offs against business targets |
| Safety and quota validation | Rejects unsupported machine types and invalid provisioning | Works within the operator's constraints |
| Actuation | Makes atomic REST calls to `slurmrestd` or the simulator | Decides when to run, and which candidate |
| Ground-truth verification | Confirms real allocations on the nodes and enforces timeouts | Explains the rationale to the operator |

## Plan comparison

- **Up to three plans**, each distinct:
  - **Lowest cost** (`cost_optimized`): the lowest estimated spend that meets the deadline.
  - **Fastest** (`deadline_favored`): the shortest time to result within the budget.
  - **Balanced** (`balanced_tradeoff`): a middle candidate, with hedged Spot and Standard provisioning when Spot
    is allowed. It is only shown when it differs from the other two.
- **Spot only where it can run.** Spot is offered only when the workload tolerates interruption, and never on
  the Slurm runtime, whose partitions run Standard capacity.
- **An honest saving.** The dashboard states the recommended plan's saving against the fastest plan compared
  for the same deadline, never against an invented baseline.
- **Cost scope.** Costs are a deterministic model at 0.050 EUR per vCPU-hour by default, not an observed or
  quoted cloud price. The included scope (`vm_compute_hourly`) and the known exclusions (`network_egress`,
  `persistent_disk_storage`) are part of every plan.
- **Capacity is the third axis, not a slogan.** Every plan carries `capacity_status` (`QUOTA_AVAILABLE` /
  `QUOTA_EXCEEDED` / `QUOTA_UNKNOWN` / `NOT_CHECKED`), `capacity_detail` and `capacity_source`, read from the
  Compute Engine quota API when a project is configured. An unreadable quota stays `QUOTA_UNKNOWN`, which is
  deliberately not the same statement as available, and an exceeded quota is also folded into the plan's
  `unverified_points`. Set `AGENTGRID_PLAN_CAPACITY_CHECK=false` to skip the lookup: the plans then report
  `NOT_CHECKED` rather than an assumed availability.
- **No invented winner.** If the constraints conflict, the answer is an empty plan set with an explanation of
  what blocks and what could be relaxed.

Tests: `tests/test_evolved_capabilities.py`, `tests/test_agentgrid_evolutions.py`,
`tests/test_plan_capacity_dimension.py`.

## Governance, execution and fallback

Every mutation is governed by an operator control mode. The mode is held by the server: a caller cannot grant
itself permission.

- **Three control modes:**
  - **Advisory** (`advisory`): read-only suggestions. Every mutation and submission is rejected.
  - **Validation** (`validation`, the default): execution is blocked until the operator approves the plan.
  - **Delegation** (`delegation`): execution bounded by `DelegationPolicy` guardrails (budget ceiling, allowed
    machine types, allowed regions, allowed provisioning models, retry count) without stopping for each
    approval.
- **The delegated budget is a cumulative ceiling, reconstructed server-side.** It is not a per-action check.
  Before each submission the server sums what this workload has already committed (every ledger row in
  `claimed`, `submitted` or `uncertain` state, priced at claim time) and adds the plan being proposed. A caller
  may pass its own `accumulated_cost_eur`, but the server takes `max(caller, ledger)`, so a caller can only ever
  *tighten* the bound. Three distinct 8 EUR plans under a 10 EUR delegation therefore yield one submission and
  two refusals, not three jobs. `GET /api/workloads/{id}/control` reports `committed_cost_eur` and
  `remaining_delegated_budget_eur`. A `failed` submission created nothing and is not charged; an `uncertain`
  one may have, so it is. Ledger rows written before submissions were priced are surfaced as
  `unpriced_prior_submissions` rather than counted as free. The same rule applies to every rung of the fallback
  ladder.
- **The retry ceiling is enforced the same way.** Every claim ever won for a workload counts as a launch, and
  releasing a claim archives it instead of deleting it: otherwise a release-and-retry loop erases its own
  history and runs forever. A released `submitted` or `uncertain` claim keeps its cost charged, since a job
  existed or may have. A released `failed` claim created nothing and is refunded.
- **Both ceilings are evaluated inside the claim's own transaction.** Checking on one connection and inserting
  on another is a time-of-check to time-of-use race: four *different* plans submitted concurrently do not share
  a submission key, so the primary key does not separate them. `claim_submission_within_limits` takes the write
  lock with `BEGIN IMMEDIATE` before counting.
- **Exactly-once submission.** A submission is claimed in the SQLite ledger, keyed by workload and plan
  fingerprint, *before* the runtime is touched. The claim is the primary key itself, so a double click, a
  replayed HTTP request or a process restart resolves to the same job instead of creating a second one. An
  ambiguous timeout is resolved by looking up that identity, not by guessing.
- **The constraints declared on the workload bind execution, not just planning.** `allowed_regions`,
  `allowed_zones`, `allow_spot`, `allow_region_change`, `allow_zone_change` and `allow_fallback_to_standard` are
  checked on every submission, in every mode: an approval authorises *a plan*, it does not repeal a location
  limit. They are checked against the profile the server persisted when the plans were compared, so a caller
  cannot widen its own constraints in the execution request. The refusal names which profile refused
  (`constraint_source`).
- **The controlled fallback is reachable, and bound by the same constraints.** `POST
  /api/workloads/{id}/fallback` and the MCP tool `select_fallback_plan` return the next authorised rung.
  Advisory refuses outright, the delegated ceilings are recomputed from the ledger, and rungs that break the
  workload's constraints come back in `skipped_candidates` with their reason. `constraint_breach` is returned
  when none survives. When no profile can be read at all, `profile_constraints_source: unavailable` says so
  instead of implying the rung was checked. Outside delegation the verdict carries `requires_approval: true`.
  The decision never submits: `submitted: false`, and the plan still has to go through `/api/execute-plan`. The
  dashboard shows each plan's declared fallback.

Tests: `tests/test_http_journey.py`, `tests/test_mcp_http_governance.py`,
`tests/test_delegation_budget_ceiling.py`, `tests/test_concurrent_submission.py`,
`tests/test_profile_constraints.py`, `tests/test_fallback_exposure.py`.

## Slurm verification

The Slurm adapter ([`slurm_adapter.py`](../src/agentic_compute/slurm_adapter.py)) never takes its own request
as proof of what happened.

1. **Independent observation.** The observed machine type and provisioning are derived from controller
   telemetry (the partition the job runs in), never echoed from the request.
2. **Allocated, not requested.** A pending job stays in `pending_verification` until the controller assigns
   resources to it (`allocated_cpus > 0`).
3. **Command outcome and telemetry stay separate.** A command rejected by Slurm (HTTP 400 or 500) remains
   `failed`. Ambient cluster telemetry cannot turn a rejected command into `applied`.
4. **Verification deadline.** A change that the controller does not confirm within
   `SLURM_VERIFICATION_TIMEOUT_SECS` (60 s by default) is reported as timed out instead of lingering.
5. **No placeholder job.** Before any job exists, the adapter reports `job_found: false`, and the dashboard
   shows no current job instead of a fictitious one.
6. **What is submitted.** Partition, CPUs per task, memory and GPUs when requested, and the command. The
   requested machine type is recorded in the job comment. Node features and constraints are not sent: on the
   reference cluster machine types are not node features, and the partition pins the machine type. A command
   without an interpreter line gets `#!/bin/bash`.
7. **Usage-based cost.** Cost accrues from the controller's elapsed time and allocated CPUs, at the configured
   rate (`CPU_COST_PER_HOUR_EUR`, 0.05 by default). It is a usage figure, not a billed price.

Tests: `tests/test_slurm.py`, `tests/test_slurm_telemetry_defects.py`, `tests/test_no_invented_facts.py`,
`tests/test_execution_runtime_consistency.py`.

## Capacity search

- **Four stages per candidate:** `catalog_proposed` (CPU, memory, GPU, AVX-512 match), `quota_authorized`
  (`QUOTA_AVAILABLE`, `QUOTA_EXCEEDED` or `QUOTA_UNKNOWN`), `capacity_estimated` (Capacity Advisor signals) and
  `actually_allocated` (verified on the scheduler).
- **Truthful provenance.** Each figure says where it comes from: `gcp_live_api`, `simulated_demo` or
  `unavailable`.
- **Every permitted location is searched.** The search covers each region in the workload's `allowed_regions`,
  not just the first, so a shortage in one region is not reported as "no compatible capacity" while other
  authorised regions were never queried. An explicit `region` narrows the search back to that region. A single
  search is capped at `MAX_SEARCH_REGIONS` (5) and names the regions it left out. The response carries
  `searched_regions` and, when location constraints exclude everything, a `location_note` saying which region
  was asked for and which were permitted. The search and the note come from the same resolver, so they cannot
  disagree.
- **Each region's quota document is read once per search**, not once per machine type (30 s TTL,
  `AGENTGRID_QUOTA_CACHE_TTL=0` to read through). Only successful reads are cached: a credential failure or a
  503 reaches the caller every time it happens.

Tests: `tests/test_capacity_location_constraints.py`, `tests/test_multi_region_search.py`,
`tests/test_quota_fetch_cache.py`, `tests/test_capacity_location_reaches_the_agent.py`,
`tests/test_quota_api_contract.py`.

## Blocker diagnosis

Blockers are classified into:

- `resource_waiting`: cluster saturation, or a cloud VM still starting.
- `priority`: queued behind higher-priority work.
- `dependencies`: upstream dependencies, or user and admin holds.
- `quota`: a ceiling was reached, **and the finding names who holds it.** A GCP regional or project quota is
  sourced `gcp_compute_quota` and remedied by a quota request. A Slurm QoS or association ceiling
  (`QOSMaxJobsPerUserLimit`, `AssocGrpCPURunMinutes`, …) is sourced `slurm_controller` and remedied by reducing
  parallelism, waiting for the account's own jobs, or `sacctmgr`. Both are reported when both apply.
- `capacity_shortage`: cloud stockouts (`ZONE_RESOURCE_POOL_EXHAUSTED`) and preemption spikes.
- `incompatible_configuration`: invalid constraints (`BadConstraints`) and unsupported shapes.
- `application_error`: non-zero exit codes (for example 137 for an OOM kill, 139 for a segmentation fault),
  with targeted remediations.

A reason the controller did not report is never invented. Tests: `tests/test_scheduler_limit_attribution.py`,
`tests/test_diagnostics_slurm_record.py`, `tests/test_no_invented_facts.py`.

## Lifecycle and resumption

- **Lifecycle states:** `DEFINED` → `PLANNING` → `READY_FOR_APPROVAL` → `SUBMITTING` → `QUEUED` → `RUNNING` →
  `COMPLETED`, `PREEMPTED`, `FAILED` or `CANCELLED`. The workload identity (`workload_id`) is separate from its
  attempts (`attempt_id`).
- **Progress has three states, never conflated:** `measured` (read from the runtime), `declared_unverified` (a
  figure someone stated, including the language model through the MCP tools) and `unavailable`.
- **A resume is checked before it is promised.** The checkpoint location is inspected: a local path is read,
  and a `gs://` URI is listed when the Cloud Storage client is available. The outcomes are `verified_present`,
  `verified_absent` and `unverified`, and a retry only records a recovery against a checkpoint verified present.
  A workload with an empty checkpoint location is told it restarts from 0%.

Tests: `tests/test_checkpoint_verification.py`, `tests/test_cost_accounting_journey.py`.

## Cost, history and reconciliation

- **Persistent SQLite storage** of profiles, plans, attempts and post-mortems (`AGENTGRID_DB_PATH`, by default
  `~/.agentgrid/agentgrid_history.db`). On Cloud Run the file lives in the container, so it is lost when the
  instance is replaced.
- **Three cost tiers:**
  1. `initial_estimated_cost_eur`: the estimate before execution.
  2. `calculated_from_usage_eur`: elapsed node-hours at the configured rate, from verified telemetry.
  3. `reconciled_billed_cost_eur`: only labelled `reconciled_billed` when the caller names
     `billed_cost_source="gcp_billing_export"`. There is no billing integration in this project, so a figure
     simply handed to the API is reported as `caller_supplied_unverified`, and the absence of any figure as
     `source_not_integrated`.
- Tier 2 counts **only** attempts whose `cost_status` is `calculated_from_usage`. A figure a caller merely
  stated (`cost_status="estimated"`, which is what the MCP tool records) is reported separately as
  `declared_unverified_cost_eur`. `total_recorded_cost_eur` still holds everything.
- **Known gap:** the runtime's measured cost and duration are not yet written back to the Ledger after a run,
  so the dashboard says "none measured yet" even after a verified, completed job.

Tests: `tests/test_cost_reconciliation_honesty.py`, `tests/test_cost_accounting_journey.py`.

## Spot capacity and the Capacity Advisor

- With credentials, capacity search calls the Compute Engine Capacity Advisor (`advice/capacity` and
  `advice/capacityHistory`) for Spot obtainability, the recommended zone and the 7-day preemption rate. Live
  answers carry `data_provenance: gcp_live_api`. Otherwise the figures are labelled `simulated_demo` (simulator
  or `DEMO_MODE`) or `unavailable`, never passed off as live.
- Spot is checked against the regular `CPUS` quota when the project has no dedicated `PREEMPTIBLE_CPUS` quota,
  as [GCP documents](https://cloud.google.com/compute/resource-usage#preemptible_quotas).
- **Hedged provisioning is agent guidance, not an enforced rule.** The agent's instructions
  ([`agent.py`](../compute_agent/agent.py)) use the deadline slack ratio
  S = (deadline − elapsed) / remaining time: above 1.5, Spot; between 1.1 and 1.5, a hedge such as 80% Spot and
  20% Standard; at 1.1 or below, or with a preemption rate above 25%, Standard. The plan engine does not apply
  this rule.

## What is verified, and what is not

| Area | Status | How it was checked |
| :--- | :--- | :--- |
| Governance (advisory, validation, delegation), plan registration, fingerprint-bound approval, idempotent submission | **Verified** | `tests/test_http_journey.py`, `tests/test_mcp_http_governance.py`, and a full journey with `curl` against a live server |
| Cumulative delegated budget ceiling (server-derived, a caller cannot understate it) | **Verified** | `tests/test_delegation_budget_ceiling.py`: 9 of its 10 controller tests fail against the previous code |
| Idempotency and delegated ceilings under real concurrency | **Verified** | `tests/test_concurrent_submission.py`: 8 racing threads yield one job and one charge |
| Plan comparison, infeasibility explanations, constraint rejection | **Verified** | `tests/test_evolved_capabilities.py`, `tests/test_agentgrid_evolutions.py` |
| Location constraints and quota staging | **Verified** | `tests/test_capacity_location_constraints.py` |
| Lifecycle, checkpoint verification, cost accounting across attempts and restarts | **Verified** | `tests/test_checkpoint_verification.py`, `tests/test_cost_accounting_journey.py` |
| GCP quota read (regional `CPUS` and `NVIDIA_*_GPUS`, and the project-wide `GPUS_ALL_REGIONS` ceiling) | **Exercised against the real API** | a live `/api/capacity-search` returned `data_provenance: gcp_live_api` with a genuine `QUOTA_EXCEEDED`; response shapes pinned in `tests/test_quota_api_contract.py` against the published [`regions.get`](https://cloud.google.com/compute/docs/reference/rest/v1/regions/get) and [`projects.get`](https://cloud.google.com/compute/docs/reference/rest/v1/projects/get) contracts |
| Measured cost kept apart from declared cost; no unsourced figure called "reconciled billing" | **Verified** | `tests/test_cost_reconciliation_honesty.py`: 10 of its 11 tests fail against the previous code |
| The workload's own constraints bind submission *and* every fallback rung | **Verified** | `tests/test_profile_constraints.py`: before the fix, a plan in a forbidden region was submitted with a real job id |
| A caller cannot widen its own constraints at execution time | **Verified** | `tests/test_profile_constraints.py` |
| The capacity axis of the cost, delay and capacity comparison | **Verified, and exercised against the real API** | `tests/test_plan_capacity_dimension.py` for the contract; against the live project, 88 vCPU plans answered `QUOTA_EXCEEDED ... available 0/0` with `data_provenance: gcp_live_api` |
| Every quota figure names the project it came from | **Verified** | `tests/test_plan_capacity_dimension.py`: `resolve_quota_project()` is the single resolver |
| The controlled fallback is reachable by an operator and by the agent | **Verified** | `tests/test_fallback_exposure.py`: `POST /api/workloads/{id}/fallback` and MCP `select_fallback_plan`; the decision never submits |
| A blocker keeps the origin that actually holds it | **Verified** | `tests/test_scheduler_limit_attribution.py`: a Slurm `QOSMaxJobsPerUserLimit` used to be reported as a cloud quota; 20 of the file's 39 tests fail against the previous code |
| Every region the operator permitted is searched, not just the first | **Verified** | `tests/test_multi_region_search.py`: one resolver for the search and the explanation; capped at 5 regions, naming the ones left out |
| One capacity search reads each region's quota document once | **Verified, measured against the real API** | `tests/test_quota_fetch_cache.py`: the same 12 live-API tests went from **88.7 s to 5.1 s** |
| The agent sees complete location metadata, not just candidates | **Verified** | `tests/test_capacity_location_reaches_the_agent.py`: 7 of 8 tests fail before the fix |
| Dashboard runs reach the runtime the Cluster tab shows | **Verified** | `tests/test_execution_runtime_consistency.py`, and the deploy script gives the agent service the Slurm settings |
| No placeholder job, no invented diagnosis reason | **Verified** | `tests/test_no_invented_facts.py`, `tests/test_dashboard_jsx.py` |
| Slurm adapter | **Mock `slurmrestd` only, so far** | `tests/test_slurm.py`, `tests/test_slurm_telemetry_defects.py`, and a real-browser run against a mock controller. On the reference cluster, one submission was authenticated and then rejected (`ESLURM_INVALID_FEATURE`) because the machine type was sent as a node feature; fixed, live re-run pending |
| Dashboard | **Compiled and rendered offline; driven in headless Chrome during development** | `tools/check_jsx.py`, `tools/render_check.py`, `tests/test_dashboard_jsx.py` |
| `gs://` checkpoints | **Not verifiable in the reference environment** | `google-cloud-storage` is not installed; the result is reported as `unverified`, never as present |
| Measured cost and duration in the Ledger | **Not implemented** | see [Cost, history and reconciliation](#cost-history-and-reconciliation) |
| Multi-tenant authentication | **Out of scope** | governance works without it, but there is no user identity model |
