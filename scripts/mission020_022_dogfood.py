#!/usr/bin/env python3
"""Program block B (M020-M022) — deterministic portfolio/daemon dogfood.

Drives the REAL production code paths (portfolio coordinator -> existing goal
runner -> mission orchestrator + scheduler; persistent daemon; provider-neutral
agent backends) with a deterministic exact-attestation runner substituted for
the model subprocess. It proves Issue #225's acceptance criteria end to end:

* S1 two isolated strategic goals coexist without state/evidence leakage;
* S2 the portfolio arbitrates resources deterministically and cross-goal
  dependencies block while a prerequisite is incomplete;
* S3 the daemon persists authoritative state, reconstructs exact state after
  interruption, and resumes without replaying completed work;
* S4 a daemon safe stop blocks new work and resume completes;
* S5 backend capability discovery, bounded retry, cancellation, lifecycle
  normalization and exact provider/model route identity are machine-readable
  and fail closed;
* S6 Pi remains the proven fallback and no scenario performs a Git
  trust-boundary write.

All state lives in an isolated temp root (or ``--root``); no network, no GPU,
no Git history mutation.

Exit codes: 0 = all scenarios proven and report written;
            3 = proof blocked (one or more checks failed);
            2 = usage error.
"""

# ruff: noqa: E402
# (the repository src/ layout is added to sys.path below)

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import sys
import tempfile
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from trajectory_os.agents import capabilities, lifecycle, retry, route
from trajectory_os.agents import model as agent_model
from trajectory_os.daemon import engine as daemon_engine
from trajectory_os.daemon import model as daemon_model
from trajectory_os.goals import launch
from trajectory_os.graph import store as graph_store
from trajectory_os.graph.proof import engine as proof_engine
from trajectory_os.missions import model as mission_model
from trajectory_os.missions import semantic
from trajectory_os.missions.runner import SubrunResult
from trajectory_os.portfolio import engine as portfolio_engine
from trajectory_os.portfolio import model as portfolio_model

GOAL_A = "g-dogfood-a"
GOAL_B = "g-dogfood-b"
MISSION_A = "m-dogfood-a"
MISSION_B = "m-dogfood-b"

CHECK_LOG: list[dict[str, Any]] = []


def _check(scenario: str, description: str, ok: bool) -> bool:
    CHECK_LOG.append({"scenario": scenario, "description": description,
                      "ok": bool(ok)})
    return bool(ok)


class AttestedRunner:
    """Deterministic exact-attestation success for every phase."""

    def run(self, request: Any) -> SubrunResult:
        return SubrunResult(
            0, mission_model.CR_COMPLETED,
            semantic_status=semantic.STATUS_SUCCESS,
            attestation=semantic.ATTESTATION_VERIFIED)


def _spec(goal_id: str, mission_id: str, priority: int) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "goal_id": goal_id,
        "objective": f"Dogfood portfolio isolation for {goal_id}.",
        "nodes": [{
            "node_id": f"n-{goal_id}",
            "title": f"Mission for {goal_id}",
            "priority": priority,
            "depends_on": [],
            "acceptance_criteria": [{
                "criterion_id": f"ac-{goal_id}",
                "statement": f"{goal_id} work is proven"}],
            "mission_ref": {"mission_id": mission_id, "required": True},
            "resources": {"cpu_slots": 1},
            "budgets": {"repair_budget": 0, "max_attempts": 1},
        }],
    }


def _setup(root: str, goal_id: str, mission_id: str, priority: int) -> None:
    defaults = launch.MissionLaunchDefaults(
        repo_root=None, baseline_revision="base", repair_budget=0)
    spec = _spec(goal_id, mission_id, priority)
    launch.provision_from_spec(root, spec, defaults)
    graph_store.create_graph(root, spec, repo_root=defaults.repo_root,
                             baseline_revision=defaults.baseline_revision)


