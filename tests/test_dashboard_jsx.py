"""Guard the dashboard's inline JSX against syntax regressions.

The React app in `compute_agent/static/index.html` is compiled in the browser by
@babel/standalone. A syntax error there yields a blank page and a console-only
error, which every Python-level test would otherwise miss -- this is exactly how
the unterminated `{activeTab === 'mission' && (` block shipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.check_jsx import (  # noqa: E402
    DEFAULT_TARGET,
    JsxCheckError,
    check_file,
    extract_babel_blocks,
    syntax_diagnostics,
)


def _require_toolchain() -> None:
    try:
        syntax_diagnostics("const x = 1;")
    except JsxCheckError as exc:
        pytest.skip(f"JSX toolchain unavailable: {exc}")


def test_dashboard_jsx_compiles() -> None:
    """The shipped dashboard must parse as valid JSX."""
    _require_toolchain()
    errors = check_file(DEFAULT_TARGET)
    assert errors == [], "Dashboard JSX failed to compile:\n" + "\n".join(errors)


def test_dashboard_has_single_babel_block() -> None:
    """Sanity check that the checker is actually inspecting the app source."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    blocks = extract_babel_blocks(html)
    assert len(blocks) == 1
    # The block must contain the app, not just a stub.
    assert "function App" in blocks[0] or "const App" in blocks[0]
    assert "ReactDOM.createRoot" in blocks[0]


def test_checker_detects_adjacent_jsx_elements() -> None:
    """Negative control: the exact error class that shipped must be caught.

    Without this, a checker that silently returns "no diagnostics" for every
    input would pass `test_dashboard_jsx_compiles` while providing no coverage.
    """
    _require_toolchain()
    broken = """
    const Broken = () => {
      return (
        <div>first</div>
        <div>second</div>
      );
    };
    """
    diags = syntax_diagnostics(broken)
    assert diags, "checker failed to flag adjacent JSX elements"
    assert any(d["code"] == 2657 for d in diags), diags


def test_checker_detects_unterminated_conditional_block() -> None:
    """Negative control for the unclosed `{cond && (` pattern."""
    _require_toolchain()
    broken = """
    const Broken = () => {
      return (
        <div>
          {show && (
            <span>content</span>
        </div>
      );
    };
    """
    diags = syntax_diagnostics(broken)
    assert diags, "checker failed to flag an unterminated conditional block"


def test_checker_accepts_modern_syntax() -> None:
    """Optional chaining and template literals must not be false positives."""
    _require_toolchain()
    ok = """
    const Fine = ({ snap }) => {
      const label = snap?.workload?.status ?? "unknown";
      return <div className={`tag ${label}`}>{label}</div>;
    };
    """
    assert syntax_diagnostics(ok) == []


# ---------------------------------------------------------------------------
# Render-time verification
#
# There is no browser in this environment, so "it compiles" is not enough: an
# undefined variable or a missing handler compiles fine and only fails at
# render time. tools/render_check.py executes the component in an embedded JS
# engine against a minimal React. These tests drive it, including a negative
# control proving the harness would actually catch such a defect.
# ---------------------------------------------------------------------------

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


def test_every_dashboard_tab_renders_without_error():
    import render_check

    problems = render_check.check_file(render_check.DEFAULT_TARGET)
    assert problems == [], "\n".join(problems)


def test_render_harness_detects_an_undefined_reference():
    """Negative control: the harness must fail on a genuinely broken component."""
    import render_check

    source = """
      const { useState } = React;
      function App() {
        const [activeTab, setActiveTab] = useState("mission");
        return <div>{thisVariableDoesNotExist}</div>;
      }
      ReactDOM.createRoot(document.getElementById("root")).render(<App />);
    """
    outcome = render_check.render_tab(source, "mission")
    assert outcome["errors"], "an undefined reference was not reported"
    assert "ReferenceError" in outcome["errors"][0]


def test_render_harness_detects_a_tab_that_renders_nothing():
    """Negative control: a tab with no panel must be reported, not silently pass."""
    import render_check

    source = """
      const { useState } = React;
      function App() {
        const [activeTab, setActiveTab] = useState("mission");
        return <div>{activeTab === "mission" && (<span>Mission</span>)}</div>;
      }
      ReactDOM.createRoot(document.getElementById("root")).render(<App />);
    """
    mission = render_check.render_tab(source, "mission")
    assert "Mission" in mission["text"]

    plans = render_check.render_tab(source, "plans")
    assert "Mission" not in plans["text"]


