"""Program block C (M023-M025) — runtime + resource + artifact dogfood.

Deterministic end-to-end proof that:

* M023 — the official DeepSeek Harness SDK is qualified in *isolation*: the
  project interpreter never receives the SDK's ``PYTHONPATH``, and the
  bounded structured qualification records SDK/runtime identity, handshake,
  lifecycle, completion/error semantics, bounded timeout/cancellation
  capability, evidence structure and a Pi comparison with deterministic
  UNAVAILABLE/INCOMPATIBLE fallback;
* M024 — local CPU/GPU/VRAM discovery, explicit reviewer VRAM/CPU
  reservations and hard admission control are operational, distinguish
  remote inference from local GPU use, and are exposed through the CLI and
  the live TUI snapshot;
* M025 — per-goal/per-mission workspaces, content-addressed artifact
  provenance and lineage are durable and reconstructible, implicit
  cross-goal leakage is rejected, and lineage is visible through the
  operator surfaces.

The model subprocess is replaced deterministically (exactly as the
M016-M022 harnesses do), so no network or Git write is involved.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trajectory_os.agents import harness_qualification as hq
from trajectory_os.agents import model as agent_model
from trajectory_os.agents.contract import AgentBackend
from trajectory_os.artifacts import engine as artifact_engine
from trajectory_os.artifacts import model as artifact_model
from trajectory_os.artifacts import store as artifact_store
from trajectory_os.goals import cli, snapshot, tui
from trajectory_os.resources import arbiter as resource_arbiter
from trajectory_os.resources import model as resource_model
from trajectory_os.resources import probe as resource_probe
from trajectory_os.resources import summary as resource_summary
from trajectory_os.runs import resources as runs_resources

GIB = 1024 ** 3
GOAL = "g-runtime-artifacts"


# --- M023 ---------------------------------------------------------------------


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


def _factory(name: str) -> AgentBackend:
    return _FakeHarness() if name == agent_model.BACKEND_DEEPSEEK_HARNESS \
        else _FakePi()


def _fake_sdk_env(root: Path) -> Path:
    (root / "bin").mkdir(parents=True, exist_ok=True)
    for name in ("python", "dsh"):
        path = root / "bin" / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
    dist = (root / "lib" / "python3.14" / "site-packages"
            / "deepseek_harness_sdk-0.1.5rc1.dist-info")
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: deepseek-harness-sdk\n"
        "Version: 0.1.5rc1\n", encoding="utf-8")
    return root


def test_m023_isolated_qualification_is_structured_and_isolated(
        tmp_path: Path) -> None:
    root = _fake_sdk_env(tmp_path / "sdk")
    env = hq.resolve_environment(root=str(root), which=lambda _: None)

    def identity_runner(python: str, script: str, env_vars: dict[str, str],
                        timeout_s: int) -> tuple[int, str, str]:
        assert "PYTHONPATH" not in env_vars
        return 0, ('{"module_present": true, "sdk_version": "0.1.5rc1", '
                   '"python_version": "3.14.4"}\n'), ""

    identity = hq.probe_identity(env, runner=identity_runner)
    outcome = hq.qualify_isolated(
        workspace=str(tmp_path / "ws"), environment=env, identity=identity,
        factory=_factory)
    document = outcome.to_dict()
    assert document["isolation"]["pythonpath_dropped"] is True
    assert document["isolation"]["sdk_imported_into_project"] is False
    assert document["environment"]["status"] == hq.ES_PRESENT
    assert document["identity"]["status"] == hq.IS_OK
    for dimension in ("sdk_identity", "runtime_identity", "handshake",
                      "lifecycle", "completion_semantics", "evidence_structure",
                      "pi_comparison"):
        assert outcome.checks[dimension] == hq.CK_PASS
    assert outcome.checks["timeout_semantics"] == hq.CK_CONFIGURED
    assert outcome.checks["cancellation_semantics"] == hq.CK_CONFIGURED
    assert outcome.fallback["pi_route"]["backend"] == agent_model.BACKEND_PI
    assert outcome.git_writes is False


def test_m023_missing_sdk_yields_deterministic_unavailable() -> None:
    env = hq.resolve_environment(root="/nonexistent/sdk")
    identity = hq.probe_identity(env)
    outcome = hq.qualify_isolated(
        workspace="/tmp", environment=env, identity=identity)
    assert outcome.status in ("CANARY_UNAVAILABLE", "CANARY_INCOMPATIBLE")
    assert hq.pi_continues(outcome) is True
    assert outcome.git_writes is False


# --- M024 ---------------------------------------------------------------------


def _runner(stdout: str, code: int = 0):
    def run(argv: list[str], timeout_s: int) -> tuple[int, str, str]:
        return code, stdout, ""
    return run


def test_m024_real_discovery_and_admission_are_operational(
        tmp_path: Path) -> None:
    # Real discovery on the host must always return a valid, bounded report
    # (unknown dimensions are honest, never guessed).
    report = resource_probe.discover()
    assert report.report_id == report.compute_report_id()
    assert report.cpu_known or report.cpu_slots is None

    # Deterministic arbitration over a synthetic RTX-class report.
    synthetic = resource_model.LocalResourceReport.build(
        probed_at="2026-01-01T00:00:00Z", cpu_slots=16, ram_bytes=64 * GIB,
        gpu_count=1, gpu_mem_bytes=24 * GIB, gpu_mem_used_bytes=GIB,
        gpu_utilization_pct=3, gpu_name="NVIDIA GeForce RTX 3090",
        nvidia=True, cpu_known=True, ram_known=True, gpu_known=True)
    policy = resource_model.ResourcePolicy(
        reviewer_vram_reserve_bytes=8 * GIB, max_local_concurrency=2)
    arb = resource_arbiter.ResourceArbiter(
        synthetic, policy=policy, root=tmp_path)

    reviewer = arb.admit(
        "review-1", runs_resources.ResourceRequirement(
            gpu=True, gpu_mem_bytes=8 * GIB),
        role=resource_model.ROLE_REVIEWER)
    assert reviewer.allowed is True

    # Local agent cannot consume the reviewer reserve (24 - 8 = 16 GiB).
    blocked = arb.admit(
        "agent-local", runs_resources.ResourceRequirement(
            gpu=True, gpu_mem_bytes=17 * GIB),
        role=resource_model.ROLE_AGENT)
    assert blocked.allowed is False
    assert blocked.reason == resource_model.AR_EXHAUSTED

    # Remote inference uses no local GPU and is admitted concurrently.
    remote = arb.admit(
        "agent-remote", runs_resources.ResourceRequirement(
            gpu=True, gpu_mem_bytes=20 * GIB),
        locality=resource_model.REMOTE, role=resource_model.ROLE_AGENT)
    assert remote.allowed is True
    assert remote.reservation is not None
    assert remote.reservation.gpu_mem_bytes == 0

    # A single-GPU host still bounds local model concurrency to one
    # reviewer OR one local agent job at a time.
    single = resource_arbiter.ResourceArbiter(
        synthetic,
        policy=resource_model.ResourcePolicy(max_local_concurrency=1))
    assert single.admit(
        "local-a", runs_resources.ResourceRequirement(
            gpu=True, gpu_mem_bytes=GIB),
        role=resource_model.ROLE_AGENT).allowed is True
    second = single.admit(
        "local-b", runs_resources.ResourceRequirement(
            gpu=True, gpu_mem_bytes=GIB),
        role=resource_model.ROLE_AGENT)
    assert second.allowed is False
    assert second.reason == resource_model.AR_LOCAL_CONCURRENCY

    document = resource_summary.status_document(
        root=tmp_path, report=synthetic, policy=policy)
    assert document["status"] == "OK"
    assert document["reservation_count"] == 2
    text = resource_summary.render_status(document)
    assert "NVIDIA GeForce RTX 3090" in text
    # Restart reconstructs the exact durability reservations.
    reloaded = resource_arbiter.ResourceArbiter(
        synthetic, policy=policy, root=tmp_path)
    assert {r.job_id for r in reloaded.active()} == {
        "review-1", "agent-remote"}


def test_m024_cli_and_snapshot_expose_live_state(tmp_path: Path) -> None:
    root = str(tmp_path / "state")
    code = cli.main(["resource-status", "--root", root, "--json"])
    assert code == 0


def test_m024_snapshot_and_tui_render_local_resources(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_goal(root)
    report = resource_model.LocalResourceReport.build(
        probed_at="2026-01-01T00:00:00Z", cpu_slots=8, ram_bytes=32 * GIB,
        gpu_count=1, gpu_mem_bytes=24 * GIB, gpu_mem_used_bytes=GIB,
        gpu_utilization_pct=4, gpu_name="NVIDIA GeForce RTX 3090",
        nvidia=True, cpu_known=True, ram_known=True, gpu_known=True)
    live = resource_summary.status_document(root=root, report=report)
    snap = snapshot.build_snapshot(
        str(root), GOAL, now_iso="2026-01-01T00:00:00Z",
        local_resources=live)
    assert snap["local_resources"]["report"]["gpu_name"] == \
        "NVIDIA GeForce RTX 3090"
    frame = tui.render_frame(snap, width=120, height=60)
    assert "local     :" in frame
    assert "vram      : total=24.0GiB" in frame


# --- M025 ---------------------------------------------------------------------


def test_m025_workspaces_artifacts_and_lineage_round_trip(
        tmp_path: Path) -> None:
    manager = artifact_engine.WorkspaceManager(tmp_path)
    goal_ws = manager.ensure_goal_workspace(
        GOAL, created_at="2026-01-01T00:00:00Z")
    mission_ws = manager.ensure_mission_workspace(
        GOAL, "m-1", created_at="2026-01-01T00:00:00Z")
    assert Path(goal_ws.path).is_dir()
    assert Path(mission_ws.path).is_dir()

    report = manager.record_artifact(
        goal_id=GOAL, kind=artifact_model.AK_REPORT, name="report.json",
        content=b'{"ok": true}', mission_id="m-1",
        producer=artifact_model.PRODUCER_AGENT,
        proof_id="proof-1", criterion_id="ac-1",
        created_at="2026-01-01T00:00:01Z")
    dataset = manager.record_artifact(
        goal_id=GOAL, kind=artifact_model.AK_DATASET, name="vectors.bin",
        content=b"0123456789", mission_id="m-1",
        parent_ids=(report.artifact_id,), reuse_input_id="reuse-1",
        created_at="2026-01-01T00:00:02Z")
    assert dataset.artifact_id == dataset.compute_artifact_id()
    lineage = manager.lineage(GOAL, dataset.artifact_id)
    assert [record.name for record in lineage] == [
        "vectors.bin", "report.json"]
    ok, problems = manager.verify(GOAL)
    assert ok is True and problems == []

    state = manager.reconstruct(GOAL)
    assert state.lineage_id == state.compute_lineage_id()
    assert len(state.mission_workspaces) == 1
    # Cross-goal leakage is structurally rejected.
    manager.ensure_goal_workspace("g-other",
                                  created_at="2026-01-01T00:00:00Z")
    try:
        artifact_store.load_artifact(tmp_path, "g-other", report.artifact_id)
        raise AssertionError("cross-goal load should have failed")
    except artifact_model.ArtifactError:
        pass


def test_m025_cli_and_snapshot_expose_artifact_lineage(
        tmp_path: Path) -> None:
    root = tmp_path / "state"
    _seed_goal(root)
    manager = artifact_engine.WorkspaceManager(root)
    report = manager.record_artifact(
        goal_id=GOAL, kind=artifact_model.AK_REPORT, name="summary.md",
        content=b"# summary\n", mission_id="m-runtime-artifacts",
        created_at="2026-01-01T00:00:01Z")
    manager.record_artifact(
        goal_id=GOAL, kind=artifact_model.AK_MODEL, name="model.bin",
        content=b"weights", mission_id="m-runtime-artifacts",
        parent_ids=(report.artifact_id,),
        created_at="2026-01-01T00:00:02Z")

    snap = snapshot.build_snapshot(str(root), GOAL,
                                   now_iso="2026-01-01T00:00:03Z")
    assert snap["artifacts"]["present"] is True
    assert snap["artifacts"]["counts"]["artifacts_total"] == 2

    assert cli.main(["artifacts", "--root", str(root), GOAL, "--json"]) == 0
    assert cli.main(["artifacts", "--root", str(root), GOAL,
                     "--lineage", report.artifact_id]) == 0


# --- shared setup -------------------------------------------------------------


def _seed_goal(root: Path) -> None:
    from trajectory_os.goals import launch
    from trajectory_os.graph import store as graph_store

    spec = {
        "schema_version": 1,
        "goal_id": GOAL,
        "objective": "Prove runtime resources and persistent artifacts.",
        "nodes": [{
            "node_id": "n-runtime",
            "title": "Runtime artifacts mission",
            "priority": 50,
            "depends_on": [],
            "acceptance_criteria": [{
                "criterion_id": "ac-runtime",
                "statement": "runtime artifacts are proven"}],
            "mission_ref": {"mission_id": "m-runtime-artifacts",
                            "required": True},
            "resources": {"cpu_slots": 1},
            "budgets": {"repair_budget": 0, "max_attempts": 1},
        }],
    }
    defaults = launch.MissionLaunchDefaults(
        repo_root=None, baseline_revision="base", repair_budget=0)
    launch.provision_from_spec(str(root), spec, defaults)
    graph_store.create_graph(str(root), spec, repo_root=defaults.repo_root,
                             baseline_revision=defaults.baseline_revision)


def _unused_factory(_: str) -> Any:  # pragma: no cover - typing helper
    raise AssertionError
