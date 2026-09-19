"""M048–M055 — platform CLI registered on the unified ``scripts/trajectory``.

Observation commands are read-only. Mutations are explicit. Git release
writes remain impossible from this surface: the platform never commits, pushes
or merges.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from trajectory_os.operator._util import utc_now
from trajectory_os.platform import api as api_module
from trajectory_os.platform import efficiency as efficiency_module
from trajectory_os.platform import hardening as hardening_module
from trajectory_os.platform import inbox as inbox_module
from trajectory_os.platform import lifeos as lifeos_module
from trajectory_os.platform import model
from trajectory_os.platform import projects as project_registry
from trajectory_os.platform import queue as queue_module
from trajectory_os.platform import supervisor as supervisor_module

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _root(args: argparse.Namespace) -> str:
    return str(args.root)


def _emit(args: argparse.Namespace, payload: Any, text: str) -> int:
    if getattr(args, "json", False):
        _print_json(payload)
    else:
        print(text)
    return EXIT_OK


# --- projects -----------------------------------------------------------------


def _cmd_project(args: argparse.Namespace) -> int:
    root = _root(args)
    action = args.project_command
    if action == "create":
        project = project_registry.create_project(
            root, name=args.name, description=args.description or "",
            objective_domain=args.objective_domain, workspace=args.workspace,
            repository=args.repository, default_policy_profile=args.policy,
            clock=utc_now)
        return _emit(args, project.to_dict(),
                     f"project {project.project_id} ({project.name})")
    if action == "list":
        projects = project_registry.list_projects(root)
        return _emit(args, {"projects": [p.to_dict() for p in projects]},
                     "\n".join(f"{p.project_id} {p.lifecycle} {p.name}"
                               for p in projects) or "no projects")
    if action == "show":
        return _emit(args, project_registry.load_project(
            root, args.project_id).to_dict(), args.project_id)
    if action == "status":
        return _emit(args, project_registry.project_status(
            root, args.project_id), args.project_id)
    if action == "update":
        project = project_registry.update_project(
            root, args.project_id, name=args.name,
            description=args.description, objective_domain=args.objective_domain,
            workspace=args.workspace, repository=args.repository,
            default_policy_profile=args.policy, clock=utc_now)
        return _emit(args, project.to_dict(), f"updated {project.project_id}")
    if action == "archive":
        project = project_registry.archive_project(
            root, args.project_id, clock=utc_now)
        return _emit(args, project.to_dict(), f"archived {project.project_id}")
    if action == "objective-add":
        project = project_registry.add_objective(
            root, args.project_id, objective_id=args.objective_id,
            title=args.title, description=args.description or "", clock=utc_now)
        return _emit(args, project.to_dict(), "objective added")
    if action == "link":
        project = project_registry.link_mission(
            root, args.project_id, objective_id=args.objective_id,
            mission_id=args.mission_id, clock=utc_now)
        return _emit(args, project.to_dict(), "mission linked")
    if action == "reconstruct":
        return _emit(args, project_registry.reconstruct_projects(root),
                     "projects reconstructed")
    if action == "linkage":
        return _emit(args, project_registry.project_linkage(
            root, args.project_id), args.project_id)
    raise model.PlatformError(model.E_MALFORMED, action)


# --- supervisor ---------------------------------------------------------------


def run_foreground(root: str, *, max_cycles: int,
                   project_ids: tuple[str, ...] | None = None,
                   cycle_interval: float = 0.0) -> dict[str, Any]:
    report = supervisor_module.run_supervisor(
        root, project_ids=project_ids, max_cycles=max_cycles,
        cycle_interval_s=cycle_interval)
    return report.to_dict()


def spawn_supervisor(root: str, *, max_cycles: int,
                     project_ids: tuple[str, ...] = (),
                     cycle_interval: float = 0.0) -> int:
    """Start the supervisor detached in its own session (no terminal)."""
    root_abs = str(Path(root).resolve())
    Path(root_abs).mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m",
               "trajectory_os.platform.supervisor_main",
               "--root", root_abs, "--max-cycles", str(max_cycles)]
    for project_id in project_ids:
        command += ["--project-id", project_id]
    if cycle_interval > 0:
        command += ["--cycle-interval", str(cycle_interval)]
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, env=env,
        cwd=root_abs)
    return int(process.pid)


def _cmd_daemon(args: argparse.Namespace) -> int:
    root = _root(args)
    action = args.daemon_command
    project_ids = tuple(getattr(args, "project_id", None) or ())
    if action == "start":
        if args.foreground:
            payload = run_foreground(root, max_cycles=args.max_cycles,
                                     project_ids=project_ids or None,
                                     cycle_interval=args.cycle_interval)
            return _emit(args, payload,
                         f"supervisor {payload['status']} "
                         f"({payload['cycles']} cycles)")
        pid = spawn_supervisor(root, max_cycles=args.max_cycles,
                               project_ids=project_ids,
                               cycle_interval=args.cycle_interval)
        return _emit(args, {"status": "STARTED", "pid": pid},
                     f"supervisor started (pid {pid})")
    if action == "status":
        return _emit(args, supervisor_module.status_document(root),
                     supervisor_module.status_document(root)["status"] or "-")
    if action == "stop":
        document = supervisor_module.request_stop(root, reason=args.reason,
                                                  clock=utc_now)
        return _emit(args, document, f"stop requested: {document['reason']}")
    if action == "resume":
        if args.foreground:
            payload = run_foreground(root, max_cycles=args.max_cycles,
                                     project_ids=project_ids or None,
                                     cycle_interval=args.cycle_interval)
            return _emit(args, payload, f"supervisor {payload['status']}")
        supervisor_module.clear_stop(root)
        pid = spawn_supervisor(root, max_cycles=args.max_cycles,
                               project_ids=project_ids,
                               cycle_interval=args.cycle_interval)
        return _emit(args, {"status": "RESUMED", "pid": pid},
                     f"supervisor resumed (pid {pid})")
    if action == "reconstruct":
        return _emit(args, supervisor_module.reconstruct(root),
                     "supervisor reconstructed")
    if action == "crash":
        return _emit(args, supervisor_module.detect_crash(root),
                     "crash state read")
    raise model.PlatformError(model.E_MALFORMED, action)


# --- queue --------------------------------------------------------------------


def _resources_from_args(args: argparse.Namespace) -> queue_module.ResourceRequest:
    return queue_module.ResourceRequest(
        cpu_slots=args.cpu, gpu_slots=args.gpu,
        gpu_mem_bytes=args.vram, locality=args.locality,
        provider=args.provider)


def _cmd_queue(args: argparse.Namespace) -> int:
    root = _root(args)
    action = args.queue_command
    if action == "add":
        entry = queue_module.enqueue(
            root, mission_id=args.mission_id, project_id=args.project_id,
            objective_id=args.objective_id, priority=args.priority,
            dependencies=tuple(args.depends_on or ()),
            resources=_resources_from_args(args), backend=args.backend,
            clock=utc_now)
        return _emit(args, entry.to_dict(), f"queued {entry.mission_id}")
    if action == "list":
        state = queue_module.load_queue(root)
        return _emit(args, state.to_dict(),
                     "\n".join(f"{e.mission_id} {e.state} p={e.priority}"
                               for e in state.entries) or "empty queue")
    if action == "schedule":
        decision = queue_module.schedule_once(
            root, backend_available=(
                {"pi": True} if args.backend_available else None))
        return _emit(args, decision.to_dict(),
                     f"{decision.reason}: {list(decision.selected)}")
    if action == "accounting":
        return _emit(args, queue_module.resource_accounting(root),
                     "resource accounting")
    if action in ("pause", "resume", "cancel", "requeue"):
        function = getattr(queue_module, action)
        entry = function(root, args.mission_id, clock=utc_now)
        return _emit(args, entry.to_dict(), f"{action} {entry.mission_id}")
    if action == "reconstruct":
        return _emit(args, queue_module.reconstruct(root), "queue reconstructed")
    raise model.PlatformError(model.E_MALFORMED, action)


# --- inbox --------------------------------------------------------------------


def _cmd_inbox(args: argparse.Namespace) -> int:
    root = _root(args)
    action = args.inbox_command
    if action == "refresh":
        return _emit(args, inbox_module.refresh(root, clock=utc_now),
                     "inbox refreshed")
    if action == "list":
        records = inbox_module.list_notifications(root, state=args.state)
        return _emit(args, {"notifications": [r.to_dict() for r in records]},
                     "\n".join(f"{r.state} {r.type} {r.mission_id}"
                               for r in records) or "empty inbox")
    if action == "gates":
        return _emit(args, {"gates": inbox_module.pending_human_gates(root)},
                     "pending gates")
    if action == "ack":
        record = inbox_module.acknowledge(root, args.notification_id,
                                          clock=utc_now)
        return _emit(args, record.to_dict(), f"acknowledged {record.type}")
    if action == "resolve":
        record = inbox_module.resolve(root, args.notification_id,
                                      clock=utc_now)
        return _emit(args, record.to_dict(), f"resolved {record.type}")
    if action == "notify":
        by_id = {r.notification_id: r
                 for r in inbox_module.list_notifications(root)}
        notify_record = by_id.get(args.notification_id)
        if notify_record is None:
            model.fail(model.E_INBOX_MISSING, args.notification_id)
        result = inbox_module.notify_send_adapter(
            notification_id_value=notify_record.notification_id,
            title=notify_record.title, body=notify_record.detail)
        return _emit(args, result, f"notify-send: {result['status']}")
    raise model.PlatformError(model.E_MALFORMED, action)


# --- API ----------------------------------------------------------------------


def _cmd_api(args: argparse.Namespace) -> int:
    root = _root(args)
    action = args.api_command
    if action == "projection":
        return _emit(args, api_module.build_projection(root),
                     "projection built")
    if action == "dashboard":
        projection = api_module.build_projection(root)
        print(api_module.render_dashboard(projection))
        return EXIT_OK
    if action == "serve":
        server = api_module.serve(root, host=args.host, port=args.port,
                                  token=args.token, background=False)
        address = server.server_address
        print(f"serving on http://{address[0]}:{address[1]}")
        import contextlib

        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
        return EXIT_OK
    raise model.PlatformError(model.E_MALFORMED, action)


# --- LifeOS -------------------------------------------------------------------


def _lifeos_config(args: argparse.Namespace) -> lifeos_module.ObsidianConfig:
    return lifeos_module.ObsidianConfig(vault=args.vault,
                                        project_dir=args.project_dir)


def _cmd_lifeos(args: argparse.Namespace) -> int:
    root = _root(args)
    config = _lifeos_config(args)
    if args.lifeos_command == "sync":
        if args.project_id:
            report = lifeos_module.sync_project(root, config,
                                                args.project_id)
        else:
            report = lifeos_module.sync_all(root, config)
        return _emit(args, report.to_dict(), f"lifeos {report.status}")
    if args.lifeos_command == "status":
        return _emit(args, lifeos_module.integration_status(root, config),
                     "lifeos status")
    raise model.PlatformError(model.E_MALFORMED, args.lifeos_command)


# --- efficiency ---------------------------------------------------------------


def _cmd_routing_evidence(args: argparse.Namespace) -> int:
    root = _root(args)
    trials: list[dict[str, Any]] = []
    if args.trials:
        document = json.loads(Path(args.trials).read_text(encoding="utf-8"))
        trials = document if isinstance(document, list) else document.get(
            "trials", [])
    if args.benchmark_root:
        trials.extend(efficiency_module.load_trial_documents(
            args.benchmark_root))
    evidence = efficiency_module.build_evidence(trials, clock=utc_now)
    efficiency_module.persist_evidence(root, evidence)
    return _emit(args, evidence.to_dict(),
                 f"routing evidence: {evidence.recommendation.reason}")


# --- hardening ----------------------------------------------------------------


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    payload = hardening_module.bootstrap(
        args.config, state_root=args.state_root, repo=args.repo,
        api_host=args.api_host, api_port=args.api_port, clock=utc_now)
    return _emit(args, payload, f"bootstrap {payload['preflight']['status']}")


def _cmd_systemd_unit(args: argparse.Namespace) -> int:
    config = hardening_module.load_config(args.config)
    if config is None:
        model.fail(model.E_MALFORMED, f"config not found: {args.config}")
    unit = hardening_module.generate_systemd_unit(
        config, trajectory=args.trajectory, working_dir=args.working_dir)
    if args.out:
        path = hardening_module.install_systemd_unit(args.out, unit)
        return _emit(args, {"unit": str(path)}, f"unit written to {path}")
    print(unit)
    return EXIT_OK


def _cmd_backup(args: argparse.Namespace) -> int:
    manifest = hardening_module.backup(_root(args), args.backup_dir,
                                       clock=utc_now)
    return _emit(args, manifest,
                 f"backup {manifest['backup_id']} "
                 f"({manifest['file_count']} files)")


def _cmd_restore(args: argparse.Namespace) -> int:
    report = hardening_module.restore(args.backup_dir, args.target,
                                      clock=utc_now)
    return _emit(args, report, f"restored {report['file_count']} files")


def _cmd_dr(args: argparse.Namespace) -> int:
    report = hardening_module.disaster_recovery(
        _root(args), backup_dir=args.backup_dir, fixture_root=args.fixture,
        clock=utc_now)
    return _emit(args, report, f"disaster recovery {report['status']}")


def _cmd_corruption(args: argparse.Namespace) -> int:
    return _emit(args, hardening_module.detect_corruption(_root(args)),
                 "corruption check")


def _cmd_migrate(args: argparse.Namespace) -> int:
    return _emit(args, hardening_module.migrate_platform_documents(_root(args)),
                 "schema migration")


# --- acceptance / dogfood -----------------------------------------------------


def _cmd_platform_acceptance(args: argparse.Namespace) -> int:
    from trajectory_os.platform import acceptance

    report = acceptance.run_acceptance(_root(args))
    if args.out:
        Path(args.out).write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    if args.json:
        _print_json(report.to_dict())
    else:
        print(report.render())
    return EXIT_OK if report.status == "PASS" else EXIT_REJECTED


def _cmd_platform_dogfood(args: argparse.Namespace) -> int:
    from trajectory_os.platform import dogfood

    payload = dogfood.run_platform_dogfood(
        _root(args), repo=args.repo, write_evidence=True)
    if args.json:
        _print_json(payload)
    else:
        print(f"platform dogfood: {payload['status']}")
        print(f"  real    : {payload['real_evidence']['summary']}")
        print(f"  fixture : {payload['fixture_evidence']['summary']}")
    return EXIT_OK if payload["status"] == "PASS" else EXIT_REJECTED


# --- registration -------------------------------------------------------------


def register(sub: Any) -> None:
    p = sub.add_parser("project", help="M048 project registry")
    project = p.add_subparsers(dest="project_command", required=True)
    c = project.add_parser("create")
    c.add_argument("--root", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--description", default="")
    c.add_argument("--objective-domain", dest="objective_domain",
                   default="general")
    c.add_argument("--workspace", required=True)
    c.add_argument("--repository", default=None)
    c.add_argument("--policy", default="release")
    c.add_argument("--json", action="store_true")
    for name in ("list",):
        q = project.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--json", action="store_true")
    for name in ("show", "status", "archive", "linkage", "reconstruct"):
        q = project.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--project-id", dest="project_id", default=None)
        q.add_argument("--json", action="store_true")
    u = project.add_parser("update")
    u.add_argument("--root", required=True)
    u.add_argument("--project-id", dest="project_id", required=True)
    u.add_argument("--name", default=None)
    u.add_argument("--description", default=None)
    u.add_argument("--objective-domain", dest="objective_domain", default=None)
    u.add_argument("--workspace", default=None)
    u.add_argument("--repository", default=None)
    u.add_argument("--policy", default=None)
    u.add_argument("--json", action="store_true")
    o = project.add_parser("objective-add")
    o.add_argument("--root", required=True)
    o.add_argument("--project-id", dest="project_id", required=True)
    o.add_argument("--objective-id", dest="objective_id", required=True)
    o.add_argument("--title", required=True)
    o.add_argument("--description", default="")
    o.add_argument("--json", action="store_true")
    link = project.add_parser("link")
    link.add_argument("--root", required=True)
    link.add_argument("--project-id", dest="project_id", required=True)
    link.add_argument("--objective-id", dest="objective_id", required=True)
    link.add_argument("--mission-id", dest="mission_id", required=True)
    link.add_argument("--json", action="store_true")

    p = sub.add_parser("daemon", help="M049 persistent supervisor")
    daemon = p.add_subparsers(dest="daemon_command", required=True)
    for name in ("start", "resume"):
        q = daemon.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--max-cycles", dest="max_cycles", type=int, default=1)
        q.add_argument("--project-id", dest="project_id", action="append",
                       default=[])
        q.add_argument("--cycle-interval", dest="cycle_interval", type=float,
                       default=0.0)
        q.add_argument("--foreground", action="store_true")
        q.add_argument("--json", action="store_true")
    for name in ("status", "crash", "reconstruct"):
        q = daemon.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--json", action="store_true")
    stop = daemon.add_parser("stop")
    stop.add_argument("--root", required=True)
    stop.add_argument("--reason", default="OPERATOR_STOP")
    stop.add_argument("--json", action="store_true")

    p = sub.add_parser("queue", help="M050 multi-mission queue/scheduler")
    job = p.add_subparsers(dest="queue_command", required=True)
    a = job.add_parser("add")
    a.add_argument("--root", required=True)
    a.add_argument("--mission-id", dest="mission_id", required=True)
    a.add_argument("--project-id", dest="project_id", required=True)
    a.add_argument("--objective-id", dest="objective_id", default=None)
    a.add_argument("--priority", type=int, default=0)
    a.add_argument("--depends-on", dest="depends_on", action="append",
                   default=[])
    a.add_argument("--cpu", type=int, default=1)
    a.add_argument("--gpu", type=int, default=0)
    a.add_argument("--vram", type=int, default=0)
    a.add_argument("--locality", choices=("local", "remote"), default="remote")
    a.add_argument("--provider", default=None)
    a.add_argument("--backend", default=None)
    a.add_argument("--json", action="store_true")
    for name in ("list", "schedule", "accounting", "reconstruct"):
        q = job.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--json", action="store_true")
    schedule = job.choices["schedule"]
    schedule.add_argument("--backend-available", dest="backend_available",
                          action="store_true")
    for name in ("pause", "resume", "cancel", "requeue"):
        q = job.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--mission-id", dest="mission_id", required=True)
        q.add_argument("--json", action="store_true")

    p = sub.add_parser("inbox", help="M051 notifications/human-gate inbox")
    box = p.add_subparsers(dest="inbox_command", required=True)
    for name in ("refresh", "gates"):
        q = box.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--json", action="store_true")
    listing = box.add_parser("list")
    listing.add_argument("--root", required=True)
    listing.add_argument("--state", default=None)
    listing.add_argument("--json", action="store_true")
    for name in ("ack", "resolve", "notify"):
        q = box.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--notification-id", dest="notification_id",
                       required=True)
        q.add_argument("--json", action="store_true")

    p = sub.add_parser("api", help="M052 local API/dashboard")
    local = p.add_subparsers(dest="api_command", required=True)
    for name in ("projection", "dashboard"):
        q = local.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--json", action="store_true")
    server = local.add_parser("serve")
    server.add_argument("--root", required=True)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--token", default=None)

    p = sub.add_parser("lifeos", help="M053 LifeOS/Obsidian projection")
    life = p.add_subparsers(dest="lifeos_command", required=True)
    for name in ("sync", "status"):
        q = life.add_parser(name)
        q.add_argument("--root", required=True)
        q.add_argument("--vault", required=True)
        q.add_argument("--project-dir", dest="project_dir",
                       default="TrajectoryOS")
        q.add_argument("--project-id", dest="project_id", default=None)
        q.add_argument("--json", action="store_true")

    p = sub.add_parser("routing-evidence",
                       help="M054 evidence-based model routing")
    p.add_argument("--root", required=True)
    p.add_argument("--trials", default=None)
    p.add_argument("--benchmark-root", dest="benchmark_root", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("bootstrap", help="M055 bootstrap/preflight")
    p.add_argument("--config", required=True)
    p.add_argument("--state-root", dest="state_root", required=True)
    p.add_argument("--repo", default=None)
    p.add_argument("--api-host", dest="api_host", default="127.0.0.1")
    p.add_argument("--api-port", dest="api_port", type=int, default=8765)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("systemd-unit", help="M055 generate a systemd unit")
    p.add_argument("--config", required=True)
    p.add_argument("--trajectory", default="scripts/trajectory")
    p.add_argument("--working-dir", dest="working_dir", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--json", action="store_true")

    for name in ("backup", "restore", "dr"):
        q = sub.add_parser(name, help=f"M055 {name}")
        q.add_argument("--root", required=True)
        if name == "backup":
            q.add_argument("--backup-dir", dest="backup_dir", required=True)
        elif name == "restore":
            q.add_argument("--backup-dir", dest="backup_dir", required=True)
            q.add_argument("--target", required=True)
        else:
            q.add_argument("--backup-dir", dest="backup_dir", required=True)
            q.add_argument("--fixture", required=True)
        q.add_argument("--json", action="store_true")

    for name in ("corruption", "migrate"):
        q = sub.add_parser(name, help=f"M055 {name}")
        q.add_argument("--root", required=True)
        q.add_argument("--json", action="store_true")

    p = sub.add_parser("platform-acceptance",
                       help="run the M048-M055 acceptance matrix")
    p.add_argument("--root", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("platform-dogfood",
                       help="run the M048-M055 dogfood evidence")
    p.add_argument("--root", required=True)
    p.add_argument("--repo", default=None)
    p.add_argument("--json", action="store_true")


_HANDLERS = {
    "project": _cmd_project,
    "daemon": _cmd_daemon,
    "queue": _cmd_queue,
    "inbox": _cmd_inbox,
    "api": _cmd_api,
    "lifeos": _cmd_lifeos,
    "routing-evidence": _cmd_routing_evidence,
    "bootstrap": _cmd_bootstrap,
    "systemd-unit": _cmd_systemd_unit,
    "backup": _cmd_backup,
    "restore": _cmd_restore,
    "dr": _cmd_dr,
    "corruption": _cmd_corruption,
    "migrate": _cmd_migrate,
    "platform-acceptance": _cmd_platform_acceptance,
    "platform-dogfood": _cmd_platform_dogfood,
}


def is_platform_command(command: str) -> bool:
    return command in _HANDLERS


def dispatch(args: argparse.Namespace) -> int:
    handler = _HANDLERS.get(args.command)
    if handler is None:
        print(f"error: unknown platform command {args.command!r}",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        return int(handler(args))
    except (model.PlatformError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REJECTED


__all__ = [
    "dispatch",
    "is_platform_command",
    "register",
    "run_foreground",
    "spawn_supervisor",
]