def _scenario_portfolio(root: str) -> dict[str, Any]:
    _setup(root, GOAL_A, MISSION_A, priority=90)
    _setup(root, GOAL_B, MISSION_B, priority=10)
    policy = portfolio_model.PortfolioPolicy(max_active_goals=1,
                                             global_concurrency=2)

    # Cross-goal dependency: while A is incomplete, a dependent goal is
    # excluded with the stable dependency-pending reason.
    _setup(root, "g-dogfood-dep", "m-dogfood-dep", priority=50)
    deps = portfolio_model.PortfolioDependencies.normalize(
        {"g-dogfood-dep": [GOAL_A]})
    dependency_plan = portfolio_engine.plan_portfolio(
        root, [GOAL_A, "g-dogfood-dep"], policy, deps,
        created_at="2026-01-01T00:00:00Z")
    entry = dependency_plan.entry("g-dogfood-dep")
    _check("S2", "cross-goal dependency blocks a dependent goal",
           entry is not None
           and entry.reason == portfolio_model.R_DEPENDENCY_PENDING)

    progress: list[dict[str, Any]] = []
    for index in range(2):
        result = portfolio_engine.run_cycle(
            root, [GOAL_A, GOAL_B], policy, runner_factory=AttestedRunner,
            session_subruns=32,
            created_at=f"2026-01-01T00:00:0{index}Z")
        progress.append({
            "cycle": index,
            "selected": [e.goal_id for e in result.decision.selected()],
            "excluded": {e.goal_id: e.reason for e in result.decision.entries
                         if e.outcome == portfolio_model.O_EXCLUDED},
            "steps": [s.to_dict() for s in result.steps],
        })
    proof_a = proof_engine.build_proof(root, GOAL_A)
    proof_b = proof_engine.build_proof(root, GOAL_B)
    state, latest = portfolio_engine.reconstruct(root)
    _check("S1", "goal A proof is independent and complete", proof_a.complete)
    _check("S1", "goal B proof is independent and complete", proof_b.complete)
    _check("S1", "goal proofs have distinct identities",
           proof_a.proof_id != proof_b.proof_id)
    _check("S1", "goal A evidences only its own node",
           {n.node_id for n in proof_a.nodes} == {f"n-{GOAL_A}"})
    _check("S1", "goal B evidences only its own node",
           {n.node_id for n in proof_b.nodes} == {f"n-{GOAL_B}"})
    _check("S2", "portfolio arbitrates one goal per concurrency slot",
           progress[0]["selected"] == [GOAL_A]
           and progress[0]["excluded"].get(GOAL_B)
           == portfolio_model.R_CONCURRENCY_LIMIT)
    _check("S2", "portfolio reconstruction matches the persisted decisions",
           latest is not None and latest.decision_id == state.last_decision_id)
    return {"progress": progress,
            "proofs": {"a": proof_a.proof_id, "b": proof_b.proof_id}}


