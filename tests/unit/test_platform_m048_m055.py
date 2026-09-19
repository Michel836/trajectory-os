"""M048–M055 — persistent autonomous operator platform unit tests."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from trajectory_os.operator import cli as operator_cli
from trajectory_os.operator._util import utc_now
from trajectory_os.platform import api as api_module
from trajectory_os.platform import cli as platform_cli
from trajectory_os.platform import dogfood as dogfood_module
from trajectory_os.platform import efficiency as efficiency_module
from trajectory_os.platform import hardening as hardening_module
from trajectory_os.platform import inbox as inbox_module
from trajectory_os.platform import lifeos as lifeos_module
from trajectory_os.platform import model
from trajectory_os.platform import projects as project_registry
from trajectory_os.platform import queue as queue_module
from trajectory_os.platform import supervisor as supervisor_module

# --- projects -----------------------------------------------------------------


def test_project_registry_lifecycle(tmp_path: Path) -> None:
    project = project_registry.create_project(
        str(tmp_path), name="Alpha", workspace=str(tmp_path / "ws"),
        description="alpha")
    assert project.project_id == project_registry.project_id_for("Alpha")
    assert project_registry.load_project(
        str(tmp_path), project.project_id).name == "Alpha"
    updated = project_registry.update_project(
        str(tmp_path), project.project_id, description="beta",
        default_policy_profile="safe")
    assert updated.description == "beta"
    assert updated.default_policy_profile == "safe"
    archived = project_registry.archive_project(
        str(tmp_path), project.project_id)
    assert archived.lifecycle == project_registry.PL_ARCHIVED
    with pytest.raises(model.PlatformError):
        project_registry.update_project(
            str(tmp_path), project.project_id, description="nope")


def test_project_environment_mutation_is_forbidden(tmp_path: Path) -> None:
    with pytest.raises(model.PlatformError) as exc:
        project_registry.create_project(
            str(tmp_path), name="Env", workspace=str(tmp_path / "ws"),
            source=project_registry.SOURCE_ENVIRONMENT)
    assert exc.value.code == model.E_PROJECT_ENVIRONMENT


def test_project_mission_linkage_and_reconstruction(tmp_path: Path) -> None:
    project = project_registry.create_project(
        str(tmp_path), name="Link", workspace=str(tmp_path / "ws"))
    project_registry.add_objective(str(tmp_path), project.project_id,
                                   objective_id="o1", title="O1")
    project_registry.link_mission(str(tmp_path), project.project_id,
                                  objective_id="o1", mission_id="m1")
    linkage = project_registry.project_linkage(str(tmp_path),
                                               project.project_id)
    assert linkage["objectives"][0]["missions"][0]["mission_id"] == "m1"
    report = project_registry.reconstruct_projects(str(tmp_path))
    assert report["reconstructed"]
    assert project.project_id in report["project_ids"]
    status = project_registry.project_status(str(tmp_path),
                                             project.project_id)
    assert status["missions"] == 1


# --- supervisor ---------------------------------------------------------------


def test_supervisor_single_owner_and_crash(tmp_path: Path) -> None:
    root = str(tmp_path)
    live = os.getpid()
    owner = supervisor_module.build_owner(root, pid=live, token="a")
    outcome, _ = supervisor_module.claim_ownership(root, owner)
    assert outcome == supervisor_module.CLAIM_FRESH
    denied, previous = supervisor_module.claim_ownership(
        root, supervisor_module.build_owner(root, pid=live + 1000, token="b"))
    assert denied == supervisor_module.CLAIM_DENIED
    assert previous is not None and previous.pid == live


def test_supervisor_crash_detection_and_recovery(tmp_path: Path) -> None:
    root = str(tmp_path)
    dead = supervisor_module.build_owner(root, pid=999999999, token="dead")
    from trajectory_os.operator._util import write_json

    write_json(supervisor_module.owner_path(root), dead.to_dict())
    state = supervisor_module.SupervisorState.initial(
        started_at=utc_now(), owner=dead, project_ids=())
    supervisor_module.save_state(root, state)
    assert supervisor_module.detect_crash(root)["crash_detected"] is True
    outcome, _ = supervisor_module.claim_ownership(
        root, supervisor_module.build_owner(root, pid=os.getpid(),
                                            token="recover"))
    assert outcome == supervisor_module.CLAIM_RECOVERED


def test_supervisor_detached_relative_root(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch,
                                           ) -> None:
    monkeypatch.chdir(tmp_path)
    pid = platform_cli.spawn_supervisor("rel-root", max_cycles=1)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        state = supervisor_module.load_state("rel-root")
        if state is not None and state.cycles >= 1:
            break
        time.sleep(0.05)
    assert pid > 0
    assert supervisor_module.load_state("rel-root") is not None


def test_supervisor_bounded_loop_and_stop(tmp_path: Path) -> None:
    root = str(tmp_path)
    seen: list[int] = []

    def cycle(root_: str | Path, index: int) -> dict[str, object]:
        seen.append(index)
        return {"reason": "TEST", "complete": False}

    report = supervisor_module.run_supervisor(
        root, max_cycles=3, cycle_fn=cycle)
    assert report.cycles == 3
    assert seen == [1, 2, 3]
    assert supervisor_module.read_heartbeat(root) is not None
    assert supervisor_module.read_owner(root) is None
    supervisor_module.request_stop(root)
    assert supervisor_module.stop_requested(root)
    supervisor_module.clear_stop(root)
    assert not supervisor_module.stop_requested(root)


def test_supervisor_claim_window_is_not_a_false_crash(tmp_path: Path) -> None:
    """Death between owner claim and first state save is not a false crash."""
    root = str(tmp_path)
    from trajectory_os.operator._util import write_json

    dead = supervisor_module.build_owner(root, pid=999999999, token="window")
    write_json(supervisor_module.owner_path(root), dead.to_dict())
    # No state.json was persisted: crash detection requires persisted RUNNING.
    crash = supervisor_module.detect_crash(root)
    assert crash["status"] == "ABSENT"
    assert crash["crash_detected"] is False


def test_supervisor_dispatches_selected_missions_when_wired(
        tmp_path: Path) -> None:
    """A wired dispatcher receives admitted work and a durable decision."""
    root = str(tmp_path)
    project = project_registry.create_project(
        root, name="Dispatch", workspace=str(tmp_path / "ws"))
    queue_module.enqueue(root, mission_id="m1", project_id=project.project_id)
    dispatched: list[str] = []
    report = supervisor_module.run_supervisor(
        root, max_cycles=1, dispatch_fn=dispatched.append)
    assert report.cycles == 1
    assert dispatched == ["m1"]
    entry = queue_module.load_queue(root).by_id("m1")
    assert entry is not None and entry.state == queue_module.Q_ACTIVE
    assert queue_module.decisions(root)


# --- queue --------------------------------------------------------------------


def _seed(tmp_path: Path) -> str:
    project = project_registry.create_project(
        str(tmp_path), name="Portfolio", workspace=str(tmp_path / "ws"))
    return project.project_id


def test_queue_priority_and_determinism(tmp_path: Path) -> None:
    project = _seed(tmp_path)
    queue_module.enqueue(str(tmp_path), mission_id="low",
                         project_id=project, priority=1)
    queue_module.enqueue(str(tmp_path), mission_id="high",
                         project_id=project, priority=9)
    decision = queue_module.schedule_once(
        str(tmp_path), policy=queue_module.QueuePolicy(max_concurrent=1))
    assert decision.selected == ("high",)
    assert decision.reason == queue_module.R_SELECTED
    assert queue_module.decisions(str(tmp_path))


def test_queue_dependency_and_resources(tmp_path: Path) -> None:
    project = _seed(tmp_path)
    queue_module.enqueue(str(tmp_path), mission_id="dep",
                         project_id=project)
    queue_module.enqueue(str(tmp_path), mission_id="dependent",
                         project_id=project, dependencies=("dep",))
    blocked = queue_module.schedule_once(
        str(tmp_path), policy=queue_module.QueuePolicy(max_concurrent=1))
    assert "dependent" not in blocked.selected
    queue_module.mark_state(str(tmp_path), "dep", queue_module.Q_DONE)
    unblocked = queue_module.schedule_once(
        str(tmp_path), policy=queue_module.QueuePolicy(max_concurrent=1))
    assert "dependent" in unblocked.selected


def test_queue_gpu_vram_admission(tmp_path: Path) -> None:
    project = _seed(tmp_path)
    for name in ("a", "b"):
        queue_module.enqueue(
            str(tmp_path), mission_id=name, project_id=project,
            resources=queue_module.ResourceRequest(
                cpu_slots=1, gpu_slots=1, gpu_mem_bytes=6 << 30,
                locality=queue_module.LOCAL))
    decision = queue_module.schedule_once(
        str(tmp_path), policy=queue_module.QueuePolicy(
            max_concurrent=4, gpu_slots=2, gpu_mem_bytes=8 << 30))
    assert len(decision.selected) == 1
    assert any(a.reason == queue_module.R_VRAM_LIMIT
               for a in decision.admissions)


def test_queue_pause_resume_cancel_requeue(tmp_path: Path) -> None:
    project = _seed(tmp_path)
    queue_module.enqueue(str(tmp_path), mission_id="m", project_id=project)
    assert queue_module.pause(str(tmp_path), "m").state == queue_module.Q_PAUSED
    assert queue_module.resume(str(tmp_path), "m").state == queue_module.Q_QUEUED
    queue_module.mark_state(str(tmp_path), "m", queue_module.Q_ACTIVE)
    assert queue_module.cancel(str(tmp_path), "m").state == (
        queue_module.Q_CANCELLED)
    assert queue_module.requeue(str(tmp_path), "m").state == queue_module.Q_QUEUED


# --- inbox --------------------------------------------------------------------


def test_inbox_dedup_ack_resolve(tmp_path: Path) -> None:
    root = str(tmp_path)
    notification = inbox_module.Notification.build(
        type=inbox_module.N_GO_COMMIT_REQUIRED, mission_id="m1",
        key="patch", title="GO COMMIT", detail="pending")
    assert inbox_module.record_notification(root, notification) is True
    assert inbox_module.record_notification(root, notification) is False
    records = inbox_module.list_notifications(root)
    assert len(records) == 1
    acknowledged = inbox_module.acknowledge(root, notification.notification_id)
    assert acknowledged.state == inbox_module.NS_ACKNOWLEDGED
    resolved = inbox_module.resolve(root, notification.notification_id)
    assert resolved.state == inbox_module.NS_RESOLVED


def test_inbox_notify_send_unavailable(tmp_path: Path) -> None:
    result = inbox_module.notify_send_adapter(
        notification_id_value="x", title="t", body="b",
        which=lambda name: None)
    assert result["status"] == "UNAVAILABLE"
    assert result["reason"]


# --- api ----------------------------------------------------------------------


def test_api_read_only_and_mutation_auth(tmp_path: Path) -> None:
    api = api_module.LocalApi(str(tmp_path))
    assert api.handle("GET", "/api/health").status == 200
    assert api.handle("GET", "/api/projection").status == 200
    refused = api.handle("POST", "/api/inbox/refresh")
    assert refused.status == 403
    authorized = api.handle("POST", "/api/inbox/refresh", {
        "Authorization": f"Bearer {api.token}",
        "X-CSRF-Token": api.csrf_token,
    })
    assert authorized.status == 200
    assert api.handle("POST", "/api/inbox/refresh", {
        "Authorization": f"Bearer {api.token}",
        "X-CSRF-Token": "wrong",
    }).status == 403


def test_api_bind_is_loopback_only(tmp_path: Path) -> None:
    with pytest.raises(model.PlatformError):
        api_module.check_bind("0.0.0.0")
    api_module.check_bind("127.0.0.1")


def test_api_serve_threaded_http_surface(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    server = api_module.serve(str(tmp_path), port=0, background=True)
    try:
        host, port = server.server_address[0], server.server_address[1]
        with urllib.request.urlopen(
                f"http://{host}:{port}/api/health", timeout=5) as response:
            status = response.status
            payload = json.loads(response.read().decode("utf-8"))
        assert status == 200
        assert payload["status"] == "OK"
        # POST without authorization is refused by the same handler surface.
        request = urllib.request.Request(
            f"http://{host}:{port}/api/inbox/refresh", data=b"{}",
            method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_api_local_instance_serialises_requests(tmp_path: Path) -> None:
    api = api_module.LocalApi(str(tmp_path))
    assert api._lock is not None  # serialised shared-instance contract
    assert api.handle("GET", "/api/health").status == 200


# --- lifeos -------------------------------------------------------------------


def test_lifeos_idempotent_and_degraded(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    project = project_registry.create_project(
        str(root), name="Life", workspace=str(root / "ws"))
    vault = tmp_path / "vault"
    vault.mkdir()
    config = lifeos_module.ObsidianConfig(vault=str(vault))
    first = lifeos_module.sync_project(str(root), config, project.project_id)
    assert first.status == lifeos_module.LS_SYNCED
    second = lifeos_module.sync_project(str(root), config, project.project_id)
    assert second.status == lifeos_module.LS_SYNCED
    assert second.written == ()
    degraded = lifeos_module.sync_all(
        str(root), lifeos_module.ObsidianConfig(vault=str(tmp_path / "nope")))
    assert degraded.status == lifeos_module.LS_DEGRADED
    assert degraded.reason


def test_lifeos_target_inside_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    project = project_registry.create_project(
        str(root), name="Life2", workspace=str(root / "ws"))
    inside = root / "vault"
    inside.mkdir()
    report = lifeos_module.sync_all(
        str(root), lifeos_module.ObsidianConfig(vault=str(inside)))
    assert report.status == lifeos_module.LS_DEGRADED
    assert report.reason == "VAULT_INSIDE_CANONICAL_ROOT"
    del project


def test_lifeos_refuses_secret_shaped_content(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    project = project_registry.create_project(
        str(root), name="Leak", workspace=str(root / "ws"),
        description="password: hunter2")
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(model.PlatformError) as exc:
        lifeos_module.sync_project(
            str(root), lifeos_module.ObsidianConfig(vault=str(vault)),
            project.project_id)
    assert exc.value.code == model.E_LIFEOS_SECRET


def test_lifeos_sync_all_isolates_a_secret_project(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    project_registry.create_project(
        str(root), name="Clean", workspace=str(root / "ws-clean"))
    project_registry.create_project(
        str(root), name="Leak2", workspace=str(root / "ws-leak"),
        description="api_key: abc123")
    vault = tmp_path / "vault"
    vault.mkdir()
    report = lifeos_module.sync_all(
        str(root), lifeos_module.ObsidianConfig(vault=str(vault)))
    assert report.status == lifeos_module.LS_PARTIAL
    assert report.written


# --- efficiency ---------------------------------------------------------------


def _trial(model_name: str, duration: int, provenance: str = "LOCAL",
           ) -> dict[str, object]:
    return {
        "benchmark_run_id": "run", "backend": "pi", "provider": "ollama",
        "model": model_name, "locality": "local",
        "workload_class": "SMALL_TARGETED_REPAIR",
        "telemetry": {
            "total_duration_ms": duration, "ttft_ms": 10,
            "prompt_tokens": 1, "completion_tokens": 2,
            "cache_read_tokens": 0, "prompt_tps": 1.0, "generation_tps": 1.0,
            "gpu_utilization_pct": None, "vram_bytes": None, "cost_usd": None,
            "retries": 0, "repairs": 0, "protocol_errors": 0,
            "sources": {"total_duration_ms": provenance,
                        "gpu_utilization_pct": "UNAVAILABLE"},
            "unavailable": {"gpu_utilization_pct": "not exposed"},
        },
        "validation": {"passed": True}, "review": {"outcome": "VALID_PASS"},
        "patch": {"insertions": 1, "deletions": 0, "files_changed": 1},
        "agent": {},
    }


def test_efficiency_provenance_and_recommendation(tmp_path: Path) -> None:
    routes = efficiency_module.build_route_evidence([
        _trial("qwen3.8:27b-q4_K_M", 1000),
        _trial("qwen3.8-dev3090", 800),
    ])
    assert len(routes) == 2
    unavailable = routes[0].metrics[efficiency_module.M_GPU_UTILIZATION]
    assert unavailable.value is None
    assert unavailable.provenance == efficiency_module.SRC_UNAVAILABLE
    assert unavailable.reason
    recommendation = efficiency_module.recommend(routes)
    assert recommendation.supported is True
    assert recommendation.preferred_route is not None


def test_efficiency_provenance_is_deterministic() -> None:
    multi = efficiency_module._aggregate_metric(
        [(10.0, "LOCAL", ""), (20.0, "PROVIDER", "")])
    assert multi.provenance == efficiency_module.SRC_DERIVED
    single = efficiency_module._aggregate_metric(
        [(10.0, "LOCAL", ""), (20.0, "LOCAL", "")])
    assert single.provenance == "LOCAL"


def test_efficiency_no_unsupported_claim(tmp_path: Path) -> None:
    routes = efficiency_module.build_route_evidence(
        [_trial("qwen3.8:27b-q4_K_M", 1000)])
    recommendation = efficiency_module.recommend(routes)
    assert recommendation.supported is False
    assert recommendation.preferred_route is None
    routes = efficiency_module.build_route_evidence(
        [_trial("qwen3.8:27b-q4_K_M", 1000)])
    recommendation = efficiency_module.recommend(routes)
    assert recommendation.supported is False
    assert recommendation.preferred_route is None


def test_efficiency_persist_is_advisory(tmp_path: Path) -> None:
    evidence = efficiency_module.build_evidence(
        [_trial("qwen3.8:27b-q4_K_M", 1000)])
    efficiency_module.persist_evidence(str(tmp_path), evidence)
    document = efficiency_module.load_evidence(str(tmp_path))
    assert document is not None
    assert document["authorization_state"] == (
        efficiency_module.AUTHORIZATION_ADVISORY)
    assert document["policy_mutated"] is False
    assert document["final_reviewer"] == "qwen3.8:27b-q4_K_M"


# --- hardening ----------------------------------------------------------------


def test_hardening_backup_restore_and_corruption(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    project_registry.create_project(
        str(root), name="Hard", workspace=str(root / "ws"))
    supervisor_module.write_heartbeat(
        str(root), supervisor_module.build_owner(str(root)), cycle=1,
        at=utc_now())
    manifest = hardening_module.backup(str(root), str(tmp_path / "backup"))
    paths = {entry["path"] for entry in manifest["files"]}
    assert "platform/supervisor/heartbeat.json" not in paths
    assert hardening_module.verify_backup(str(tmp_path / "backup"))["status"] \
        == "OK"
    restored = hardening_module.restore(
        str(tmp_path / "backup"), str(tmp_path / "fixture"))
    assert restored["file_count"] == manifest["file_count"]
    corrupt = root / model.PLATFORM_DIR / "projects" / "bad.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{", encoding="utf-8")
    assert hardening_module.detect_corruption(str(root))["clean"] is False


def test_hardening_excludes_glob_transient_files(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "worker.pid").write_text(str(os.getpid()), encoding="utf-8")
    (root / "scratch.tmp").write_text("x", encoding="utf-8")
    (root / "keep.json").write_text("{}\n", encoding="utf-8")
    manifest = hardening_module.backup(str(root), str(tmp_path / "backup"))
    paths = {entry["path"] for entry in manifest["files"]}
    assert hardening_module.TRANSIENT_PATTERNS
    assert "worker.pid" not in paths
    assert "scratch.tmp" not in paths
    assert "keep.json" in paths


def test_hardening_schema_migration(tmp_path: Path) -> None:
    root = tmp_path / "state"
    legacy = root / model.PLATFORM_DIR / "legacy.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"schema_version": 0}), encoding="utf-8")
    report = hardening_module.migrate_platform_documents(str(root))
    assert report["migrated"]
    assert json.loads(legacy.read_text(encoding="utf-8"))["schema_version"] == 1


def test_hardening_config_secrets_separation(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    payload = hardening_module.bootstrap(
        str(config_path), state_root=str(tmp_path / "state"))
    assert payload["created"] is True
    assert payload["preflight"]["status"] in ("OK", "DEGRADED")
    config = hardening_module.load_config(str(config_path))
    assert config is not None
    assert "password" not in json.dumps(config.to_dict()).lower()
    secrets = tmp_path / "secrets.json"
    secrets.write_text(json.dumps({"api_key": "s3cr3t"}), encoding="utf-8")
    config_with_secret = hardening_module.PlatformConfig(
        state_root=config.state_root, secrets_ref=str(secrets))
    loaded = hardening_module.load_secrets(config_with_secret)
    assert loaded["api_key"] == "s3cr3t"
    assert "s3cr3t" not in json.dumps(config_with_secret.to_dict())


def test_hardening_systemd_unit(tmp_path: Path) -> None:
    config = hardening_module.PlatformConfig(
        state_root=str(tmp_path / "state"))
    unit = hardening_module.generate_systemd_unit(config)
    assert "ExecStart" in unit
    assert "daemon start" in unit
    target = hardening_module.install_systemd_unit(
        str(tmp_path / "unit.service"), unit)
    assert target.is_file()


# --- dogfood ------------------------------------------------------------------


def test_dogfood_objectives_are_structured_strings(tmp_path: Path) -> None:
    """Objectives must never serialize a string character-by-character."""
    assert isinstance(dogfood_module.OBJECTIVES, tuple)
    assert len(dogfood_module.OBJECTIVES) >= 2
    assert all(isinstance(objective, str) and len(objective) > 1
               for objective in dogfood_module.OBJECTIVES)
    assert all(objective.strip() for objective in dogfood_module.OBJECTIVES)
    payload = dogfood_module.run_platform_dogfood(
        tmp_path, write_evidence=False)
    objectives = payload["objectives"]
    assert isinstance(objectives, list)
    assert objectives == list(dogfood_module.OBJECTIVES)
    assert len(objectives) == len(dogfood_module.OBJECTIVES)
    assert all(isinstance(objective, str) and len(objective) > 1
               for objective in objectives)
    assert not any(len(objective) == 1 for objective in objectives)


# --- CLI ----------------------------------------------------------------------


def test_operator_cli_registers_platform_commands(tmp_path: Path) -> None:
    parser = operator_cli.build_parser()
    args = parser.parse_args([
        "project", "create", "--root", str(tmp_path), "--name", "CLI",
        "--workspace", str(tmp_path / "ws")])
    assert args.command == "project"
    assert operator_cli.main([
        "project", "create", "--root", str(tmp_path), "--name", "CLI",
        "--workspace", str(tmp_path / "ws")]) == 0
    assert operator_cli.main([
        "project", "list", "--root", str(tmp_path)]) == 0
    assert operator_cli.main([
        "daemon", "status", "--root", str(tmp_path), "--json"]) == 0
    assert operator_cli.main([
        "inbox", "refresh", "--root", str(tmp_path)]) == 0