def test_render_harness_detects_an_object_rendered_as_a_child():
    """Negative control: React refuses a plain object as a child (error #31).

    The diagnosis panel rendered each `{action, consequences}` dict directly.
    The fake React flattened it to nothing, while a browser threw and blanked
    the whole page the moment the Cluster view opened.
    """
    import render_check

    source = """
      const { useState } = React;
      function App() {
        const [activeTab, setActiveTab] = useState("run");
        const options = [{ action: "Wait", consequences: "No extra cost" }];
        return <ul>{options.map((opt, i) => <li key={i}>{opt}</li>)}</ul>;
      }
      ReactDOM.createRoot(document.getElementById("root")).render(<App />);
    """
    outcome = render_check.render_tab(source, "run")
    assert any(
        "Objects are not valid as a React child" in e and "action, consequences" in e
        for e in outcome["errors"]
    ), outcome


def test_render_harness_hands_jsx_children_to_components():
    """Children given to a component must be rendered, and checked, as React does.

    Without this, whatever sat inside <Tag> or <Fact> was invisible to the
    harness: no marker could be asserted there and no defect was caught there.
    """
    import render_check

    source = """
      const { useState } = React;
      function Label({ children }) { return <b>{children}</b>; }
      function App() {
        const [activeTab, setActiveTab] = useState("run");
        return (
          <div>
            <Label>visible text</Label>
            <Label>{{ reason: "an object" }}</Label>
          </div>
        );
      }
      ReactDOM.createRoot(document.getElementById("root")).render(<App />);
    """
    outcome = render_check.render_tab(source, "run")
    assert "visible text" in outcome["text"]
    assert any("found: object with keys {reason}" in e for e in outcome["errors"]), outcome


def test_dashboard_exposes_a_panel_for_every_navigation_tab():
    """Every tab button must have a matching conditional panel, and back.

    A visible tab with no panel behind it is exactly the "button with no
    effect" the acceptance criteria forbid; a panel no button reaches is dead
    code that still ships.
    """
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    for tab in ("run", "ledger", "cluster"):
        assert f'setActiveTab("{tab}")' in html, f"no navigation button for tab {tab}"
        assert f"activeTab === '{tab}' &&" in html, f"no rendered panel for tab {tab}"
    panels = set(re.findall(r"\{activeTab === '([a-z]+)' &&", html))
    targets = set(re.findall(r'setActiveTab\("([a-z]+)"\)', html))
    assert panels == targets, f"panels {sorted(panels)} vs navigation targets {sorted(targets)}"


def test_dashboard_control_mode_selector_is_pushed_to_the_server():
    """The mode is enforced server-side, so the selector must call the API."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    for mode in ("advisory", "validation", "delegation"):
        assert f'applyControlMode("{mode}")' in html
    assert "/api/workloads/${WORKLOAD_ID}/control" in html


def test_dashboard_sends_the_operator_values_not_hard_coded_ones():
    """Budget, deadline and size must each be transmitted from their own field."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "budget_amount: Number(budget)" in html
    assert "deadline_minutes_from_start: Number(deadline)" in html
    assert "cpu_requested: Number(cpuRequested)" in html
    # The previously hard-coded search parameters must be gone.
    assert "cpu_requested=16&memory_gb_requested=32" not in html


def test_dashboard_reports_the_server_verdict_for_approval_and_execution():
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    # Approval is only shown when the server said "approved".
    assert 'body.status === "approved"' in html
    assert "setApprovalError" in html
    # Execution shows whatever the server returned, including "blocked".
    assert "setExecutionResult(body)" in html
    assert "already_submitted" in html


# ---------------------------------------------------------------------------
# UI <-> model contract
#
# A React panel that reads a field the API never returns renders an empty cell
# and passes every syntax and render check. This caught real defects: the
# capacity table read `c.cpu` (the model exposes `cpu_count`), the diagnostics
# panel read `d.title`/`d.detail`/`d.severity` (the model exposes `category`/
# `observed_facts`/`confirmed`), and the history table read `r.state` and
# `r.estimated_cost_eur` (the model exposes `final_status` and
# `initial_estimated_cost_eur`).
# ---------------------------------------------------------------------------

import re

# Loop-variable convention the dashboard script follows, and this check relies
# on: `plan` is always an ExecutionPlan, `r` an ExecutionHistoryRecord, `c` a
# CapacityCandidate and `d` a DiagnosticItem. Scanning the whole script (not
# one tab panel) is what covers the module-scope helpers (`planName`,
# `ParetoScatter`) and the impact preview rendered outside every panel.
_CONTRACT_BINDINGS = {
    "plan": "ExecutionPlan",
    "r": "ExecutionHistoryRecord",
    "c": "CapacityCandidate",
    "d": "DiagnosticItem",
}


def _dashboard_script() -> str:
    return extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]


def _fields_read_on(source: str, variable: str) -> set[str]:
    """Property names read from `variable` inside the dashboard source."""
    return set(re.findall(rf"\b{re.escape(variable)}\.([A-Za-z_][A-Za-z0-9_]*)", source))


