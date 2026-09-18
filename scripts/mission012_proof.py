#!/usr/bin/env python3
"""Mission 012 — production goal decomposition graph proof harness.

Bounded, deterministic, fail-closed harness that drives the REAL production
surface (``scripts/trajectory-pi-goals`` -> ``trajectory_os.graph.cli`` ->
``trajectory_os.graph.store`` / ``readiness``) and proves Issue #215's
acceptance criteria end to end:

* S1 graph creation from the checked-in declarative goal spec;
* S2 persistence + strict reconstruction with identical deterministic
  topological order, nodes, edges and graph identity;
* S3 dependency readiness: roots eligible, fan-in/chain nodes blocked with
  explicit reasons (and never eligible);
* S4 readiness transition when authoritative referenced mission evidence
  permits it (a real mission is created and run by the canonical
  orchestrator; the graph only references it);
* S5 machine-readable M013 scheduler-facing projection;
* S6 controlled malformed/cyclic rejection;
* S7 M008/M009/M010/M011 compatibility invariants.

Safety: all state lives in an isolated temp root (or ``--root``); no
network, no GPU, no Git history mutation. The harness never performs a Git
trust-boundary write.

Exit codes: 0 = all scenarios proven and report written;
            3 = proof blocked (one or more checks failed);
            2 = usage error.
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
CLI = REPO / "scripts" / "trajectory-pi-goals"
DOGFOOD_SPEC = REPO / "examples" / "goal_decomposition_dogfood.json"

CHECK_LOG: list[dict[str, Any]] = []


def _check(scenario: str, description: str, ok: bool) -> bool:
    CHECK_LOG.append({"scenario": scenario, "description": description,
                      "ok": bool(ok)})
    if not ok:
        print(f"  FAIL [{scenario}] {description}", file=sys.stderr)
    return ok


def _run_cli(args: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 - argv is harness-fixed
        [str(CLI), *args],
        capture_output=True, text=True, timeout=120, check=False, cwd=str(REPO),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _scenario_checks(scenario: str) -> list[dict[str, Any]]:
    return [check for check in CHECK_LOG if check["scenario"] == scenario]


def _scenario_result(scenario: str) -> dict[str, Any]:
    checks = _scenario_checks(scenario)
    return {
        "scenario": scenario,
        "status": "PASS" if checks and all(c["ok"] for c in checks)
        else "FAIL",
        "checks": checks,
    }


def _load_graph(root: str, goal_id: str) -> Any:
    from trajectory_os.graph import store
    return store.load_graph(root, goal_id)[0]


def _scenario_s1_create(root: pathlib.Path, work: pathlib.Path) -> None:
    code, out, err = _run_cli([
        "--root", str(root), "create", "--spec", str(DOGFOOD_SPEC),
        "--repo", str(work), "--head", "proof-baseline",
    ])
    _check("S1", "graph created from declarative spec", code == 0)
    _check("S1", "creation reports graph identity and order",
           "graph    :" in out and "order    :" in out)
    _check("S1", "persisted graph document exists",
           (root / "goals" / "g-m012-dogfood" / "graph.json").is_file())
    if code != 0:
        print(err, file=sys.stderr)


def _scenario_s2_reconstruct(root: pathlib.Path) -> None:
    from trajectory_os.graph import store
    graph, _ = store.load_graph(str(root), "g-m012-dogfood")
    again, _ = store.load_graph(str(root), "g-m012-dogfood")
    _check("S2", "reconstruction preserves graph identity",
           graph.graph_id == again.graph_id)
    _check("S2", "reconstruction preserves normalized nodes and edges",
           graph.to_dict() == again.to_dict())
    order = graph.topological_order()
    code, out, _ = _run_cli(["--root", str(root), "--json", "order",
                             "g-m012-dogfood"])
    rendered = json.loads(out)["topological_order"] if code == 0 else []
    _check("S2", "deterministic topological order is stable",
           list(order) == rendered)
    _check("S2", "all six nodes and six edges normalized",
           len(graph.nodes) == 6 and len(graph.edges) == 6)


def _scenario_s3_readiness(root: pathlib.Path) -> None:
    from trajectory_os.graph import readiness
    graph = _load_graph(str(root), "g-m012-dogfood")
    projection = readiness.project_with_store(str(root), graph)
    ready = set(projection.ready())
    blocked = set(projection.blocked())
    _check("S3", "independent roots are eligible",
           ready == {"n-spec", "n-model"})
    _check("S3", "fan-in and chained nodes are blocked",
           blocked == {"n-store", "n-readiness", "n-cli", "n-dogfood"})
    statuses = projection.by_id()
    _check("S3", "blocked nodes are never eligible",
           all(not statuses[n].eligible for n in blocked))
    code, out, _ = _run_cli(["--root", str(root), "why",
                             "g-m012-dogfood", "n-cli"])
    _check("S3", "blocked reason is explicit for the operator",
           code == 0 and "UPSTREAM_NOT_PROVEN" in out)


def _scenario_s4_evidence_transition(root: pathlib.Path,
                                     work: pathlib.Path) -> None:
    from trajectory_os.graph import readiness
    from trajectory_os.missions import model as mm
    from trajectory_os.missions import orchestrator
    from trajectory_os.missions.runner import SubrunResult

    class _OkRunner:
        def run(self, request: Any) -> SubrunResult:
            return SubrunResult(0, mm.CR_COMPLETED)

    orchestrator.create_mission(str(root), orchestrator.MissionConfig(
        mission_id="m-proof",
        objective="proof reference",
        phase_specs=orchestrator.default_phase_specs(
            {kind: ("true",) for kind in mm.CANONICAL_SEQUENCE}),
    ))
    spec = {
        "schema_version": 1,
        "goal_id": "g-proof-transition",
        "objective": "evidence transition",
        "nodes": [
            {"node_id": "n-base", "title": "base", "priority": 90,
             "depends_on": [],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "base proven"}],
             "mission_ref": {"mission_id": "m-proof", "required": True}},
            {"node_id": "n-next", "title": "next", "priority": 10,
             "depends_on": ["n-base"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "next proven"}]},
        ],
    }
    spec_path = work / "transition.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    code, _, err = _run_cli([
        "--root", str(root), "create", "--spec", str(spec_path),
        "--repo", str(work), "--head", "proof-baseline",
    ])
    _check("S4", "graph with a required live mission reference is accepted",
           code == 0)
    if code != 0:
        print(err, file=sys.stderr)
        return
    graph = _load_graph(str(root), "g-proof-transition")
    before = readiness.project_with_store(str(root), graph)
    _check("S4", "unproven mission node is IN_PROGRESS not COMPLETE",
           before.by_id()["n-base"].state == readiness.RS_IN_PROGRESS)
    _check("S4", "dependent node is blocked before proof",
           before.by_id()["n-next"].state == readiness.RS_BLOCKED)
    orchestrator.run_mission(str(root), "m-proof", _OkRunner())
    after = readiness.project_with_store(str(root), graph)
    _check("S4", "authoritative mission evidence completes the node",
           after.by_id()["n-base"].state == readiness.RS_COMPLETE)
    _check("S4", "dependent node becomes eligible after proof",
           after.by_id()["n-next"].state == readiness.RS_READY)
    raw = (root / "goals" / "g-proof-transition" / "graph.json").read_text(
        encoding="utf-8")
    _check("S4", "graph state never duplicates mission evidence",
           "mission_state" not in raw and "classification" not in raw)


def _scenario_s5_projection(root: pathlib.Path) -> None:
    code, out, _ = _run_cli(["--root", str(root), "--json", "project",
                             "g-m012-dogfood"])
    payload = json.loads(out) if code == 0 else {}
    required = {"goal_id", "graph_id", "topological_order", "nodes", "ready",
                "blocked", "counts"}
    _check("S5", "M013 projection is machine-readable",
           code == 0 and required.issubset(payload))
    nodes = payload.get("nodes", [])
    _check("S5", "projection carries acceptance criteria, resources and budgets",
           bool(nodes) and all(
               {"acceptance_criteria", "resources", "budgets"}.issubset(n)
               for n in nodes))


def _scenario_s6_refusal(root: pathlib.Path, work: pathlib.Path) -> None:
    cyclic = {
        "schema_version": 1, "goal_id": "g-proof-cycle", "objective": "cycle",
        "nodes": [
            {"node_id": "n-a", "title": "A", "priority": 1,
             "depends_on": ["n-b"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}]},
            {"node_id": "n-b", "title": "B", "priority": 2,
             "depends_on": ["n-a"],
             "acceptance_criteria": [{"criterion_id": "ac-1",
                                      "statement": "s"}]},
        ],
    }
    spec_path = work / "cycle.json"
    spec_path.write_text(json.dumps(cyclic), encoding="utf-8")
    code, _, err = _run_cli([
        "--root", str(root), "create", "--spec", str(spec_path)])
    _check("S6", "cyclic graph is rejected fail-closed",
           code == 3 and "DEPENDENCY_CYCLE" in err)
    tampered = root / "goals" / "g-m012-dogfood" / "graph.json"
    raw = json.loads(tampered.read_text(encoding="utf-8"))
    raw["nodes"][0]["priority"] = 0
    tampered.write_text(json.dumps(raw), encoding="utf-8")
    code, _, err = _run_cli(["--root", str(root), "validate",
                             "g-m012-dogfood"])
    _check("S6", "tampered persisted graph fails closed on identity",
           code == 3 and "GRAPH_IDENTITY_MISMATCH" in err)


def _scenario_s7_compatibility() -> None:
    from trajectory_os.graph import identity as graph_identity
    from trajectory_os.missions import identity, runner, semantic
    from trajectory_os.missions import model as mm
    _check("S7", "M008 unattested success is never COMPLETED",
           runner.classify_subrun(
               0, semantic_status=semantic.STATUS_SUCCESS,
               attestation=None).classification == mm.CR_UNPROVEN)
    _check("S7", "M010 patch identity domains stay independent",
           identity.WRAPPER_SNAPSHOT_DOMAIN
           != identity.MISSION_WORKTREE_DOMAIN)
    _check("S7", "graph/spec domains never overload M010 domains",
           graph_identity.GRAPH_DOMAIN not in identity.DOMAIN_IDS
           and graph_identity.SPEC_DOMAIN not in identity.DOMAIN_IDS)


def produce_report(root: pathlib.Path, work: pathlib.Path,
                   out_dir: pathlib.Path) -> dict[str, Any]:
    _scenario_s1_create(root, work)
    _scenario_s2_reconstruct(root)
    _scenario_s3_readiness(root)
    _scenario_s4_evidence_transition(root, work)
    _scenario_s5_projection(root)
    _scenario_s6_refusal(root, work)
    _scenario_s7_compatibility()
    scenarios = [_scenario_result(name) for name in (
        "S1", "S2", "S3", "S4", "S5", "S6", "S7")]
    all_ok = all(s["status"] == "PASS" for s in scenarios)
    report = {
        "schema_version": 1,
        "mission": "M012",
        "goal_decomposition_graph": True,
        "generated_at": datetime.datetime.now(datetime.UTC)
        .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "root": str(root),
        "dogfood_spec": str(DOGFOOD_SPEC),
        "status": "COMPLETE" if all_ok else "BLOCKED",
        "scenarios": scenarios,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "mission-012-goal-decomposition-graph-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=None,
                        help="isolated root (default: fresh temp)")
    parser.add_argument("--out", default=".",
                        help="report output directory")
    args = parser.parse_args(argv)

    tmp_root = None
    if args.root:
        root = pathlib.Path(args.root)
        work = root
    else:
        base = pathlib.Path(tempfile.mkdtemp(prefix="mission012-proof-"))
        root = base / "root"
        work = base / "work"
        tmp_root = base
    try:
        root.mkdir(parents=True, exist_ok=True)
        work.mkdir(parents=True, exist_ok=True)
        report = produce_report(root, work, pathlib.Path(args.out))
        print(json.dumps({
            "status": report["status"],
            "scenarios": {s["scenario"]: s["status"]
                          for s in report["scenarios"]},
        }, indent=2))
        return 0 if report["status"] == "COMPLETE" else 3
    finally:
        if tmp_root is not None:
            shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