def _scenario_daemon(root: str) -> dict[str, Any]:
    _setup(root, GOAL_A, MISSION_A, priority=90)
    _setup(root, GOAL_B, MISSION_B, priority=10)
    config = daemon_model.DaemonConfig(
        max_cycles=1, session_subruns=32,
        policy=portfolio_model.PortfolioPolicy(max_active_goals=1,
                                               global_concurrency=2))
    first = daemon_engine.run_daemon(
        root, [GOAL_A, GOAL_B], config=config,
        runner_factory=AttestedRunner, clock=lambda: "2026-01-01T00:00:00Z")
    _check("S3", "first bounded daemon session stops at the cycle bound",
           first.status == daemon_model.DS_CYCLE_BOUND)
    # Interruption: a fresh process reconstructs the exact durable state.
    state, cycles = daemon_engine.reconstruct(root)
    _check("S3", "daemon reconstruction matches persisted cycle count",
           state.cycles_executed == first.state.cycles_executed
           and len(cycles) == state.cycles_executed)
    _check("S3", "daemon reconstruction matches the persisted decision ids",
           state.decision_ids == first.state.decision_ids)
    resumed = daemon_engine.run_daemon(
        root, [GOAL_A, GOAL_B], config=config,
        runner_factory=AttestedRunner, clock=lambda: "2026-01-01T00:00:01Z")
    _check("S3", "daemon resume completes all goals",
           resumed.status == daemon_model.DS_COMPLETE
           and resumed.reason == daemon_engine.DR_ALL_GOALS_COMPLETE)
    _check("S3", "daemon resume does not replay the prior cycle",
           resumed.state.cycles_executed == 2)

    # Safe stop then resume.
    _setup(root, "g-dogfood-stop", "m-dogfood-stop", priority=50)
    daemon_engine.request_stop(root, reason="dogfood safe stop",
                               requested_at="2026-01-01T00:01:00Z")
    stopped = daemon_engine.run_daemon(
        root, ["g-dogfood-stop"], config=config, runner_factory=AttestedRunner,
        clock=lambda: "2026-01-01T00:01:00Z")
    _check("S4", "daemon safe stop blocks new work",
           stopped.status == daemon_model.DS_STOPPED
           and stopped.session_cycles == 0)
    resumed_stop = daemon_engine.resume_daemon(
        root, ["g-dogfood-stop"], config=config,
        runner_factory=AttestedRunner, clock=lambda: "2026-01-01T00:01:01Z")
    _check("S4", "daemon resume after stop completes the goal",
           resumed_stop.status == daemon_model.DS_COMPLETE
           and proof_engine.build_proof(root, "g-dogfood-stop").complete)
    return {"first": first.to_dict(), "resumed": resumed.to_dict(),
            "stopped": stopped.to_dict()}


def _scenario_backends(root: str) -> dict[str, Any]:
    class _FakeBackend:
        name = agent_model.BACKEND_PI

        def __init__(self, results: list[agent_model.AgentResult]) -> None:
            self._results = results
            self.calls = 0

        def probe(self) -> agent_model.BackendProbe:
            return agent_model.BackendProbe(
                backend=self.name, available=True, reason=agent_model.R_OK,
                transport=agent_model.TRANSPORT_SUBPROCESS)

        def run(self, request: agent_model.AgentRequest, *,
                cancel: object | None = None) -> agent_model.AgentResult:
            self.calls += 1
            return self._results[min(self.calls - 1, len(self._results) - 1)]

    def _result(status: str, reason: str,
                source: str = agent_model.CS_NONE,
                reliable: bool = False) -> agent_model.AgentResult:
        return agent_model.AgentResult.build(
            backend=agent_model.BACKEND_PI, status=status, reason=reason,
            events=[agent_model.AgentEvent.build(
                sequence=0, kind=agent_model.LK_STARTED, method="dogfood")],
            completion=agent_model.CompletionEvidence.build(
                source=source, reliable=reliable, detail=reason))

    report = capabilities.discover(_FakeBackend([]))
    _check("S5", "capability discovery is deterministic and machine-readable",
           report.report_id == report.compute_report_id()
           and capabilities.CAP_CANCELLATION in report.capabilities)
    backend = _FakeBackend([
        _result(agent_model.RS_TIMEOUT, agent_model.R_TIMEOUT),
        _result(agent_model.RS_COMPLETED, agent_model.R_OK,
                agent_model.CS_EXIT_CODE_MARKER, True)])
    outcome = retry.run_with_retries(
        backend, agent_model.AgentRequest(task="x", workspace=str(root)),
        retry.RetryPolicy(max_attempts=2))
    _check("S5", "bounded retry recovers without rewriting provenance",
           outcome.result.status == agent_model.RS_COMPLETED
           and [a.status for a in outcome.attempts] == [
               agent_model.RS_TIMEOUT, agent_model.RS_COMPLETED])
    lifecycle_summary = lifecycle.LifecycleSummary.of(outcome.result)
    _check("S5", "lifecycle normalization is machine-readable",
           lifecycle_summary.has(lifecycle.FLAG_STARTED)
           and lifecycle_summary.summary_id
           == lifecycle_summary.compute_summary_id())
    exact = route.resolve_route(
        agent_model.BACKEND_DEEPSEEK_HARNESS,
        agent_model.AgentRequest(task="x", workspace=str(root),
                                 model="deepseek-reasoner"),
        transport=agent_model.TRANSPORT_RUNTIME)
    default = route.default_route(agent_model.BACKEND_DEEPSEEK_HARNESS)
    _check("S5", "exact provider/model route identity is distinguished",
           exact.route_id != default.route_id)
    pi_route = route.default_route(agent_model.BACKEND_PI)
    _check("S6", "Pi remains the documented proven fallback",
           pi_route.provider == "ollama")
    return {"capabilities": report.to_dict(),
            "retry": outcome.to_dict(),
            "route": exact.to_dict()}