def _contract_problems(script: str) -> list[str]:
    from agentic_compute import models

    # Names that are JS built-ins or local helpers rather than model fields.
    js_builtins = {"map", "length", "filter", "toFixed", "join", "slice", "props"}

    problems = []
    for variable, model_name in _CONTRACT_BINDINGS.items():
        model = getattr(models, model_name)
        read = _fields_read_on(script, variable)
        if not read:
            # A renamed loop variable would otherwise turn this check off.
            problems.append(
                f"no field is read through `{variable}`: the naming convention "
                f"this contract check relies on ({model_name}) was broken"
            )
        allowed = set(model.model_fields) | js_builtins
        for field in sorted(read - allowed):
            problems.append(
                f"the dashboard reads `{variable}.{field}` but {model_name} has no such field"
            )
    return problems


def test_panels_only_read_fields_the_api_actually_returns():
    problems = _contract_problems(_dashboard_script())
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("variable", sorted(_CONTRACT_BINDINGS))
def test_the_contract_check_detects_a_field_that_does_not_exist(variable):
    """Negative control: without this, the test above could pass while blind."""
    script = _dashboard_script() + f"\n{{{variable}.definitely_not_a_field}}\n"
    problems = _contract_problems(script)
    assert any(f"`{variable}.definitely_not_a_field`" in p for p in problems), problems


def test_capacity_panel_shows_quota_and_provenance():
    """Quota state and data provenance must be visible, not implied."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "c.quota_status" in html
    assert "c.data_provenance" in html
    assert "c.state_stage" in html


def test_diagnostics_panel_keeps_the_source_of_each_finding():
    """A Slurm QoS limit and a GCP quota are different problems."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "d.source" in html
    assert "d.confirmed" in html


def test_the_waiting_panel_only_diagnoses_the_job_the_runtime_reports():
    """No job, or a running one, must not produce a diagnosis.

    With no job the panel used to send a hard-coded `PENDING / ReqNodeNotAvail`
    and showed the answer as a *confirmed* finding about a job that did not
    exist; with a running job it showed an "unknown / hypothesis" card.
    """
    import render_check

    source = _dashboard_script()
    assert "ReqNodeNotAvail" not in source, "the dashboard must not invent a Slurm state reason"

    def cluster_view(workload):
        snapshot = {
            "runtime": "simulator",
            "execution_runtime": "simulator",
            "snapshot_source": "local_runtime",
            "snapshot": {"cluster": {"total_cpu": 64, "free_cpu": 48, "total_gpu": 0, "free_gpu": 0}},
        }
        if workload is not None:
            snapshot["snapshot"]["workload"] = workload
        seeded = render_check.seed_state(source, {"snapshotPayload": snapshot})
        outcome = render_check.render_tab(seeded, "cluster")
        assert not outcome["errors"], outcome["errors"]
        return outcome["text"]

    assert "No job on the runtime yet." in cluster_view(None)

    running = cluster_view({"id": "job-7", "status": "RUNNING", "done": False, "failed": False})
    assert "Job job-7 is running: nothing is holding it back." in running
    assert "Check again" not in running

    failed = cluster_view({"id": "job-8", "status": "FAILED", "done": True, "failed": True})
    assert "Why did my job fail?" in failed
    assert "Check again" in failed


def test_history_panel_compares_estimated_and_observed_cost():
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "r.initial_estimated_cost_eur" in html
    assert "r.final_calculated_cost_eur" in html
    assert "r.cost_comparison_delta_eur" in html
    assert "r.reconciliation_status" in html


# ---------------------------------------------------------------------------
# Visualization & experience wiring
#
# Several analytics endpoints were computed server-side and read by nothing:
# /api/portfolio/finops, /api/workloads/{id}/anomalies, and the shock parameters
# of /api/plans/what-if. A panel that exists but calls no endpoint, or calls one
# and reads keys it does not return, is the same defect class the contract tests
# above already guard for the older tabs.
# ---------------------------------------------------------------------------


def test_finops_panel_calls_the_portfolio_endpoint():
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "/api/portfolio/finops" in html
    assert "setFinops(" in html
    assert "setFinopsError" in html


