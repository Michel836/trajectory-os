"""M048–M055 — deterministic persistent-platform acceptance matrix.

Every case drives the *real* platform code with fixture executors, scripted
reviewers, an explicit clock and in-memory Git/GitHub fixtures. The report is
a pure function of those inputs: no credentials, network, real repository
mutation or Git trust-boundary write.

The matrix proves the fifty-seven claims listed in ``_CASES``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.observability import store as obs_store
from trajectory_os.operator import acceptance as operator_acceptance
from trajectory_os.operator.policy import (
    release_policy,
)
from trajectory_os.platform import api as api_module
from trajectory_os.platform import cli as platform_cli
from trajectory_os.platform import efficiency as efficiency_module
from trajectory_os.platform import hardening as hardening_module
from trajectory_os.platform import inbox as inbox_module
from trajectory_os.platform import lifeos as lifeos_module
from trajectory_os.platform import model
from trajectory_os.platform import projects as project_registry
from trajectory_os.platform import queue as queue_module
from trajectory_os.platform import supervisor as supervisor_module
from trajectory_os.release import model as release_model
from trajectory_os.release import store as release_store

ACCEPTANCE_VERSION = "m055.1"
TOKEN = "m048-m055-acceptance-token"

AcceptanceCheck = operator_acceptance.AcceptanceCheck
AcceptanceCase = operator_acceptance.AcceptanceCase

_SCRIPTED = operator_acceptance.ScriptedClock

_WRITE_VERBS = ("commit", "push", "merge", "reset", "restore", "clean",
                "stash", "rebase", "switch", "checkout")


@dataclass(frozen=True)
class PlatformAcceptanceReport:
    version: str
    root: str
    generated_at: str
    cases: tuple[AcceptanceCase, ...]

    @property
    def status(self) -> str:
        return "PASS" if all(case.ok for case in self.cases) else "FAIL"

    @property
    def checks(self) -> tuple[AcceptanceCheck, ...]:
        return tuple(c for case in self.cases for c in case.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": f"trajectory-platform-acceptance/{self.version}",
            "version": self.version,
            "generated_at": self.generated_at,
            "root": self.root,
            "status": self.status,
            "cases": [case.to_dict() for case in self.cases],
            "summary": {
                "cases": len(self.cases),
                "passed_cases": sum(1 for c in self.cases if c.ok),
                "checks": len(self.checks),
                "passed_checks": sum(1 for c in self.checks if c.ok),
            },
        }

    def render(self) -> str:
        lines = [
            "=" * 72,
            f"TRAJECTORY-OS PLATFORM ACCEPTANCE ({self.version})",
            f"status : {self.status}",
            f"root   : {self.root}",
            f"at     : {self.generated_at}",
            "-" * 72,
        ]
        for case in self.cases:
            lines.append(f"[{'PASS' if case.ok else 'FAIL'}] "
                         f"{case.case}: {case.title}")
            for check in case.checks:
                lines.append(
                    f"    {'ok  ' if check.ok else 'FAIL'} "
                    f"{check.description}")
        lines.append("-" * 72)
        lines.append(
            f"{sum(1 for c in self.cases if c.ok)}/{len(self.cases)} cases, "
            f"{sum(1 for c in self.checks if c.ok)}/{len(self.checks)} checks")
        lines.append("=" * 72)
        return "\n".join(lines)


def _check(checks: list[AcceptanceCheck], case: str, description: str,
           ok: bool, **detail: Any) -> None:
    checks.append(AcceptanceCheck(case=case, description=description,
                                  ok=bool(ok), detail=detail))


def _digest_root(path: Path) -> str:
    material = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            material.update(str(item.relative_to(path)).encode("utf-8"))
            material.update(item.read_bytes())
    return material.hexdigest()


def _mission_pipe(root: str, mission_id: str) -> Any:
    return operator_acceptance.OperatorPipeline(str(root), mission_id)


def _ready_mission(root: str, mission_id: str) -> Any:
    pipe = _mission_pipe(root, mission_id)
    pipe.ready()
    return pipe


# --- cases 1-6: project registry ----------------------------------------------


def _case_project_registry(root: str, checks: list[AcceptanceCheck]) -> None:
    project = project_registry.create_project(
        root, name="Registry", workspace=str(Path(root) / "ws-registry"),
        description="registry project")
    _check(checks, "1", "project create yields a stable identity",
           bool(project.project_id)
           and project.project_id == project_registry.project_id_for(
               "Registry"))
    loaded = project_registry.load_project(root, project.project_id)
    _check(checks, "2", "project read returns the exact document",
           loaded.to_dict() == project.to_dict())
    updated = project_registry.update_project(
        root, project.project_id, description="updated",
        default_policy_profile="safe")
    _check(checks, "3", "project update changes only explicit metadata",
           updated.description == "updated"
           and updated.default_policy_profile == "safe"
           and updated.project_id == project.project_id)
    archived = project_registry.archive_project(root, project.project_id)
    _check(checks, "4", "project archive is explicit and idempotent",
           archived.lifecycle == project_registry.PL_ARCHIVED
           and project_registry.archive_project(
               root, project.project_id).lifecycle
           == project_registry.PL_ARCHIVED)
    # mission linkage on an active project
    active = project_registry.create_project(
        root, name="Linkage", workspace=str(Path(root) / "ws-linkage"))
    project_registry.add_objective(root, active.project_id,
                                   objective_id="obj-1", title="Obj")
    project_registry.link_mission(root, active.project_id,
                                  objective_id="obj-1", mission_id="m-link")
    linkage = project_registry.project_linkage(root, active.project_id)
    linked = linkage["objectives"][0]["missions"][0]["mission_id"]
    _check(checks, "5", "project -> objective -> mission linkage is durable",
           linked == "m-link")
    reconstruction = project_registry.reconstruct_projects(root)
    _check(checks, "6", "projects reconstruct deterministically after restart",
           reconstruction["reconstructed"]
           and active.project_id in reconstruction["project_ids"])


# --- cases 7-11: supervisor ---------------------------------------------------


def _case_supervisor(root: str, checks: list[AcceptanceCheck]) -> None:
    sup_root = str(Path(root) / "supervisor-case")
    live_pid = os.getpid()
    owner = supervisor_module.build_owner(sup_root, pid=live_pid,
                                          token="owner-token")
    outcome, _ = supervisor_module.claim_ownership(sup_root, owner)
    _check(checks, "7", "supervisor single-owner claim succeeds",
           outcome == supervisor_module.CLAIM_FRESH)
    other = supervisor_module.build_owner(sup_root, pid=live_pid + 1000,
                                          token="other-token")
    denied, previous = supervisor_module.claim_ownership(sup_root, other)
    _check(checks, "8", "a duplicate live supervisor is blocked",
           denied == supervisor_module.CLAIM_DENIED
           and previous is not None and previous.pid == live_pid)
    # simulate a dead owner: state RUNNING + lock with a dead pid
    dead = supervisor_module.build_owner(sup_root, pid=999999999,
                                         token="dead-token")
    from trajectory_os.operator._util import write_json

    write_json(supervisor_module.owner_path(sup_root), dead.to_dict())
    state = supervisor_module.SupervisorState.initial(
        started_at="2026-01-01T00:00:00Z", owner=dead, project_ids=())
    supervisor_module.save_state(sup_root, state)
    crash = supervisor_module.detect_crash(sup_root)
    _check(checks, "9", "a dead owner is detected as a crash",
           crash["crash_detected"] is True and crash["owner_alive"] is False)
    recovered, previous = supervisor_module.claim_ownership(
        sup_root, supervisor_module.build_owner(sup_root, pid=4244,
                                                token="recover-token"))
    _check(checks, "10", "supervisor restarts by explicit stale recovery",
           recovered == supervisor_module.CLAIM_RECOVERED)
    report = supervisor_module.run_supervisor(
        sup_root, max_cycles=1,
        cycle_fn=lambda r, c: {"reason": "TEST", "complete": False},
        owner=supervisor_module.build_owner(sup_root, pid=4245,
                                            token="run-token"))
    _check(checks, "10", "restarted supervisor completes a bounded cycle",
           report.cycles == 1 and _supervisor_cycles(sup_root) == 1)
    # terminal independence: detached process, no stdin
    term_root = str(Path(root) / "supervisor-term")
    Path(term_root).mkdir(parents=True, exist_ok=True)
    pid = platform_cli.spawn_supervisor(term_root, max_cycles=1)
    deadline = time.time() + 15.0
    finished = False
    while time.time() < deadline:
        if _supervisor_finished(term_root):
            finished = True
            break
        time.sleep(0.05)
    _check(checks, "11", "supervisor is terminal-independent (detached)",
           pid > 0 and finished, pid=pid)


# --- cases 12-23: queue and scheduler -----------------------------------------


def _seed_projects(root: str) -> str:
    project = project_registry.create_project(
        root, name="Portfolio", workspace=str(Path(root) / "ws-portfolio"))
    project_registry.add_objective(root, project.project_id,
                                   objective_id="p-obj", title="Portfolio obj")
    return project.project_id


def _case_queue(root: str, project_id: str,
                checks: list[AcceptanceCheck]) -> None:
    # priority
    q_root = str(Path(root) / "queue-priority")
    Path(q_root).mkdir(parents=True, exist_ok=True)
    project_registry.create_project(
        q_root, name="Q", workspace=str(Path(q_root) / "ws"))
    project = project_registry.list_projects(q_root)[0]
    queue_module.enqueue(q_root, mission_id="low", project_id=project.project_id,
                         priority=1)
    queue_module.enqueue(q_root, mission_id="high",
                         project_id=project.project_id, priority=10)
    decision = queue_module.schedule_once(
        q_root, policy=queue_module.QueuePolicy(max_concurrent=1))
    _check(checks, "12", "higher priority is selected first",
           decision.selected == ("high",))
    # dependency blocking / unblocking
    d_root = str(Path(root) / "queue-deps")
    Path(d_root).mkdir(parents=True, exist_ok=True)
    project_registry.create_project(
        d_root, name="D", workspace=str(Path(d_root) / "ws"))
    dproj = project_registry.list_projects(d_root)[0]
    queue_module.enqueue(d_root, mission_id="dep", project_id=dproj.project_id)
    queue_module.enqueue(d_root, mission_id="blocked",
                         project_id=dproj.project_id,
                         dependencies=("dep",))
    blocked = queue_module.schedule_once(
        d_root, policy=queue_module.QueuePolicy(max_concurrent=1))
    _check(checks, "13", "a dependency blocks its dependent",
           "blocked" not in blocked.selected
           and any(a.reason == queue_module.R_DEPENDENCY_PENDING
                   for a in blocked.admissions))
    queue_module.mark_state(d_root, "dep", queue_module.Q_DONE)
    unblocked = queue_module.schedule_once(
        d_root, policy=queue_module.QueuePolicy(max_concurrent=1))
    _check(checks, "14", "a completed dependency unblocks its dependent",
           "blocked" in unblocked.selected)
    # bounded concurrency
    c_root = str(Path(root) / "queue-conc")
    Path(c_root).mkdir(parents=True, exist_ok=True)
    project_registry.create_project(
        c_root, name="C", workspace=str(Path(c_root) / "ws"))
    cproj = project_registry.list_projects(c_root)[0]
    for name in ("c1", "c2", "c3"):
        queue_module.enqueue(c_root, mission_id=name,
                             project_id=cproj.project_id)
    conc = queue_module.schedule_once(
        c_root, policy=queue_module.QueuePolicy(max_concurrent=2))
    _check(checks, "15", "bounded concurrency admits exactly the limit",
           len(conc.selected) == 2
           and any(a.reason == queue_module.R_CONCURRENCY_LIMIT
                   for a in conc.admissions))
    # GPU / VRAM admission
    g_root = str(Path(root) / "queue-gpu")
    Path(g_root).mkdir(parents=True, exist_ok=True)
    project_registry.create_project(
        g_root, name="G", workspace=str(Path(g_root) / "ws"))
    gproj = project_registry.list_projects(g_root)[0]
    queue_module.enqueue(
        g_root, mission_id="gpu-heavy", project_id=gproj.project_id,
        resources=queue_module.ResourceRequest(
            cpu_slots=1, gpu_slots=1, gpu_mem_bytes=6 << 30,
            locality=queue_module.LOCAL))
    queue_module.enqueue(
        g_root, mission_id="gpu-second", project_id=gproj.project_id,
        resources=queue_module.ResourceRequest(
            cpu_slots=1, gpu_slots=1, gpu_mem_bytes=6 << 30,
            locality=queue_module.LOCAL))
    gpu_decision = queue_module.schedule_once(
        g_root, policy=queue_module.QueuePolicy(
            max_concurrent=4, gpu_slots=2, gpu_mem_bytes=8 << 30))
    _check(checks, "16", "local GPU/VRAM admission is enforced",
           len(gpu_decision.selected) == 1
           and any(a.reason == queue_module.R_VRAM_LIMIT
                   for a in gpu_decision.admissions))
    # remote provider admission
    r_root = str(Path(root) / "queue-remote")
    Path(r_root).mkdir(parents=True, exist_ok=True)
    project_registry.create_project(
        r_root, name="R", workspace=str(Path(r_root) / "ws"))
    rproj = project_registry.list_projects(r_root)[0]
    queue_module.enqueue(
        r_root, mission_id="remote", project_id=rproj.project_id,
        resources=queue_module.ResourceRequest(provider="deepseek"))
    remote_decision = queue_module.schedule_once(
        r_root, provider_available={"deepseek": False})
    _check(checks, "17", "an unavailable remote provider blocks admission",
           any(a.reason == queue_module.R_REMOTE_PROVIDER_UNAVAILABLE
               for a in remote_decision.admissions)
           and not remote_decision.selected)
    # deterministic reason: identical fixtures yield identical decisions
    det_a = str(Path(root) / "queue-det-a")
    det_b = str(Path(root) / "queue-det-b")
    decisions_seen: list[str] = []
    for det_root in (det_a, det_b):
        Path(det_root).mkdir(parents=True, exist_ok=True)
        project_registry.create_project(
            det_root, name="Det", workspace=str(Path(det_root) / "ws"))
        dproj = project_registry.list_projects(det_root)[0]
        queue_module.enqueue(det_root, mission_id="d1",
                             project_id=dproj.project_id, priority=1)
        queue_module.enqueue(det_root, mission_id="d2",
                             project_id=dproj.project_id, priority=5)
        det = queue_module.schedule_once(
            det_root, policy=queue_module.QueuePolicy(max_concurrent=1))
        decisions_seen.append(f"{det.decision_id}:{det.reason}")
    _check(checks, "18", "scheduling decisions are deterministic",
           decisions_seen[0] == decisions_seen[1] and bool(decisions_seen[0]))
    # starvation resistance
    s_root = str(Path(root) / "queue-starve")
    Path(s_root).mkdir(parents=True, exist_ok=True)
    project_registry.create_project(
        s_root, name="S", workspace=str(Path(s_root) / "ws"))
    sproj = project_registry.list_projects(s_root)[0]
    queue_module.enqueue(s_root, mission_id="old-low",
                         project_id=sproj.project_id, priority=1)
    for index in range(5):
        queue_module.enqueue(s_root, mission_id=f"filler-{index}",
                             project_id=sproj.project_id, priority=0)
    # the low-priority oldest entry should be selected by aging
    starve = queue_module.schedule_once(
        s_root, policy=queue_module.QueuePolicy(max_concurrent=1))
    _check(checks, "19", "starvation resistance ages waiting entries",
           starve.selected == ("old-low",))
    # no duplicate execution after restart
    n_root = str(Path(root) / "queue-nodup")
    _ready_mission(n_root, "already-ready")
    project_registry.create_project(
        n_root, name="N", workspace=str(Path(n_root) / "ws"))
    nproj = project_registry.list_projects(n_root)[0]
    queue_module.enqueue(n_root, mission_id="already-ready",
                         project_id=nproj.project_id)
    nodup = queue_module.schedule_once(n_root)
    _check(checks, "20", "a canonically terminal mission is never re-executed",
           "already-ready" not in nodup.selected
           and _entry_state(n_root, "already-ready") == queue_module.Q_DONE)
    # pause / resume
    p_root = str(Path(root) / "queue-pause")
    Path(p_root).mkdir(parents=True, exist_ok=True)
    project_registry.create_project(
        p_root, name="P", workspace=str(Path(p_root) / "ws"))
    pproj = project_registry.list_projects(p_root)[0]
    queue_module.enqueue(p_root, mission_id="p1", project_id=pproj.project_id)
    queue_module.pause(p_root, "p1")
    queue_module.resume(p_root, "p1")
    _check(checks, "21", "queued pause/resume is explicit and reversible",
           _entry_state(p_root, "p1") == queue_module.Q_QUEUED)
    # active cancel
    queue_module.mark_state(p_root, "p1", queue_module.Q_ACTIVE)
    queue_module.cancel(p_root, "p1")
    _check(checks, "22", "active cancel is recorded",
           _entry_state(p_root, "p1") == queue_module.Q_CANCELLED)
    # requeue
    queue_module.requeue(p_root, "p1")
    _check(checks, "23", "requeue returns a cancelled mission to the queue",
           _entry_state(p_root, "p1") == queue_module.Q_QUEUED)


# --- cases 24-29: inbox -------------------------------------------------------


def _case_inbox(root: str, checks: list[AcceptanceCheck]) -> None:
    # GO COMMIT inbox generation
    commit_pipe = _mission_pipe(root, "inbox-commit")
    commit_pipe.ready()
    commit_pipe.handoff()
    inbox_module.refresh(root)
    records = inbox_module.list_notifications(root)
    _check(checks, "24", "GO COMMIT gate generates an inbox notification",
           any(r.type == inbox_module.N_GO_COMMIT_REQUIRED
               and r.mission_id == "inbox-commit" for r in records))
    # GO MERGE inbox generation
    merge_pipe = _mission_pipe(root, "inbox-merge")
    merge_pipe.ready()
    merge_pipe.handoff()
    merge_pipe.commit()
    merge_pipe.bind()
    merge_pipe.set_ci(release_model.CI_SUCCESS)
    merge_pipe.watch()
    merge_pipe.merge_handoff()
    inbox_module.refresh(root)
    records = inbox_module.list_notifications(root)
    _check(checks, "25", "GO MERGE gate generates an inbox notification",
           any(r.type == inbox_module.N_GO_MERGE_REQUIRED
               and r.mission_id == "inbox-merge" for r in records))
    before = len(records)
    second = inbox_module.refresh(root)
    _check(checks, "26", "notifications are deduplicated across recovery",
           second["created"] == 0
           and len(inbox_module.list_notifications(root)) == before)
    target = next(r for r in records
                  if r.type == inbox_module.N_GO_COMMIT_REQUIRED)
    acknowledged = inbox_module.acknowledge(root, target.notification_id)
    _check(checks, "27", "notifications can be acknowledged",
           acknowledged.state == inbox_module.NS_ACKNOWLEDGED)
    resolved = inbox_module.resolve(root, target.notification_id)
    _check(checks, "28", "notifications can be resolved",
           resolved.state == inbox_module.NS_RESOLVED)
    unavailable = inbox_module.notify_send_adapter(
        notification_id_value=target.notification_id, title="t", body="b",
        which=lambda name: None)
    _check(checks, "29", "notify-send absence degrades explicitly",
           unavailable["status"] == "UNAVAILABLE"
           and bool(unavailable["reason"]))


# --- cases 30-34: API ---------------------------------------------------------


def _case_api(root: str, project_id: str,
              checks: list[AcceptanceCheck]) -> None:
    del project_id
    api = api_module.LocalApi(root)
    before = _digest_root(Path(root) / model.PLATFORM_DIR)
    read_ok = True
    for path in ("/api/projection", "/api/projects", "/api/queue",
                 "/api/inbox", "/api/supervisor", "/api/health", "/"):
        if api.handle("GET", path).status != 200:
            read_ok = False
    after = _digest_root(Path(root) / model.PLATFORM_DIR)
    _check(checks, "30", "API observation is read-only",
           read_ok and before == after)
    projection = api_module.build_projection(root)
    dashboard = api_module.render_dashboard(projection)
    mission_rows = len(projection["missions"])
    _check(checks, "31", "dashboard and canonical projection agree",
           projection["projection_id"] in dashboard
           and dashboard.count("<tr>") == mission_rows + 1
           and projection["platform_version"] in dashboard)
    # mutation isolation: unauthenticated mutation refused, state unchanged
    state_before = _digest_root(Path(root) / model.PLATFORM_DIR)
    refused = api.handle("POST", "/api/inbox/refresh")
    _check(checks, "32", "mutations are isolated behind explicit auth",
           refused.status == 403
           and _digest_root(Path(root) / model.PLATFORM_DIR) == state_before)
    code = _error_code(lambda: api_module.check_bind("0.0.0.0"))
    _check(checks, "33", "non-loopback binding is refused by default",
           code == model.E_API_BIND_FORBIDDEN, code=code)
    # contradictory canonical state fails closed
    contradiction_root = str(Path(root) / "contradiction")
    pipe = _ready_mission(contradiction_root, "contradiction-mission")
    mission_root = pipe.mission_root
    obs_store.write_json(mission_root / release_store.RELEASE_CLOSURE_NAME, {
        "schema_version": 1, "status": "CLOSED", "mission_id":
        "contradiction-mission"})
    contradiction_code = _error_code(
        lambda: api_module.build_projection(contradiction_root))
    _check(checks, "34", "contradictory canonical state fails closed",
           contradiction_code == model.E_STATE_CONTRADICTION,
           code=contradiction_code)


def _error_code(action: Any) -> str | None:
    try:
        action()
    except model.PlatformError as exc:
        return str(exc.code)
    except (RuntimeError, ValueError) as exc:
        return type(exc).__name__
    return None


def _supervisor_cycles(root: str) -> int:
    state = supervisor_module.load_state(root)
    return 0 if state is None else state.cycles


def _supervisor_finished(root: str) -> bool:
    state = supervisor_module.load_state(root)
    return (state is not None and state.cycles >= 1
            and not supervisor_module.stop_requested(root))


def _entry_state(root: str, mission_id: str) -> str | None:
    entry = queue_module.load_queue(root).by_id(mission_id)
    return None if entry is None else entry.state


# --- cases 35-39: LifeOS ------------------------------------------------------


def _case_lifeos(root: str, checks: list[AcceptanceCheck]) -> None:
    project = project_registry.create_project(
        root, name="LifeOS", workspace=str(Path(root) / "ws-lifeos"),
        description="lifeos projection project")
    project_registry.add_objective(root, project.project_id,
                                   objective_id="life-obj", title="Life objective")
    project_registry.link_mission(root, project.project_id,
                                  objective_id="life-obj",
                                  mission_id="life-mission")
    _ready_mission(root, "life-mission")
    vault = Path(root).parent / f"{Path(root).name}-vault"
    vault.mkdir(parents=True, exist_ok=True)
    config = lifeos_module.ObsidianConfig(vault=str(vault))
    report = lifeos_module.sync_project(root, config, project.project_id)
    note = (vault / "TrajectoryOS" / project.project_id
            / f"{project.name}.md")
    _check(checks, "35", "LifeOS note creation writes the project note",
           report.status == lifeos_module.LS_SYNCED and note.is_file())
    first = note.read_text(encoding="utf-8")
    again = lifeos_module.sync_project(root, config, project.project_id)
    _check(checks, "36", "LifeOS updates are idempotent",
           again.status == lifeos_module.LS_SYNCED
           and note.read_text(encoding="utf-8") == first
           and bool(again.skipped))
    journal = (vault / "TrajectoryOS" / project.project_id / "journal"
               / "life-mission.md")
    journal_text = journal.read_text(encoding="utf-8") if journal.is_file() \
        else ""
    _check(checks, "37", "LifeOS notes carry canonical provenance",
           "provenance" in first and "life-mission/status.json" in journal_text)
    degraded = lifeos_module.integration_status(
        root, lifeos_module.ObsidianConfig(
            vault=str(Path(root) / "no-such-vault")))
    _check(checks, "38", "an unavailable vault degrades explicitly",
           degraded["status"] == lifeos_module.LS_DEGRADED
           and bool(degraded["reason"]))
    secret_value = "super-secret-token-value-123"
    secrets = Path(root) / "secrets.json"
    secrets.write_text(json.dumps({"api_key": secret_value}),
                       encoding="utf-8")
    del secrets
    _check(checks, "39", "no secret can leak into a projected note",
           secret_value not in first
           and secret_value not in journal_text
           and not lifeos_module.scan_for_secrets(first))


# --- cases 40-45: efficiency --------------------------------------------------


def _trial(backend: str, provider: str, model_name: str, locality: str,
           duration: int, *, prompt_tokens: int = 100,
           prompt_source: str = "PROVIDER") -> dict[str, Any]:
    return {
        "benchmark_run_id": "run-1",
        "backend": backend, "provider": provider, "model": model_name,
        "locality": locality, "workload_class": "SMALL_TARGETED_REPAIR",
        "telemetry": {
            "total_duration_ms": duration,
            "ttft_ms": 50,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 200,
            "cache_read_tokens": 10,
            "prompt_tps": 20.0,
            "generation_tps": 30.0,
            "gpu_utilization_pct": 70,
            "vram_bytes": 4 << 30,
            "cost_usd": 0.01,
            "retries": 0,
            "repairs": 0,
            "protocol_errors": 0,
            "sources": {
                "total_duration_ms": "LOCAL", "ttft_ms": "LOCAL",
                "prompt_tokens": prompt_source,
                "completion_tokens": "PROVIDER",
                "cache_read_tokens": "PROVIDER",
                "prompt_tps": "DERIVED", "generation_tps": "DERIVED",
                "gpu_utilization_pct": "LOCAL", "vram_bytes": "LOCAL",
                "cost_usd": "PROVIDER", "retries": "DERIVED",
                "repairs": "DERIVED", "protocol_errors": "DERIVED",
            },
            "unavailable": {},
        },
        "validation": {"passed": True},
        "review": {"outcome": "VALID_PASS"},
        "patch": {"insertions": 10, "deletions": 2, "files_changed": 1},
        "agent": {},
    }


def _unavailable_trial() -> dict[str, Any]:
    trial = _trial("pi", "ollama", "qwen3.8:27b-q4_K_M", "local", 5000)
    trial["telemetry"]["gpu_utilization_pct"] = None
    trial["telemetry"]["sources"]["gpu_utilization_pct"] = "UNAVAILABLE"
    trial["telemetry"]["unavailable"]["gpu_utilization_pct"] = \
        "provider does not expose GPU utilization"
    return trial


def _case_efficiency(root: str, checks: list[AcceptanceCheck]) -> None:
    reference = efficiency_module.build_route_evidence([
        _trial("pi", "ollama", "qwen3.8:27b-q4_K_M", "local", 1000)])
    _check(checks, "40", "reference reviewer route telemetry is captured",
           bool(reference) and reference[0].label
           == efficiency_module.ROUTE_LOCAL_QWEN27
           and reference[0].metrics[efficiency_module.M_WALL_TIME].value
           == 1000)
    dev = efficiency_module.build_route_evidence([
        _trial("pi", "ollama", "qwen3.8-dev3090", "local", 800)])
    _check(checks, "41", "dev3090 route telemetry is captured",
           bool(dev) and dev[0].label == efficiency_module.ROUTE_LOCAL_DEV3090
           and dev[0].metrics[efficiency_module.M_GPU_UTILIZATION].value == 70)
    remote = efficiency_module.build_route_evidence([
        _trial("pi", "deepseek", "deepseek-flash", "remote", 1200)])
    _check(checks, "42", "remote implementation route telemetry is captured",
           bool(remote)
           and remote[0].label == efficiency_module.ROUTE_PI_DEEPSEEK
           and remote[0].metrics[efficiency_module.M_PROVIDER_COST].value
           == 0.01)
    unavailable = efficiency_module.build_route_evidence([
        _unavailable_trial()])
    metric = unavailable[0].metrics[efficiency_module.M_GPU_UTILIZATION]
    _check(checks, "43", "unavailable telemetry is None with a reason",
           metric.value is None
           and metric.provenance == efficiency_module.SRC_UNAVAILABLE
           and bool(metric.reason))
    _check(checks, "44", "explicit provenance is preserved per metric",
           reference[0].metrics[efficiency_module.M_PROMPT_TOKENS].provenance
           == efficiency_module.SRC_PROVIDER
           and reference[0].metrics[efficiency_module.M_WALL_TIME].provenance
           == efficiency_module.SRC_LOCAL)
    single = efficiency_module.recommend(reference)
    _check(checks, "45", "no superiority is claimed without comparison",
           single.supported is False and single.preferred_route is None
           and single.reason
           == "INSUFFICIENT_COMPARABLE_EVIDENCE")
    # persist evidence separately from policy
    evidence = efficiency_module.build_evidence([
        *_trial_set()])
    efficiency_module.persist_evidence(root, evidence)
    document = efficiency_module.load_evidence(root)
    _check(checks, "45", "routing evidence stays separate from policy",
           document is not None
           and document["authorization_state"]
           == efficiency_module.AUTHORIZATION_ADVISORY
           and document["policy_mutated"] is False
           and document["trust_gates_unchanged"] is True)


def _trial_set() -> list[dict[str, Any]]:
    return [
        _trial("pi", "ollama", "qwen3.8:27b-q4_K_M", "local", 1000),
        _trial("pi", "ollama", "qwen3.8-dev3090", "local", 800),
        _trial("pi", "deepseek", "deepseek-flash", "remote", 1200),
    ]


# --- cases 46-50: trust boundaries and human gates ----------------------------


def _scan_git_writes(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for target in paths:
        files = ([target] if target.is_file()
                 else sorted(target.rglob("*.py")) if target.is_dir() else [])
        for path in files:
            source = path.read_text(encoding="utf-8")
            for verb in _WRITE_VERBS:
                if f'"git", "{verb}"' in source:
                    offenders.append(f"{path.name}:{verb}")
    return offenders


def _case_trust(root: str, checks: list[AcceptanceCheck]) -> None:
    package = Path(__file__).resolve().parents[1]
    platform_dir = package / "platform"
    _check(checks, "46", "the supervisor never performs a release Git write",
           not _scan_git_writes([platform_dir / "supervisor.py",
                                 platform_dir / "supervisor_main.py"]))
    _check(checks, "47", "the scheduler never performs a release Git write",
           not _scan_git_writes([platform_dir / "queue.py",
                                 platform_dir / "inbox.py"]))
    policy = release_policy(clock=_SCRIPTED()).policy
    _check(checks, "48", "GO COMMIT remains an explicit human gate",
           policy.go_commit_required
           and not hasattr(inbox_module, "authorize"))
    _check(checks, "49", "exact-head CI remains required for release",
           policy.exact_head_ci_required)
    _check(checks, "50", "GO MERGE remains an explicit human gate",
           policy.go_merge_required
           and not hasattr(api_module, "go_merge"))


# --- cases 51-56: backup / restore / recovery ---------------------------------


def _hardening_fixture(root: str) -> dict[str, Any]:
    fixture_root = str(Path(root) / "hardening")
    project = project_registry.create_project(
        fixture_root, name="Hardened",
        workspace=str(Path(fixture_root) / "ws"))
    project_registry.add_objective(fixture_root, project.project_id,
                                   objective_id="h-obj", title="H")
    project_registry.link_mission(fixture_root, project.project_id,
                                  objective_id="h-obj",
                                  mission_id="h-mission")
    _ready_mission(fixture_root, "h-mission")
    queue_module.enqueue(fixture_root, mission_id="h-mission",
                         project_id=project.project_id)
    inbox_module.refresh(fixture_root)
    supervisor_module.run_supervisor(
        fixture_root, max_cycles=1,
        cycle_fn=lambda r, c: {"reason": "TEST", "complete": True})
    return {"root": fixture_root, "project_id": project.project_id}


def _case_hardening(root: str, checks: list[AcceptanceCheck]) -> None:
    fixture = _hardening_fixture(root)
    fixture_root = fixture["root"]
    backup_dir = str(Path(fixture_root) / "backup")
    manifest = hardening_module.backup(fixture_root, backup_dir)
    backed_up = {entry["path"] for entry in manifest["files"]}
    transient = {"platform/supervisor/owner.lock",
                 "platform/supervisor/heartbeat.json",
                 "platform/supervisor/stop.json"}
    _check(checks, "51", "backup excludes transient PID/lock/runtime state",
           not (backed_up & transient)
           and "platform/projects.json" in backed_up)
    verify = hardening_module.verify_backup(backup_dir)
    _check(checks, "51", "backup manifest verifies every digest",
           verify["status"] == "OK")
    fixture_target = str(Path(root) / "restored-fixture")
    restored = hardening_module.restore(backup_dir, fixture_target)
    projects = project_registry.reconstruct_projects(fixture_target)
    _check(checks, "52", "restore reconstructs canonical state",
           restored["file_count"] == manifest["file_count"]
           and projects["reconstructed"]
           and fixture["project_id"] in projects["project_ids"])
    stale = hardening_module.stale_lock_report(fixture_root)
    _check(checks, "53", "stale-lock detection is read-only and explicit",
           stale["stale"] is False and stale["reason"] == "NO_LOCK")
    corrupt_root = str(Path(root) / "corrupt")
    corrupt_project = project_registry.create_project(
        corrupt_root, name="Corrupt", workspace=str(Path(corrupt_root) / "ws"))
    corrupt_path = project_registry.project_path(
        corrupt_root, corrupt_project.project_id)
    corrupt_path.write_text("{not-json", encoding="utf-8")
    corruption = hardening_module.detect_corruption(corrupt_root)
    _check(checks, "54", "partial corruption is detected",
           not corruption["clean"] and corruption["malformed"])
    migration_root = str(Path(root) / "migration")
    migration_path = (Path(migration_root) / model.PLATFORM_DIR
                      / "legacy.json")
    migration_path.parent.mkdir(parents=True, exist_ok=True)
    migration_path.write_text(json.dumps(
        {"schema_version": 0, "value": 1}), encoding="utf-8")
    migration = hardening_module.migrate_platform_documents(migration_root)
    migrated = json.loads(migration_path.read_text(encoding="utf-8"))
    _check(checks, "55", "schema migration upgrades a legacy document",
           migration["migrated"]
           and migrated["schema_version"] == model.SCHEMA_VERSION)
    reconstruction = hardening_module.reconstruct_all(fixture_target)
    _check(checks, "56", "restored inbox/project/mission state is consistent",
           reconstruction["projects"]["projects"] >= 1
           and reconstruction["inbox"]["records"] >= 1
           and reconstruction["queue"]["entries"] >= 1)


def _case_dogfood(root: str, checks: list[AcceptanceCheck]) -> None:
    from trajectory_os.platform import dogfood

    payload = dogfood.run_platform_dogfood(
        str(Path(root) / "dogfood"), write_evidence=True)
    _check(checks, "57", "complete persistent multi-project dogfood passes",
           payload["status"] == "PASS"
           and payload["real_evidence"]["projects"] >= 3
           and payload["fixture_evidence"]["projects"] >= 3,
           status=payload["status"])


# --- matrix -------------------------------------------------------------------

_CASES = (
    ("1", "Project create"),
    ("2", "Project read"),
    ("3", "Project update metadata"),
    ("4", "Project archive"),
    ("5", "Project -> mission linkage"),
    ("6", "Project reconstruction after restart"),
    ("7", "Daemon single-owner"),
    ("8", "Duplicate daemon blocked"),
    ("9", "Daemon crash detection"),
    ("10", "Daemon restart"),
    ("11", "Daemon terminal independence"),
    ("12", "Queue priority"),
    ("13", "Dependency blocking"),
    ("14", "Dependency unblocking"),
    ("15", "Bounded concurrency"),
    ("16", "GPU/resource admission"),
    ("17", "Remote-provider admission"),
    ("18", "Deterministic scheduler reason"),
    ("19", "Starvation resistance"),
    ("20", "No duplicate execution after restart"),
    ("21", "Queued pause/resume"),
    ("22", "Active cancel"),
    ("23", "Requeue"),
    ("24", "GO COMMIT inbox generation"),
    ("25", "GO MERGE inbox generation"),
    ("26", "Notification deduplication"),
    ("27", "Notification acknowledge"),
    ("28", "Notification resolution"),
    ("29", "notify-send unavailable degradation"),
    ("30", "API observation read-only"),
    ("31", "Dashboard/canonical projection parity"),
    ("32", "Mutation isolation"),
    ("33", "Localhost-only default"),
    ("34", "Contradictory state fails closed"),
    ("35", "LifeOS note creation"),
    ("36", "LifeOS idempotent update"),
    ("37", "LifeOS provenance"),
    ("38", "Vault unavailable degradation"),
    ("39", "No secret leakage"),
    ("40", "Reference reviewer telemetry"),
    ("41", "dev3090 telemetry"),
    ("42", "Remote implementation telemetry"),
    ("43", "Unavailable telemetry semantics"),
    ("44", "Explicit routing fallback provenance"),
    ("45", "No unsupported superiority claim"),
    ("46", "Daemon trust boundary"),
    ("47", "Scheduler trust boundary"),
    ("48", "GO COMMIT remains human gated"),
    ("49", "Exact-head CI remains required"),
    ("50", "GO MERGE remains human gated"),
    ("51", "Backup excludes transient state"),
    ("52", "Restore canonical state"),
    ("53", "Stale lock handling"),
    ("54", "Corruption detection"),
    ("55", "Schema migration/reconstruction"),
    ("56", "Restored inbox/project/mission consistency"),
    ("57", "Complete persistent multi-project dogfood"),
)


def run_acceptance(root: str | Path, *,
                   generated_at: str | None = None,
                   ) -> PlatformAcceptanceReport:
    """Run the deterministic M048–M055 acceptance matrix."""
    root_str = str(root)
    Path(root_str).mkdir(parents=True, exist_ok=True)
    checks: list[AcceptanceCheck] = []

    _case_project_registry(root_str, checks)
    _case_supervisor(root_str, checks)
    project_id = _seed_projects(root_str)
    _case_queue(root_str, project_id, checks)
    _case_inbox(root_str, checks)
    _case_api(root_str, project_id, checks)
    _case_lifeos(root_str, checks)
    _case_efficiency(root_str, checks)
    _case_trust(root_str, checks)
    _case_hardening(root_str, checks)
    _case_dogfood(root_str, checks)

    cases = tuple(
        AcceptanceCase(
            case=code, title=title,
            ok=all(check.ok for check in checks if check.case == code),
            checks=tuple(check for check in checks if check.case == code))
        for code, title in _CASES)
    return PlatformAcceptanceReport(
        version=ACCEPTANCE_VERSION, root=root_str,
        generated_at=generated_at or _now(), cases=cases)


def _now() -> str:
    from trajectory_os.operator._util import utc_now

    return utc_now()


__all__ = [
    "ACCEPTANCE_VERSION",
    "PlatformAcceptanceReport",
    "run_acceptance",
]
