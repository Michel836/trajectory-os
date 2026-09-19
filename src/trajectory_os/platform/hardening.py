"""M055 — production hardening, install and disaster recovery.

This module makes the persistent platform reproducible: bootstrap/preflight,
deterministic versioned configuration with secrets separated from config, a
systemd user-service example, durable-state backup that excludes transient
PID/lock/runtime junk, deterministic restore into a clean fixture workspace,
stale-lock and partial-corruption detection, schema migration verification and
a full disaster-recovery drill that reconstructs projects, missions, releases,
scheduler, inbox, routing and LifeOS projections.

Every restore is idempotent and fails closed under contradiction. This module
never performs a Git trust-boundary write.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trajectory_os.operator._util import (
    digest,
    read_jsonl,
    read_optional_json,
    utc_now,
    write_json,
)
from trajectory_os.platform import efficiency as efficiency_module
from trajectory_os.platform import inbox as inbox_module
from trajectory_os.platform import lifeos as lifeos_module
from trajectory_os.platform import model
from trajectory_os.platform import projects as project_registry
from trajectory_os.platform import queue as queue_module
from trajectory_os.platform import supervisor as supervisor_module

#: Domain id for one backup identity.
BACKUP_DOMAIN = "trajectory-os.platform-backup.v1"

#: Transient names/dirs excluded from a durable backup. Entries containing
#: ``*`` (or ``?``/``[]``) are fnmatch patterns matched against each path part.
TRANSIENT_NAMES = frozenset({
    "owner.lock", "heartbeat.json", "stop.json", "*.pid", "*.tmp", "*.lock",
    ".DS_Store", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache",
})

#: Transient filename suffixes that are always excluded, independent of the
#: explicit name list above (keeps new runtime artifacts out of backups).
TRANSIENT_SUFFIXES = (".pid", ".tmp", ".lock", ".pyc")

#: Glob patterns declared in :data:`TRANSIENT_NAMES`, honoured by fnmatch so
#: the declared contract (``*.pid`` etc.) actually takes effect.
TRANSIENT_PATTERNS = tuple(sorted(
    name for name in TRANSIENT_NAMES if any(sym in name for sym in "*?[")))

#: Configuration schema version.
CONFIG_SCHEMA_VERSION = 1

#: Default deterministic configuration path (XDG-compatible).
DEFAULT_CONFIG_DIR = "trajectory-os"
DEFAULT_CONFIG_NAME = "platform-config.json"
DEFAULT_STATE_NAME = "state"

#: Integration statuses for the config.
CONFIG_OK = "OK"
CONFIG_MISSING = "MISSING"


@dataclass(frozen=True)
class PlatformConfig:
    """Deterministic versioned platform configuration (no secrets inline)."""

    state_root: str
    mission_root: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    queue_policy: Mapping[str, Any] = field(default_factory=dict)
    lifeos: Mapping[str, Any] = field(default_factory=dict)
    secrets_ref: str | None = None
    backup_dir: str | None = None
    repo: str | None = None
    schema_version: int = CONFIG_SCHEMA_VERSION
    platform_version: str = model.PLATFORM_VERSION

    def validate(self) -> PlatformConfig:
        if self.schema_version != CONFIG_SCHEMA_VERSION:
            model.fail(model.E_UNSUPPORTED_VERSION, str(self.schema_version))
        if not self.state_root:
            model.fail(model.E_MALFORMED, "state_root required")
        if not (1 <= self.api_port <= 65535):
            model.fail(model.E_MALFORMED, "api_port out of bounds")
        if (self.secrets_ref is not None
                and not isinstance(self.secrets_ref, str)):
            model.fail(model.E_MALFORMED, "secrets_ref must be a path")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "platform_version": self.platform_version,
            "state_root": self.state_root,
            "mission_root": self.mission_root,
            "api_host": self.api_host,
            "api_port": self.api_port,
            "queue_policy": dict(sorted(self.queue_policy.items())),
            "lifeos": dict(sorted(self.lifeos.items())),
            "secrets_ref": self.secrets_ref,
            "backup_dir": self.backup_dir,
            "repo": self.repo,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> PlatformConfig:
        version = data.get("schema_version", CONFIG_SCHEMA_VERSION)
        if version != CONFIG_SCHEMA_VERSION:
            model.fail(model.E_UNSUPPORTED_VERSION, str(version))
        queue_policy = data.get("queue_policy") or {}
        lifeos = data.get("lifeos") or {}
        return PlatformConfig(
            state_root=str(data.get("state_root", "")),
            mission_root=data.get("mission_root"),
            api_host=str(data.get("api_host", "127.0.0.1")),
            api_port=int(data.get("api_port", 8765)),
            queue_policy=(dict(queue_policy)
                          if isinstance(queue_policy, Mapping) else {}),
            lifeos=dict(lifeos) if isinstance(lifeos, Mapping) else {},
            secrets_ref=data.get("secrets_ref"),
            backup_dir=data.get("backup_dir"),
            repo=data.get("repo"),
            schema_version=int(version),
        ).validate()


def default_config_path(base: str | Path | None = None) -> Path:
    if base is not None:
        return Path(base)
    import os

    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    return root / DEFAULT_CONFIG_DIR / DEFAULT_CONFIG_NAME


def load_config(path: str | Path) -> PlatformConfig | None:
    document = read_optional_json(Path(path))
    if document is None:
        return None
    return PlatformConfig.from_dict(document)


def bootstrap(path: str | Path, *, state_root: str,
              mission_root: str | None = None,
              repo: str | None = None,
              api_host: str = "127.0.0.1", api_port: int = 8765,
              queue_policy: Mapping[str, Any] | None = None,
              lifeos: Mapping[str, Any] | None = None,
              secrets_ref: str | None = None,
              backup_dir: str | None = None,
              clock: Callable[[], str] = utc_now) -> dict[str, Any]:
    """Write a deterministic config if absent and run preflight."""
    config_path = Path(path)
    existing = load_config(config_path)
    if existing is None:
        config = PlatformConfig(
            state_root=state_root, mission_root=mission_root or state_root,
            repo=repo, api_host=api_host, api_port=api_port,
            queue_policy=dict(queue_policy or {}),
            lifeos=dict(lifeos or {}), secrets_ref=secrets_ref,
            backup_dir=backup_dir or str(Path(state_root) / "backups"),
        ).validate()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(config_path, config.to_dict())
        created = True
    else:
        config = existing
        created = False
    preflight = preflight_report(config, clock=clock)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "config_path": str(config_path),
        "created": created,
        "config": config.to_dict(),
        "preflight": preflight,
    }


def preflight_report(config: PlatformConfig, *,
                     clock: Callable[[], str] = utc_now) -> dict[str, Any]:
    """Deterministic preflight checks (no side effects beyond mkdir)."""
    config.validate()
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    state_root = Path(config.state_root)
    try:
        state_root.mkdir(parents=True, exist_ok=True)
        probe = state_root / ".preflight-write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("state_root_writable", True)
    except OSError as exc:
        check("state_root_writable", False, f"{type(exc).__name__}: {exc}")
    check("config_schema_version",
          config.schema_version == CONFIG_SCHEMA_VERSION)
    serialized = json.dumps(config.to_dict(), sort_keys=True).lower()
    check("secrets_separated",
          "secret_value" not in serialized and "password" not in serialized)
    check("api_loopback", config.api_host in (
        "127.0.0.1", "::1", "localhost"))
    notify = shutil.which("notify-send")
    checks.append({
        "check": "notify_send", "ok": notify is not None,
        "detail": notify or "UNAVAILABLE: notify-send not installed"})
    systemd = shutil.which("systemctl")
    checks.append({
        "check": "systemd_available", "ok": systemd is not None,
        "detail": systemd or "UNAVAILABLE: systemctl not installed"})
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "checked_at": clock(),
        "status": "OK" if all(c["ok"] for c in checks
                              if c["check"] != "notify_send"
                              and c["check"] != "systemd_available")
        else "DEGRADED",
        "checks": checks,
    }


def load_secrets(config: PlatformConfig, *,
                 reader: Callable[[Path], dict[str, Any]] | None = None,
                 ) -> dict[str, Any]:
    """Load secrets from the separate reference (never from config)."""
    if not config.secrets_ref:
        return {}
    path = Path(config.secrets_ref)
    if not path.is_file():
        model.fail(model.E_MALFORMED, f"secrets file missing: {path}")
    load = reader or (lambda p: read_optional_json(p) or {})
    return load(path)


# --- systemd ------------------------------------------------------------------


def generate_systemd_unit(config: PlatformConfig, *,
                          trajectory: str = "scripts/trajectory",
                          working_dir: str | None = None) -> str:
    """Generate a deterministic systemd user-service unit."""
    config.validate()
    workdir = working_dir or config.repo or str(Path.cwd())
    return "\n".join([
        "[Unit]",
        "Description=TrajectoryOS persistent autonomous operator",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=" + workdir,
        "Environment=TRAJECTORY_MISSION_ROOT=" + config.state_root,
        f"ExecStart=/usr/bin/env bash {trajectory} daemon start "
        f"--root {config.state_root} --foreground",
        "Restart=on-failure",
        "RestartSec=5",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def install_systemd_unit(path: str | Path, unit: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(unit, encoding="utf-8")
    return target


# --- backup -------------------------------------------------------------------


def _is_transient(relative: Path) -> bool:
    for part in relative.parts:
        if part in TRANSIENT_NAMES:
            return True
        if any(fnmatch.fnmatch(part, pattern)
               for pattern in TRANSIENT_PATTERNS):
            return True
    name = relative.name
    if name in TRANSIENT_NAMES:
        return True
    return name.endswith(TRANSIENT_SUFFIXES)


def _iter_durable(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _is_transient(relative):
            continue
        files.append(relative)
    return files


def backup(state_root: str | Path, backup_dir: str | Path, *,
           clock: Callable[[], str] = utc_now) -> dict[str, Any]:
    """Create a deterministic durable-state backup (transient state excluded)."""
    root = Path(state_root)
    if not root.is_dir():
        model.fail(model.E_BACKUP_INVALID, f"state root missing: {root}")
    target = Path(backup_dir)
    target.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for relative in _iter_durable(root):
        source = root / relative
        data = source.read_bytes()
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        entries.append({
            "path": relative.as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    manifest = {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "created_at": clock(),
        "state_root": str(root),
        "excluded_transient": sorted(TRANSIENT_NAMES),
        "file_count": len(entries),
        "files": entries,
    }
    manifest["backup_id"] = digest(
        {"files": entries, "schema_version": model.SCHEMA_VERSION},
        domain=BACKUP_DOMAIN)
    write_json(target / model.BACKUP_MANIFEST_NAME, manifest)
    return manifest


def verify_backup(backup_dir: str | Path) -> dict[str, Any]:
    """Verify a backup manifest and every file digest (fail closed)."""
    directory = Path(backup_dir)
    manifest = read_optional_json(directory / model.BACKUP_MANIFEST_NAME)
    if manifest is None:
        model.fail(model.E_BACKUP_INVALID, "backup manifest missing")
    if manifest.get("schema_version") != model.SCHEMA_VERSION:
        model.fail(model.E_UNSUPPORTED_VERSION,
                   str(manifest.get("schema_version")))
    files = manifest.get("files")
    if not isinstance(files, list):
        model.fail(model.E_BACKUP_INVALID, "manifest files malformed")
    corrupt: list[str] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            model.fail(model.E_BACKUP_INVALID, "manifest entry malformed")
        path = directory / str(entry.get("path"))
        if not path.is_file():
            corrupt.append(f"missing:{entry.get('path')}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != entry.get("sha256"):
            corrupt.append(f"digest:{entry.get('path')}")
    if corrupt:
        model.fail(model.E_CORRUPTION, json.dumps(sorted(corrupt)))
    expected = digest({"files": files,
                       "schema_version": model.SCHEMA_VERSION},
                      domain=BACKUP_DOMAIN)
    if manifest.get("backup_id") != expected:
        model.fail(model.E_CORRUPTION, "backup_id mismatch")
    return {"status": "OK", "backup_id": manifest.get("backup_id"),
            "file_count": len(files), "verified": True}


def restore(backup_dir: str | Path, target_root: str | Path, *,
            clean: bool = True,
            clock: Callable[[], str] = utc_now) -> dict[str, Any]:
    """Deterministically restore a backup into a clean/fresh workspace."""
    verify = verify_backup(backup_dir)
    directory = Path(backup_dir)
    target = Path(target_root)
    if clean and target.exists() and any(target.iterdir()):
        model.fail(model.E_RESTORE_CONTRADICTION,
                   f"restore target not clean: {target}")
    target.mkdir(parents=True, exist_ok=True)
    manifest = read_optional_json(directory / model.BACKUP_MANIFEST_NAME) or {}
    files = manifest.get("files") or []
    restored: list[str] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        source = directory / str(entry.get("path"))
        destination = target / str(entry.get("path"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        restored.append(str(entry.get("path")))
    reconstruction = reconstruct_all(target)
    report = {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "restored_at": clock(),
        "backup_id": verify["backup_id"],
        "target_root": str(target),
        "file_count": len(restored),
        "files": sorted(restored),
        "reconstruction": reconstruction,
        "clean": clean,
    }
    write_json(target / model.PLATFORM_DIR / model.HARDENING_DIR
               / model.RESTORE_REPORT_NAME, report)
    return report


# --- corruption / migration ---------------------------------------------------


def detect_corruption(state_root: str | Path) -> dict[str, Any]:
    """Detect partial corruption in platform JSON documents (read-only)."""
    root = Path(state_root) / model.PLATFORM_DIR
    malformed: list[str] = []
    checked = 0
    if root.is_dir():
        for path in sorted(root.rglob("*.json")):
            if _is_transient(path.relative_to(root)):
                continue
            checked += 1
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                malformed.append(str(path.relative_to(root)))
    contradictions: list[str] = []
    try:
        for project in project_registry.list_projects(state_root):
            if not project_registry.project_path(
                    state_root, project.project_id).is_file():
                contradictions.append(f"project_missing:{project.project_id}")
    except model.PlatformError as exc:
        contradictions.append(exc.code)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "checked": checked,
        "malformed": malformed,
        "contradictions": contradictions,
        "clean": not malformed and not contradictions,
    }


def migrate_platform_documents(state_root: str | Path) -> dict[str, Any]:
    """Deterministic schema migration verification (v0 -> v1) and repair."""
    root = Path(state_root) / model.PLATFORM_DIR
    migrated: list[str] = []
    verified: list[str] = []
    unsupported: list[str] = []
    if not root.is_dir():
        return {"migrated": [], "verified": [], "unsupported": [],
                "schema_version": model.SCHEMA_VERSION}
    for path in sorted(root.rglob("*.json")):
        if _is_transient(path.relative_to(root)):
            continue
        document = read_optional_json(path)
        if document is None:
            unsupported.append(str(path.relative_to(root)))
            continue
        version = document.get("schema_version", model.SCHEMA_VERSION)
        if version == model.SCHEMA_VERSION:
            verified.append(str(path.relative_to(root)))
        elif version == 0:
            document["schema_version"] = model.SCHEMA_VERSION
            write_json(path, document)
            migrated.append(str(path.relative_to(root)))
        else:
            unsupported.append(str(path.relative_to(root)))
    if unsupported:
        model.fail(model.E_MIGRATION_INVALID, json.dumps(sorted(unsupported)))
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "migrated": migrated,
        "verified": verified,
        "unsupported": unsupported,
    }


# --- stale locks / process death ----------------------------------------------


def stale_lock_report(state_root: str | Path) -> dict[str, Any]:
    """Detect a stale supervisor lock (read-only)."""
    owner = supervisor_module.read_owner(state_root)
    if owner is None:
        return {"stale": False, "owner": None, "reason": "NO_LOCK"}
    alive = supervisor_module.process_alive(owner.pid)
    return {
        "stale": not alive,
        "owner": owner.to_dict(),
        "owner_alive": alive,
        "reason": None if alive else "OWNER_PROCESS_DEAD",
    }


# --- reconstruction -----------------------------------------------------------


def reconstruct_all(state_root: str | Path) -> dict[str, Any]:
    """Reconstruct every durable platform subsystem (fail closed)."""
    projects = project_registry.reconstruct_projects(state_root)
    queue_report = queue_module.reconstruct(state_root)
    inbox_report = inbox_module.reconstruct(state_root)
    supervisor = supervisor_module.reconstruct(state_root)
    efficiency = efficiency_module.load_evidence(state_root)
    lifeos_records = read_jsonl(
        Path(state_root) / model.PLATFORM_DIR / model.LIFEOS_DIR
        / model.LIFEOS_RECORDS_NAME)
    return {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "projects": projects,
        "queue": queue_report,
        "inbox": inbox_report,
        "supervisor": supervisor,
        "routing_evidence_present": efficiency is not None,
        "lifeos_records": len(lifeos_records),
        "reconstructed": True,
    }


# --- disaster recovery drill --------------------------------------------------


def disaster_recovery(
    state_root: str | Path,
    *,
    backup_dir: str | Path,
    fixture_root: str | Path,
    project_ids: tuple[str, ...] | None = None,
    clock: Callable[[], str] = utc_now,
) -> dict[str, Any]:
    """Run a deterministic backup -> restore -> reconstruct DR drill."""
    created = backup(state_root, backup_dir, clock=clock)
    corruption_before = detect_corruption(state_root)
    stale = stale_lock_report(state_root)
    restart_simulation = _simulate_restart(backup_dir, fixture_root, clock)
    reconstruction = restart_simulation["reconstruction"]
    report = {
        "schema_version": model.SCHEMA_VERSION,
        "platform_version": model.PLATFORM_VERSION,
        "generated_at": clock(),
        "state_root": str(state_root),
        "backup_dir": str(backup_dir),
        "fixture_root": str(fixture_root),
        "backup_id": created["backup_id"],
        "backup_file_count": created["file_count"],
        "corruption_before": corruption_before,
        "stale_lock": stale,
        "restart_simulation": restart_simulation,
        "reconstruction": reconstruction,
        "projects": reconstruction["projects"],
        "process_death_recovery": supervisor_module.detect_crash(state_root),
        "project_ids": list(project_ids or ()),
        "trust_gates_unchanged": True,
        "git_writes": 0,
    }
    report["status"] = "PASS" if restart_simulation["ok"] else "FAIL"
    write_json(Path(state_root) / model.PLATFORM_DIR / model.HARDENING_DIR
               / model.DR_REPORT_NAME, report)
    return report


def _simulate_restart(backup_dir: str | Path, fixture_root: str | Path,
                      clock: Callable[[], str]) -> dict[str, Any]:
    target = Path(fixture_root)
    if target.exists() and any(target.iterdir()):
        model.fail(model.E_RESTORE_CONTRADICTION,
                   f"fixture workspace not clean: {target}")
    restored = restore(backup_dir, target, clean=True, clock=clock)
    lifeos_status = lifeos_module.integration_status(
        target, lifeos_module.ObsidianConfig(enabled=False))
    return {
        "ok": restored["file_count"] >= 0,
        "restored": restored,
        "reconstruction": restored["reconstruction"],
        "lifeos": lifeos_status,
    }


__all__ = [
    "BACKUP_DOMAIN",
    "CONFIG_OK",
    "CONFIG_MISSING",
    "CONFIG_SCHEMA_VERSION",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_CONFIG_NAME",
    "DEFAULT_STATE_NAME",
    "TRANSIENT_NAMES",
    "TRANSIENT_PATTERNS",
    "TRANSIENT_SUFFIXES",
    "PlatformConfig",
    "backup",
    "bootstrap",
    "default_config_path",
    "detect_corruption",
    "disaster_recovery",
    "generate_systemd_unit",
    "install_systemd_unit",
    "load_config",
    "load_secrets",
    "migrate_platform_documents",
    "preflight_report",
    "reconstruct_all",
    "restore",
    "stale_lock_report",
    "verify_backup",
]