def test_finops_panel_reads_keys_the_analytics_payload_returns():
    """The board must read the aggregator's own key names, not invented ones."""
    from agentic_compute.anomaly_detector import compute_portfolio_finops_analytics

    payload = compute_portfolio_finops_analytics(records=[])
    html = DEFAULT_TARGET.read_text(encoding="utf-8")

    for group in ("financial_tiers", "sustainability_metrics", "reliability_and_governance"):
        assert f"finops.{group}" in html, f"the FinOps board never reads {group}"

    # Every dotted read of a known group must name a key the payload carries.
    for group in ("financial_tiers", "sustainability_metrics", "reliability_and_governance"):
        read = set(re.findall(rf"finops\.{group}\.([A-Za-z_][A-Za-z0-9_]*)", html))
        unknown = read - set(payload[group])
        assert not unknown, f"FinOps board reads unknown {group} keys: {sorted(unknown)}"

    # The three reconciliation tiers must stay distinguishable on screen.
    for tier in (
        "tier1_estimated_total_eur",
        "tier2_verified_usage_total_eur",
        "tier3_reconciled_billed_total_eur",
    ):
        assert tier in html, f"tier {tier} is not displayed"


def test_live_run_monitors_drift_from_observed_telemetry():
    """The drift verdict must report the observed run, not the form values."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "/api/workloads/${WORKLOAD_ID}/anomalies" in html
    # Elapsed time and spend come from the runtime snapshot.
    assert "elapsed_minutes: num(clk.current_time_minutes)" in html
    assert "current_cost_eur: num(wl.accrued_cost_eur)" in html
    # The verdict displayed is the server's own.
    assert "anomalyReport.health_status" in html
    assert "anomalyReport.anomalies.map" in html


def test_anomaly_ribbon_reads_telemetry_keys_the_detector_returns():
    from agentic_compute.anomaly_detector import detect_workload_anomalies

    report = detect_workload_anomalies(workload_id="wl-ui-contract")
    html = DEFAULT_TARGET.read_text(encoding="utf-8")

    read = set(re.findall(r"anomalyReport\.telemetry\.([A-Za-z_][A-Za-z0-9_]*)", html))
    assert read, "the ribbon displays no telemetry at all"
    unknown = read - set(report["telemetry"])
    assert not unknown, f"the ribbon reads unknown telemetry keys: {sorted(unknown)}"

    # Young-Daly exposure is the reason the ribbon exists for Spot workloads.
    assert "young_daly_optimal_checkpoint_minutes" in read
    assert "uncommitted_minutes_at_risk" in read


def test_what_if_sliders_send_the_operator_shock_parameters():
    """The engine always accepted shocks; the UI used to send only defaults."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "/api/plans/what-if" in html
    assert "forced_preemptions: Number(whatIfPreemptions)" in html
    assert "budget_shock_pct: Number(whatIfBudgetShock)" in html
    assert "deadline_compression_pct: Number(whatIfDeadlineCompression)" in html
    # Each shock needs its own control.
    for setter in (
        "setWhatIfPreemptions(Number(e.target.value))",
        "setWhatIfBudgetShock(Number(e.target.value))",
        "setWhatIfDeadlineCompression(Number(e.target.value))",
    ):
        assert setter in html, f"missing slider wiring: {setter}"
    # A live run must be distinguishable from the seeded default run.
    assert "operator scenario" in html
    assert "default scenario" in html


def test_what_if_panel_reads_every_scenario_the_simulator_returns():
    """Reporting only the Spot storm hides two of the three shocks."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    for scenario in (
        "spot_storm_scenario",
        "budget_shock_scenario",
        "deadline_compression_scenario",
    ):
        assert f"sr.{scenario}.survives" in html, f"{scenario} verdict is never displayed"


def test_pareto_scatter_is_fed_by_the_compared_plans():
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "function ParetoScatter" in html
    assert "plans={planComparison.plans}" in html
    # The four axes of the 4D analysis must all reach the plot.
    for axis in (
        "estimated_cost_eur",
        "total_time_to_result_minutes",
        "interruption_risk_score",
        "green_tier",
    ):
        assert axis in html, f"the scatter ignores the {axis} dimension"
    assert "is_pareto_optimal" in html


def test_execution_opens_an_impact_preview_before_submitting():
    """Actuation must be a confirmed step, not a single unguarded click."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "setPendingExecution(plan)" in html
    assert "pendingExecution &&" in html
    # The direct-submit binding on the card button must be gone.
    assert "onClick={() => handleExecutePlan(plan)}" not in html
    # Confirming is what actually submits.
    assert "handleExecutePlan(plan);" in html