GIT_WRITE_VERBS = (
    "commit", "push", "merge", "reset", "restore", "clean", "stash",
    "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick",
)


def _scenario_no_git_write() -> dict[str, Any]:
    modules = [
        portfolio_engine, daemon_engine, capabilities, retry, route,
        lifecycle,
    ]
    offenders: list[str] = []
    for module in modules:
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for verb in GIT_WRITE_VERBS:
            if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source:
                offenders.append(f"{module.__name__}:{verb}")
    _check("S6", "no module performs a Git trust-boundary write",
           not offenders)
    return {"offenders": offenders, "checked": [m.__name__ for m in modules]}


def _scenario_result(name: str) -> dict[str, Any]:
    checks = [c for c in CHECK_LOG if c["scenario"] == name]
    return {"scenario": name, "checks": len(checks),
            "passed": sum(1 for c in checks if c["ok"]),
            "status": "PASS" if all(c["ok"] for c in checks) else "FAIL"}


def _scenarios(root: str) -> dict[str, bool]:
    _scenario_portfolio(str(pathlib.Path(root) / "portfolio"))
    _scenario_daemon(str(pathlib.Path(root) / "daemon"))
    _scenario_backends(root)
    _scenario_no_git_write()
    scenarios = sorted({c["scenario"] for c in CHECK_LOG})
    return {s: _scenario_result(s)["status"] == "PASS" for s in scenarios}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mission020_022_dogfood",
        description="Deterministic M020-M022 portfolio/daemon/backend dogfood.")
    parser.add_argument("--root", default=None,
                        help="isolated state root (default: a temp dir)")
    parser.add_argument("--out", default=None, help="report output path")
    parser.add_argument("--keep", action="store_true", default=False)
    args = parser.parse_args(argv)

    temporary = args.root is None
    root = args.root or tempfile.mkdtemp(prefix="trajectory-m020-m022-")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    try:
        results = _scenarios(root)
        scenarios = sorted({c["scenario"] for c in CHECK_LOG})
        report = {
            "mission": "M020-M022",
            "issue": 225,
            "generated_at": datetime.datetime.now(datetime.UTC)
            .replace(microsecond=0, tzinfo=None).isoformat() + "Z",
            "root": root,
            "scenarios": [_scenario_result(s) for s in scenarios],
            "checks": CHECK_LOG,
            "status": ("PASS" if all(results.values())
                       and all(c["ok"] for c in CHECK_LOG) else "FAIL"),
        }
        out = pathlib.Path(args.out or pathlib.Path(root) / "m020-m022.json")
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"M020-M022 dogfood {report['status']}: {out}")
        for scenario in scenarios:
            print(f"  {scenario}: {_scenario_result(scenario)['status']}")
        return 0 if report["status"] == "PASS" else 3
    finally:
        if temporary and not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
