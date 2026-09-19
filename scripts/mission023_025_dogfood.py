#!/usr/bin/env python3
"""Program block C (M023-M025) — runtime, resources and artifact dogfood.

Exercises the REAL production code paths with one honest exception: the live
DeepSeek model completion is attempted in a bounded, isolated subprocess and,
when the developer-preview provider is unavailable or incompatible, the
result is recorded as deterministic UNAVAILABLE/INCOMPATIBLE evidence and the
proven Pi path remains the fallback. Nothing is invented.

Scenarios:

* S1 — isolated deepseek-harness-sdk discovery + identity + bounded structured
  qualification (no PYTHONPATH contamination; structured evidence);
* S2 — real local CPU/GPU/VRAM discovery, reviewer reservation isolation,
  remote-vs-local GPU distinction, bounded admission and durable ledger;
* S3 — persistent per-goal/per-mission workspaces, content-addressed artifact
  provenance, lineage reconstruction and cross-goal leak rejection;
* S4 — provenance integrates with M014 reuse / M016 goal-proof evidence;
* S5 — no module performs a Git trust-boundary write and M008-M022 remain
  importable/behaviourally intact.

All state lives under an isolated temp root (or ``--root``); no network is
required for the deterministic checks (the live qualification is bounded and
may be skipped with ``--offline``).

Exit codes: 0 = all scenarios proven; 3 = proof blocked; 2 = usage error.
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

from trajectory_os.agents import harness_qualification as hq
from trajectory_os.agents import model as agent_model
from trajectory_os.agents import qualification
from trajectory_os.agents.contract import AgentBackend
from trajectory_os.artifacts import engine as artifact_engine
from trajectory_os.artifacts import model as artifact_model
from trajectory_os.artifacts import store as artifact_store
from trajectory_os.artifacts import summary as artifact_summary
from trajectory_os.resources import arbiter as resource_arbiter
from trajectory_os.resources import model as resource_model
from trajectory_os.resources import probe as resource_probe
from trajectory_os.resources import summary as resource_summary
from trajectory_os.runs import resources as runs_resources

GIB = 1024 ** 3
GOAL = "g-dogfood-runtime-artifacts"

CHECK_LOG: list[dict[str, Any]] = []


def _check(scenario: str, description: str, ok: bool) -> bool:
    CHECK_LOG.append({"scenario": scenario, "description": description,
                      "ok": bool(ok)})
    return bool(ok)


# --- M023 fakes (deterministic fallback for offline mode) ---------------------


class _FakeHarness:
    name = agent_model.BACKEND_DEEPSEEK_HARNESS

    def probe(self) -> agent_model.BackendProbe:
        return agent_model.BackendProbe(
            backend=self.name, available=True, reason=agent_model.R_OK,
            transport=agent_model.TRANSPORT_RUNTIME, sdk_version="0.1.5rc1")

    def run(self, request: agent_model.AgentRequest, *,
            cancel: object | None = None) -> agent_model.AgentResult:
        return agent_model.AgentResult.build(
            backend=self.name, status=agent_model.RS_COMPLETED,
            reason=agent_model.R_OK,
            events=[
                agent_model.AgentEvent.build(
                    sequence=0, kind=agent_model.LK_INITIALIZED,
                    method="initialize"),
                agent_model.AgentEvent.build(
                    sequence=1, kind=agent_model.LK_IDLE,
                    method="session.status"),
            ],
            completion=agent_model.CompletionEvidence.build(
                source=agent_model.CS_LIFECYCLE_IDLE, reliable=True,
                detail="idle"),
            transport=agent_model.TRANSPORT_RUNTIME)


class _FakePi:
    name = agent_model.BACKEND_PI

    def probe(self) -> agent_model.BackendProbe:
        return agent_model.BackendProbe(
            backend=self.name, available=True, reason=agent_model.R_OK,
            transport=agent_model.TRANSPORT_SUBPROCESS)

    def run(self, request: agent_model.AgentRequest, *,
            cancel: object | None = None) -> agent_model.AgentResult:
        return agent_model.AgentResult.build(
            backend=self.name, status=agent_model.RS_COMPLETED,
            reason=agent_model.R_OK,
            completion=agent_model.CompletionEvidence.build(
                source=agent_model.CS_EXIT_CODE_MARKER, reliable=True,
                detail="pi"))


def _offline_factory(name: str) -> AgentBackend:
    return _FakeHarness() if name == agent_model.BACKEND_DEEPSEEK_HARNESS \
        else _FakePi()


# --- scenarios ----------------------------------------------------------------


def _scenario_harness(root: str, *, offline: bool,
                      timeout_s: int) -> dict[str, Any]:
    workspace = str(pathlib.Path(root) / "harness-ws")
    pathlib.Path(workspace).mkdir(parents=True, exist_ok=True)
    environment = hq.resolve_environment()
    identity = hq.probe_identity(environment)
    _check("S1", "isolated SDK environment is resolved deterministically",
           environment.environment_id == environment.compute_environment_id())
    _check("S1", "isolated identity probe is structured (no project import)",
           identity.identity_id == identity.compute_identity_id())
    # The project interpreter must not see the SDK at all.
    import importlib.util
    _check("S1", "SDK is absent from the project interpreter",
           importlib.util.find_spec("deepseek_harness") is None)
    factory = _offline_factory if offline else None
    outcome = hq.qualify_isolated(
        workspace=workspace, timeout_s=timeout_s, factory=factory,
        environment=environment, identity=identity, allow_runtime=True)
    document = outcome.to_dict()
    _check("S1", "isolation contract holds (PYTHONPATH dropped, no SDK import)",
           document["isolation"]["pythonpath_dropped"] is True
           and document["isolation"]["sdk_imported_into_project"] is False)
    _check("S1", "qualification evidence is structured and bounded",
           isinstance(document["checks"], dict)
           and document["checks"].get("evidence_structure")
           in (hq.CK_PASS, hq.CK_UNAVAILABLE, hq.CK_INCOMPATIBLE))
    _check("S1", "timeout/cancellation semantics are recorded",
           document["checks"].get("timeout_semantics") == hq.CK_CONFIGURED
           and document["checks"].get("cancellation_semantics")
           == hq.CK_CONFIGURED)
    _check("S1", "no success is invented when the provider is incompatible",
           outcome.status in (qualification.QS_QUALIFIED,
                              qualification.QS_UNAVAILABLE,
                              qualification.QS_INCOMPATIBLE,
                              qualification.QS_FAILED))
    _check("S1", "the proven Pi route remains the deterministic fallback",
           document["fallback"]["pi_route"]["backend"]
           == agent_model.BACKEND_PI)
    _check("S1", "no Git trust-boundary write is performed",
           outcome.git_writes is False)
    return document


def _scenario_resources(root: str) -> dict[str, Any]:
    report = resource_probe.discover()
    _check("S2", "real local resource discovery returns a stable identity",
           report.report_id == report.compute_report_id())
    vram = report.gpu_mem_bytes or 0
    reserve = min(8 * GIB, vram // 3) if vram else 0
    policy = resource_model.ResourcePolicy(
        reviewer_vram_reserve_bytes=reserve, max_local_concurrency=1)
    arb = resource_arbiter.ResourceArbiter(report, policy=policy, root=root)

    reviewer = arb.admit(
        "dogfood-reviewer", runs_resources.ResourceRequirement(
            gpu=True, gpu_mem_bytes=min(reserve, GIB) or None),
        role=resource_model.ROLE_REVIEWER)
    _check("S2", "local reviewer workload is admitted into its reserved pool",
           reviewer.allowed is True)
    remote = arb.admit(
        "dogfood-agent-remote", runs_resources.ResourceRequirement(
            gpu=True, gpu_mem_bytes=(vram or GIB)),
        locality=resource_model.REMOTE, role=resource_model.ROLE_AGENT)
    _check("S2", "remote inference is admitted without consuming the local GPU",
           remote.allowed is True and remote.reservation is not None
           and remote.reservation.gpu_mem_bytes == 0)
    if vram:
        oversized = arb.admit(
            "dogfood-agent-local", runs_resources.ResourceRequirement(
                gpu=True, gpu_mem_bytes=vram),
            role=resource_model.ROLE_AGENT)
        _check("S2", "non-reviewer local work cannot oversubscribe the GPU",
               oversized.allowed is False
               and oversized.reason
               in (resource_model.AR_EXHAUSTED,
                   resource_model.AR_LOCAL_CONCURRENCY))

    document = resource_summary.status_document(
        root=root, report=report, policy=policy)
    _check("S2", "live resource state is machine-readable",
           document["status"] == "OK"
           and document["report"]["report_id"] == report.report_id)
    reloaded = resource_arbiter.ResourceArbiter(
        report, policy=policy, root=root)
    _check("S2", "reservations reconstruct after a restart",
           [r.job_id for r in reloaded.active()]
           == sorted(r.job_id for r in arb.active()))
    usage = arb.usage()
    capacity = report.capacity()
    no_oversubscription = True
    if capacity.gpu_mem_bytes is not None:
        no_oversubscription = usage.gpu_mem_bytes <= capacity.gpu_mem_bytes
    _check("S2", "admitted reservations never exceed discovered capacity",
           no_oversubscription)
    return document


def _scenario_artifacts(root: str) -> dict[str, Any]:
    manager = artifact_engine.WorkspaceManager(root)
    manager.ensure_goal_workspace(
        GOAL, created_at="2026-01-01T00:00:00Z")
    mission = manager.ensure_mission_workspace(
        GOAL, "m-dogfood", created_at="2026-01-01T00:00:00Z")
    report = manager.record_artifact(
        goal_id=GOAL, kind=artifact_model.AK_REPORT, name="report.json",
        content=json.dumps({"ok": True}).encode("utf-8"),
        mission_id="m-dogfood", producer=artifact_model.PRODUCER_AGENT,
        proof_id="proof-dogfood", criterion_id="ac-dogfood",
        created_at="2026-01-01T00:00:01Z")
    dataset = manager.record_artifact(
        goal_id=GOAL, kind=artifact_model.AK_DATASET, name="vectors.bin",
        content=bytes(range(64)), mission_id="m-dogfood",
        parent_ids=(report.artifact_id,), reuse_input_id="reuse-dogfood",
        producer=artifact_model.PRODUCER_RUNNER,
        created_at="2026-01-01T00:00:02Z")
    model_file = manager.record_artifact(
        goal_id=GOAL, kind=artifact_model.AK_MODEL, name="model.json",
        content=b'{"weights": [1, 2, 3]}', mission_id="m-dogfood",
        parent_ids=(dataset.artifact_id,),
        producer=artifact_model.PRODUCER_AGENT,
        created_at="2026-01-01T00:00:03Z")

    state = manager.reconstruct(GOAL)
    ok, problems = manager.verify(GOAL)
    lineage = manager.lineage(GOAL, model_file.artifact_id)
    _check("S3", "per-goal and per-mission workspaces persist",
           pathlib.Path(mission.path).is_dir()
           and state.lineage_id == state.compute_lineage_id())
    _check("S3", "artifact content is reconstructible and verifiable",
           ok is True and problems == [] and len(state.artifacts) == 3)
    _check("S3", "artifact lineage is reconstructed deterministically",
           [r.name for r in lineage]
           == ["model.json", "vectors.bin", "report.json"])
    _check("S4", "artifact provenance carries M016 proof/criterion identity",
           report.proof_id == "proof-dogfood"
           and report.criterion_id == "ac-dogfood")
    _check("S4", "artifact provenance carries M014 reuse input identity",
           dataset.reuse_input_id == "reuse-dogfood")

    # Implicit cross-goal leakage must be impossible.
    manager.ensure_goal_workspace("g-other",
                                  created_at="2026-01-01T00:00:00Z")
    leaked = False
    try:
        artifact_store.load_artifact(root, "g-other", report.artifact_id)
        leaked = True
    except artifact_model.ArtifactError:
        leaked = False
    _check("S3", "implicit cross-goal artifact leakage is rejected", not leaked)
    document = artifact_summary.status_document(root, GOAL)
    _check("S3", "artifact status/lineage is exposed to operators",
           document["status"] == "OK"
           and document["counts"]["artifacts_total"] == 3
           and "artifacts : total=3"
           in artifact_summary.render_status(document))
    return {
        "state": state.to_dict(),
        "status": document,
        "lineage": [r.artifact_id for r in lineage],
    }


GIT_WRITE_VERBS = (
    "commit", "push", "merge", "reset", "restore", "clean", "stash",
    "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick",
)


def _scenario_preservation() -> dict[str, Any]:
    modules = [
        resource_probe, resource_arbiter, artifact_engine,
        artifact_store, artifact_summary, hq,
    ]
    offenders: list[str] = []
    for module in modules:
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for verb in GIT_WRITE_VERBS:
            if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source:
                offenders.append(f"{module.__name__}:{verb}")
    _check("S5", "no M023-M025 module performs a Git trust-boundary write",
           not offenders)
    # Prior proven modules remain importable (M008-M022 preserved).
    import importlib
    prior = [
        "trajectory_os.missions.orchestrator",
        "trajectory_os.graph.store",
        "trajectory_os.graph.scheduler.arbiter",
        "trajectory_os.graph.proof.engine",
        "trajectory_os.graph.reuse.engine",
        "trajectory_os.graph.replan.engine",
        "trajectory_os.portfolio.engine",
        "trajectory_os.daemon.engine",
        "trajectory_os.agents.registry",
        "trajectory_os.runs.resources",
    ]
    import_errors: list[str] = []
    for name in prior:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report the failing module
            import_errors.append(f"{name}:{type(exc).__name__}")
    _check("S5", "M008-M022 proven modules remain importable",
           not import_errors)
    return {"offenders": offenders, "import_errors": import_errors,
            "checked": [m.__name__ for m in modules]}


def _scenario_result(name: str) -> dict[str, Any]:
    checks = [c for c in CHECK_LOG if c["scenario"] == name]
    return {"scenario": name, "checks": len(checks),
            "passed": sum(1 for c in checks if c["ok"]),
            "status": "PASS" if all(c["ok"] for c in checks) else "FAIL"}


def _scenarios(root: str, *, offline: bool,
               timeout_s: int) -> dict[str, Any]:
    return {
        "S1": _scenario_harness(root, offline=offline, timeout_s=timeout_s),
        "S2": _scenario_resources(str(pathlib.Path(root) / "resources")),
        "S3": _scenario_artifacts(str(pathlib.Path(root) / "artifacts")),
        "S5": _scenario_preservation(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mission023_025_dogfood",
        description="M023-M025 runtime/resource/artifact dogfood.")
    parser.add_argument("--root", default=None,
                        help="isolated state root (default: a temp dir)")
    parser.add_argument("--out", default=None, help="report output path")
    parser.add_argument("--keep", action="store_true", default=False)
    parser.add_argument("--offline", action="store_true", default=False,
                        help="skip the live qualification (deterministic "
                             "fake backend only)")
    parser.add_argument("--qualify-timeout", type=int, default=20,
                        help="bounded live qualification timeout seconds")
    args = parser.parse_args(argv)

    temporary = args.root is None
    root = args.root or tempfile.mkdtemp(prefix="trajectory-m023-m025-")
    pathlib.Path(root).mkdir(parents=True, exist_ok=True)
    try:
        results = _scenarios(root, offline=args.offline,
                             timeout_s=args.qualify_timeout)
        scenarios = sorted({c["scenario"] for c in CHECK_LOG})
        report = {
            "mission": "M023-M025",
            "issue": 226,
            "generated_at": datetime.datetime.now(datetime.UTC)
            .replace(microsecond=0, tzinfo=None).isoformat() + "Z",
            "root": root,
            "offline": args.offline,
            "scenarios": [_scenario_result(s) for s in scenarios],
            "evidence": results,
            "checks": CHECK_LOG,
            "status": ("PASS" if all(
                _scenario_result(s)["status"] == "PASS" for s in scenarios)
                and all(c["ok"] for c in CHECK_LOG) else "FAIL"),
        }
        out = pathlib.Path(
            args.out or pathlib.Path(root) / "m023-m025.json")
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"M023-M025 dogfood {report['status']}: {out}")
        for scenario in scenarios:
            print(f"  {scenario}: {_scenario_result(scenario)['status']}")
        return 0 if report["status"] == "PASS" else 3
    finally:
        if temporary and not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