def test_impact_preview_states_the_server_enforced_envelope():
    """The preview must show the mode and approval the server holds."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "Server-applied mode" in html
    assert "Approval recorded" in html
    assert "remaining_delegated_budget_eur" in html
    # And it must not claim to be the gate.
    assert "The server will re-check" in html


# ---------------------------------------------------------------------------
# Populated render
#
# `test_every_dashboard_tab_renders_without_error` renders each tab with the
# component's declared initial state, which is `null` for every data-driven
# panel. The Pareto scatter maths, the drift gauges, the reconciliation bars
# and the impact preview are therefore never executed by it: a TypeError on a
# nested key, or a NaN reaching an SVG coordinate, would pass unnoticed.
#
# These tests seed the state with responses from the real app and render again.
# ---------------------------------------------------------------------------

_WL = "workload-gui"

_PROFILE = {
    "workload_id": _WL,
    "name": "compute-simulation",
    "cpu_requested": 16,
    "memory_mb_requested": 32768,
    "deadline_minutes_from_start": 25,
    "budget_amount": 5.0,
    "is_parallelizable": True,
    "supports_checkpointing": True,
    "checkpoint_location": "gs://agentgrid-checkpoints/workload-gui",
}


def _live_seeds() -> dict:
    """Collect the payloads the panels consume, from the real endpoints.

    The run is carried end to end -- delegated, submitted to the in-process
    simulator, observed -- so the live-run card, the ledger table and the
    cluster status render from payloads the app actually produced, not from
    hand-written dictionaries that could drift from the API.
    """
    from fastapi.testclient import TestClient

    from agentic_compute.mcp_server import _runtime
    from compute_agent.app import app

    client = TestClient(app)
    try:
        # The server holds the mode. Delegation lets the plan run without an
        # approval, within the ceiling.
        client.post(
            f"/api/workloads/{_WL}/control",
            json={"control_mode": "delegation", "delegation_policy": {"max_budget_eur": 10.0}},
        ).raise_for_status()

        comparison = client.post(
            "/api/plans/compare",
            json={"workload_profile": _PROFILE, "cluster_total_cpu": 128},
        ).json()
        plans = comparison["plans"]
        assert plans, "the comparison returned no plan to render"
        recommended = next(p for p in plans if p["plan_id"] == comparison["recommended_plan_id"])

        what_if = client.post(
            "/api/plans/what-if",
            json={
                "workload_id": _WL,
                "plans": plans,
                "workload_profile": _PROFILE,
                # Deliberately not the defaults: the panel must show the operator's
                # own scenario, not the one baked into the comparison payload.
                "forced_preemptions": 5,
                "budget_shock_pct": -60.0,
                "deadline_compression_pct": -50.0,
            },
        ).json()

        # Telemetry chosen to trip every detector, so the drift panel renders
        # its populated branch rather than the "no anomaly" one.
        anomalies = client.post(
            f"/api/workloads/{_WL}/anomalies",
            json={
                "workload_profile": _PROFILE,
                "elapsed_minutes": 18.0,
                "current_cost_eur": 4.4,
                "progress_pct": 25.0,
                "minutes_since_last_checkpoint": 18.0,
            },
        ).json()
        assert anomalies["anomaly_count"] > 0, "expected the seeded run to be drifting"

        execution = client.post(
            "/api/execute-plan",
            json={
                "workload_profile": _PROFILE,
                "plan": recommended,
                "control_mode": "delegation",
                "delegation_policy": {"max_budget_eur": 10.0},
            },
        ).json()
        assert execution["status"] == "submitted", execution

        snapshot = client.get("/api/snapshot").json()
        # The live-run card only describes telemetry about *this* job.
        assert str(snapshot["snapshot"]["workload"]["id"]) == str(execution["job_id"]), snapshot

        history = client.get("/api/history").json()["records"]
        assert any(r["workload_id"] == _WL for r in history), "the run is missing from the ledger"

        # Read back after the submission, so the committed spend is non-zero.
        control = client.get(f"/api/workloads/{_WL}/control").json()

        diagnostics = client.get(
            "/api/diagnose?job_state=PENDING&state_reason=ReqNodeNotAvail"
        ).json()["diagnostics"]
        assert diagnostics, "the diagnosis returned nothing to render"
    finally:
        # The simulator is a process-wide singleton: leave it as it was found.
        _runtime.reset()

    # The live capacity search reads the Compute Engine API, which a test must
    # not depend on; the row is built through the model so it keeps its shape.
    from agentic_compute.models import CapacityCandidate

    candidates = [
        CapacityCandidate(
            machine_type="n2-standard-16",
            cpu_count=16,
            memory_gb=64.0,
            region="us-central1",
            zone="us-central1-a",
            quota_status="QUOTA_AVAILABLE",
            quota_limit=96,
            quota_usage=16,
            quota_project="agentgrid-test",
            obtainability_score=0.8,
            preemption_risk="LOW",
        ).model_dump()
    ]

    # An empty portfolio renders only the placeholder, so the board is fed a
    # populated aggregate built through the real aggregator.
    from agentic_compute.anomaly_detector import compute_portfolio_finops_analytics

    finops = compute_portfolio_finops_analytics(
        records=[
            {
                "workload_id": "wl-a",
                "control_mode": "delegation",
                "initial_estimated_cost_eur": 4.0,
                "final_calculated_cost_eur": 3.6,
                "reconciliation_status": "calculated_from_usage",
                "final_actual_duration_minutes": 22.0,
                "approved_plan": {
                    "cpu": 16,
                    "gpu": 0,
                    "machine_type": "n2-standard-16",
                    "region": "europe-west1",
                    "provisioning_model": "100% Spot",
                },
                "profile": {"deadline_minutes_from_start": 25.0},
            },
            {
                "workload_id": "wl-b",
                "control_mode": "validation",
                "initial_estimated_cost_eur": 8.4,
                "final_calculated_cost_eur": 7.3,
                "reconciliation_status": "reconciled_billed",
                "final_actual_duration_minutes": 48.0,
                "approved_plan": {
                    "cpu": 32,
                    "gpu": 0,
                    "machine_type": "n2-standard-32",
                    "region": "asia-east1",
                    "provisioning_model": "80% Spot / 20% Standard",
                },
                "profile": {"deadline_minutes_from_start": 40.0},
            },
        ]
    )
    assert finops["financial_tiers"]["spot_hedging_savings_eur"] > 0

    return {
        "planComparison": comparison,
        "whatIfResult": what_if,
        "anomalyReport": anomalies,
        "serverControl": control,
        "executionResult": execution,
        "snapshotPayload": snapshot,
        "historyRecords": history,
        "finops": finops,
        "capacityCandidates": candidates,
        "diagnosticsList": diagnostics,
        # State-driven, not tab-driven: the modal must render on every tab.
        "pendingExecution": plans[0],
    }


_POPULATED_MARKERS = {
    "run": [
        "Recommended plan",
        "Submitted to",
        "Live run",
        "CRITICAL_DRIFT",
        "Young-Daly",
        "Pareto frontier",
        "operator scenario",
    ],
    "ledger": [
        "Run ledger",
        _PROFILE["name"],
        # wl-a finished in 22 of 25 minutes, wl-b in 48 of 40.
        "1 of 2",
        "Cost reconciliation",
        "Saved by Spot vs 100% Standard",
        "Deadline compliance",
    ],
    "cluster": [
        "Live status",
        "agentgrid-test",
        "resource waiting",
        "Why is my job waiting?",
    ],
}


def test_populated_panels_render_without_error():
    """Every panel must survive being handed the data it was built for."""
    import render_check

    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    seeded = render_check.seed_state(source, _live_seeds())

    problems: list[str] = []
    for tab, markers in _POPULATED_MARKERS.items():
        outcome = render_check.render_tab(seeded, tab)
        problems.extend(f"[{tab}] render error: {e}" for e in outcome["errors"])
        text = outcome["text"]
        for marker in markers:
            if marker not in text:
                problems.append(f"[{tab}] expected {marker!r} in the rendered output")
        if "Impact preview" not in text:
            problems.append(f"[{tab}] the impact preview modal did not render")
        # A NaN in a coordinate or a figure is a rendered defect, not a warning.
        if "NaN" in text:
            problems.append(f"[{tab}] rendered a NaN")

    assert not problems, "\n".join(problems)


def test_run_view_states_the_saving_against_the_fastest_plan():
    """The headline saving must be the API's own numbers, not a marketing figure.

    The landing view claims "N% less than the fastest plan". N has to be
    (fastest - recommended) / fastest over the plans the comparison returned
    for this very job: anything else is an invented baseline.
    """
    import math

    import render_check
    from fastapi.testclient import TestClient

    from compute_agent.app import app

    # The dashboard's own default job: 2 vCPU, one hour of single-vCPU work,
    # one-hour deadline.
    profile = {
        "workload_id": "workload-value",
        "name": "nightly-risk-batch",
        "command": "sleep 30",
        "cpu_requested": 2,
        "memory_mb_requested": 4096,
        "estimated_duration_minutes": 60,
        "estimation_source": "user_specified",
        "deadline_minutes_from_start": 60,
        "budget_amount": 5.0,
        "is_parallelizable": True,
        "supports_checkpointing": True,
        "checkpoint_location": "gs://agentgrid-checkpoints/workload-value",
    }
    comparison = TestClient(app).post(
        "/api/plans/compare", json={"workload_profile": profile, "cluster_total_cpu": 128}
    ).json()
    plans = comparison["plans"]
    recommended = next(p for p in plans if p["plan_id"] == comparison["recommended_plan_id"])
    fastest = min(plans, key=lambda p: p["total_time_to_result_minutes"])
    assert fastest["plan_id"] != recommended["plan_id"], "no trade-off left to show for this job"
    saved = fastest["estimated_cost_eur"] - recommended["estimated_cost_eur"]
    # Math.round, not Python's banker's rounding.
    expected_pct = math.floor(saved / fastest["estimated_cost_eur"] * 100 + 0.5)

    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    seeded = render_check.seed_state(source, {"planComparison": comparison})
    outcome = render_check.render_tab(seeded, "run")

    assert not outcome["errors"], outcome["errors"]
    assert f"{expected_pct} % less" in outcome["text"], outcome["text"]
    assert "than the fastest plan" in outcome["text"]
    assert "saved on this run" in outcome["text"]


def _proxied_slurm_snapshot(monkeypatch) -> dict:
    """The production shape: the MCP server on Slurm, this service on the simulator.

    The payload comes from the real ``/api/snapshot`` route; only the network
    hop to the MCP server is replaced, by what its ``/snapshot`` route returns
    when it runs on Slurm.
    """
    import compute_agent.app as agent_app
    import compute_agent.auth as auth
    from fastapi.testclient import TestClient

    from agentic_compute.mcp_server import _runtime

    class _McpResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "runtime": "slurm",
                "snapshot": _runtime.snapshot().model_dump(),
                "slurm_verification": None,
            }

    monkeypatch.delenv("COMPUTE_RUNTIME", raising=False)
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.run.app/sse")
    monkeypatch.setattr(auth, "get_gcp_id_token", lambda audience: None)
    monkeypatch.setattr(agent_app.requests, "get", lambda *args, **kwargs: _McpResponse())

    payload = TestClient(agent_app.app).get("/api/snapshot").json()
    assert payload.get("runtime_mismatch") is True, payload
    return payload


def test_every_view_says_when_runs_do_not_reach_the_cluster_it_shows(monkeypatch):
    """The split-brain found in production must be on screen, not only in a payload.

    The MCP server showed a live Slurm cluster while this service submitted to
    its own simulator. Every tab must say so, name the cluster being shown,
    repeat the server's explanation, and label the runtime as the simulator.
    """
    import render_check

    snapshot = _proxied_slurm_snapshot(monkeypatch)
    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    seeded = render_check.seed_state(source, {"snapshotPayload": snapshot})

    markers = (
        "Runs from this page do not reach",
        "your Slurm cluster",
        snapshot["runtime_warning"],
        "COMPUTE_RUNTIME=slurm",
        "Simulator",
    )
    problems: list[str] = []
    for tab in ("run", "ledger", "cluster"):
        outcome = render_check.render_tab(seeded, tab)
        problems.extend(f"[{tab}] render error: {e}" for e in outcome["errors"])
        problems.extend(
            f"[{tab}] expected {marker!r} in the rendered output"
            for marker in markers
            if marker not in outcome["text"]
        )
    assert not problems, "\n".join(problems)


def test_the_cluster_warning_stays_hidden_when_runs_reach_the_runtime_shown(monkeypatch):
    """Control: a warning shown on every page is noise, and noise gets ignored."""
    import render_check
    from fastapi.testclient import TestClient

    from compute_agent.app import app

    monkeypatch.delenv("COMPUTE_RUNTIME", raising=False)
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    snapshot = TestClient(app).get("/api/snapshot").json()
    assert snapshot["snapshot_source"] == "local_runtime", snapshot
    assert "runtime_mismatch" not in snapshot, snapshot

    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    seeded = render_check.seed_state(source, {"snapshotPayload": snapshot})
    outcome = render_check.render_tab(seeded, "run")
    assert not outcome["errors"], outcome["errors"]
    assert "Runs from this page do not reach" not in outcome["text"]


def test_the_runtime_card_names_the_cluster_the_page_is_not_connected_to(monkeypatch):
    """"no cluster involved" sat next to the live figures of a real cluster.

    The operator read "Runs go to: Simulator, no cluster involved" as "you have
    no cluster", on a page showing his Slurm cluster's nodes. The card must say
    that this page is not connected to that cluster.
    """
    import render_check

    snapshot = _proxied_slurm_snapshot(monkeypatch)
    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    seeded = render_check.seed_state(source, {"snapshotPayload": snapshot})
    outcome = render_check.render_tab(seeded, "cluster")

    assert not outcome["errors"], outcome["errors"]
    assert "no cluster involved" not in outcome["text"]
    assert "not your Slurm cluster" in outcome["text"]


def _slurm_connected_snapshot_without_a_job(monkeypatch) -> dict:
    """What a Slurm-connected agent service serves before anything was submitted."""
    import requests
    from fastapi.testclient import TestClient

    import agentic_compute.mcp_server as mcp_server
    from agentic_compute.slurm_adapter import SlurmRuntime
    from compute_agent.app import app

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code, self._payload, self.text = status_code, payload, ""

        def json(self):
            return self._payload

    def fake_get(url, *args, **kwargs):
        if url.endswith("/nodes"):
            return _Resp(200, {"nodes": [{"name": "debug-0", "cpus": 2, "idle_cpus": 2}]})
        if url.endswith("/ping"):
            return _Resp(200, {})
        return _Resp(500, {"jobs": [], "errors": [{"description": "Invalid job id specified"}]})

    monkeypatch.delenv("MOCK_SLURM", raising=False)
    monkeypatch.setenv("COMPUTE_RUNTIME", "slurm")
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(mcp_server, "_runtime", SlurmRuntime(base_url="http://slurm.test/slurm/v0.0.41"))

    payload = TestClient(app).get("/api/snapshot").json()
    assert payload["snapshot_source"] == "local_runtime", payload
    assert payload["snapshot"]["workload"]["job_found"] is False, payload
    return payload


def test_a_placeholder_workload_is_neither_shown_nor_diagnosed_as_a_job(monkeypatch):
    """The Slurm adapter's placeholder (job "1", PENDING) is not a job waiting on the cluster.

    Production showed "Current job: 1 · pending · unverified" and a "Why is my
    job waiting?" diagnosis on a cluster with no job at all.
    """
    import render_check

    snapshot = _slurm_connected_snapshot_without_a_job(monkeypatch)
    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    seeded = render_check.seed_state(source, {"snapshotPayload": snapshot})
    outcome = render_check.render_tab(seeded, "cluster")

    assert not outcome["errors"], outcome["errors"]
    text = outcome["text"]
    assert "your cluster" in text
    assert "No job on the runtime yet." in text
    assert "pending · unverified" not in text
    # The re-diagnose button only exists for a job that waits or failed.
    assert "Check again" not in text
    assert "Runs from this page do not reach" not in text


def test_a_queued_run_is_never_shown_as_a_measured_zero():
    """A run that has only been submitted has no measured cost yet.

    The landing headline read "€0.00 from measured usage" right after the first
    submission: the history record claimed its cost was calculated from usage
    before anything had run, and the portfolio summed that zero as measured.
    """
    import render_check
    from fastapi.testclient import TestClient

    from agentic_compute.mcp_server import _runtime
    from compute_agent.app import app

    client = TestClient(app)
    try:
        client.post(
            f"/api/workloads/{_WL}/control",
            json={"control_mode": "delegation", "delegation_policy": {"max_budget_eur": 10.0}},
        ).raise_for_status()
        comparison = client.post(
            "/api/plans/compare", json={"workload_profile": _PROFILE, "cluster_total_cpu": 128}
        ).json()
        recommended = next(p for p in comparison["plans"] if p["plan_id"] == comparison["recommended_plan_id"])
        execution = client.post(
            "/api/execute-plan",
            json={
                "workload_profile": _PROFILE,
                "plan": recommended,
                "control_mode": "delegation",
                "delegation_policy": {"max_budget_eur": 10.0},
            },
        ).json()
        assert execution["status"] == "submitted", execution
        history = client.get("/api/history").json()["records"]
        finops = client.get("/api/portfolio/finops").json()
    finally:
        _runtime.reset()

    record = next(r for r in history if r["workload_id"] == _WL)
    assert record["reconciliation_status"] == "estimated", record
    assert finops["total_workloads"] == 1
    assert finops["financial_tiers"]["measured_workloads"] == 0

    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    seeded = render_check.seed_state(source, {"finops": finops, "historyRecords": history})
    run = render_check.render_tab(seeded, "run")
    ledger = render_check.render_tab(seeded, "ledger")
    assert not run["errors"] and not ledger["errors"], (run["errors"], ledger["errors"])

    assert "none measured yet" in run["text"], run["text"]
    assert "measured spend" not in run["text"]
    assert "no run measured yet" in ledger["text"]
    assert "not measured yet" in ledger["text"]
    assert "runs measured" not in ledger["text"]


def test_seeding_fails_loudly_when_a_state_hook_is_renamed():
    """Negative control: a silent no-op seed would revert the test to empty state."""
    import render_check

    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    with pytest.raises(RuntimeError, match="could not seed state"):
        render_check.seed_state(source, {"noSuchState": {"a": 1}})


def test_populated_render_detects_a_missing_nested_key():
    """Negative control: the populated render must actually catch a bad read."""
    import render_check

    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    # A FinOps payload missing `financial_tiers` is exactly the shape drift the
    # ledger would hit if the aggregator renamed a group.
    broken = render_check.seed_state(
        source,
        {"finops": {"total_workloads": 2, "sustainability_metrics": {}}},
    )
    outcome = render_check.render_tab(broken, "ledger")
    assert outcome["errors"], "a missing payload group was not reported"


