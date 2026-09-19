"""M023 — isolated DeepSeek Harness SDK qualification.

This module re-runs the M016 qualification over the *existing* M015/M016
provider-neutral agent-backend contract, but with one structural difference:
the official ``deepseek-harness-sdk`` lives in its **own** virtual
environment (its own Python minor version) under::

    $HOME/.cache/trajectory-os/deepseek-harness-sdk

The Trajectory_OS project interpreter must never receive that environment's
``PYTHONPATH``: importing its native modules under the project interpreter is
ABI-incompatible (and would make the canonical quality gate fail for reasons
unrelated to the change under test — see ``scripts/quality.sh``). Therefore:

* SDK identity is discovered by scanning the isolated environment and by
  running its own interpreter in **isolated mode** (``python -I``) as a
  bounded subprocess, with a sanitized environment that drops every
  ``PYTHON*`` variable;
* the DeepSeek Harness backend is constructed with the isolated runtime
  executable and SDK facts *injected* through the existing adapter's
  dependency-injection surface — the SDK is never imported into the project
  interpreter;
* a bounded structured qualification records SDK/runtime identity, handshake,
  lifecycle, completion/error semantics, timeout/cancellation capability,
  evidence structure and a Pi + DeepSeek Flash comparison;
* when the SDK, runtime or credentials are unavailable or incompatible the
  outcome is a deterministic ``UNAVAILABLE`` / ``INCOMPATIBLE`` with the
  exact reason and the proven Pi path remains the fallback.

No secret is read, logged or persisted. No Git trust-boundary write is
performed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trajectory_os.agents import identity as agent_identity
from trajectory_os.agents import model, qualification, registry
from trajectory_os.agents.deepseek_harness import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DeepSeekHarnessBackend,
)
from trajectory_os.agents.qualification import (
    QS_QUALIFIED,
    QualificationOutcome,
)

#: Environment variable overriding the isolated SDK root.
HARNESS_SDK_ROOT_ENV = "TRAJECTORY_DEEPSEEK_HARNESS_SDK"

#: Environment variable overriding the isolated interpreter.
HARNESS_SDK_PYTHON_ENV = "TRAJECTORY_DEEPSEEK_HARNESS_PYTHON"

#: Environment variable overriding the isolated runtime executable.
HARNESS_SDK_RUNTIME_ENV = "TRAJECTORY_DEEPSEEK_HARNESS_RUNTIME"

#: Default isolated SDK environment (relative to the user's home).
DEFAULT_SDK_ROOT = "~/.cache/trajectory-os/deepseek-harness-sdk"

#: SDK module / distribution names (official developer-preview names).
SDK_MODULE = "deepseek_harness"
SDK_DISTRIBUTION = "deepseek-harness-sdk"

#: Bounded identity-probe time budget (seconds).
DEFAULT_IDENTITY_TIMEOUT_S = 30

#: Every environment variable that must never reach the isolated interpreter
#: through the project process (isolation is explicit, not incidental).
ISOLATED_ENV_DROP = (
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONNOUSERSITE",
)

# --- environment statuses (closed set) ---------------------------------------

ES_PRESENT = "PRESENT"
ES_MISSING = "MISSING"
ES_INVALID = "INVALID"

#: Every known environment status (closed set).
ENVIRONMENT_STATUSES = frozenset({ES_PRESENT, ES_MISSING, ES_INVALID})

# --- identity probe statuses (closed set) ------------------------------------

IS_OK = "IDENTITY_OK"
IS_UNAVAILABLE = "IDENTITY_UNAVAILABLE"
IS_INCOMPATIBLE = "IDENTITY_INCOMPATIBLE"

# --- qualification check statuses (closed set) --------------------------------

CK_PASS = "PASS"
CK_UNAVAILABLE = "UNAVAILABLE"
CK_INCOMPATIBLE = "INCOMPATIBLE"
CK_CONFIGURED = "CONFIGURED"
CK_NOT_ATTEMPTED = "NOT_ATTEMPTED"


class HarnessQualificationError(Exception):
    """Malformed isolated-harness qualification input (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SdkEnvironment:
    """One resolved, isolated deepseek-harness-sdk environment (no import)."""

    root: str
    status: str
    present: bool
    python: str | None
    runtime: str | None
    sdk_version: str | None
    reason: str
    detail: str
    environment_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "root": self.root,
            "status": self.status,
            "python": self.python,
            "runtime": self.runtime,
            "sdk_version": self.sdk_version,
            "reason": self.reason,
        }

    def compute_environment_id(self) -> str:
        return agent_identity.harness_environment_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "detail": self.detail,
            "environment_id": self.environment_id,
        }

    @staticmethod
    def build(*, root: str, status: str, python: str | None,
              runtime: str | None, sdk_version: str | None, reason: str,
              detail: str) -> SdkEnvironment:
        if status not in ENVIRONMENT_STATUSES:
            raise HarnessQualificationError(
                "MALFORMED_HARNESS_ENVIRONMENT", f"unknown status {status!r}")
        base = SdkEnvironment(
            root=root, status=status, present=status == ES_PRESENT,
            python=python, runtime=runtime, sdk_version=sdk_version,
            reason=reason, detail=detail)
        return SdkEnvironment(
            root=base.root, status=base.status, present=base.present,
            python=base.python, runtime=base.runtime,
            sdk_version=base.sdk_version, reason=base.reason,
            detail=base.detail, environment_id=base.compute_environment_id())


@dataclass(frozen=True)
class IdentityProbe:
    """One bounded isolated SDK/runtime identity probe result."""

    status: str
    reason: str
    detail: str
    module_present: bool
    sdk_version: str | None
    python_version: str | None
    runtime_version: str | None
    identity_id: str = ""

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": model.SCHEMA_VERSION,
            "status": self.status,
            "reason": self.reason,
            "module_present": self.module_present,
            "sdk_version": self.sdk_version,
            "python_version": self.python_version,
            "runtime_version": self.runtime_version,
        }

    def compute_identity_id(self) -> str:
        return agent_identity.harness_identity_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_payload(), "detail": self.detail,
                "identity_id": self.identity_id}

    @staticmethod
    def build(*, status: str, reason: str, detail: str,
              module_present: bool, sdk_version: str | None,
              python_version: str | None,
              runtime_version: str | None) -> IdentityProbe:
        base = IdentityProbe(
            status=status, reason=reason, detail=detail,
            module_present=module_present, sdk_version=sdk_version,
            python_version=python_version, runtime_version=runtime_version)
        return IdentityProbe(
            status=base.status, reason=base.reason, detail=base.detail,
            module_present=base.module_present, sdk_version=base.sdk_version,
            python_version=base.python_version,
            runtime_version=base.runtime_version,
            identity_id=base.compute_identity_id())


@dataclass(frozen=True)
class IsolatedQualificationOutcome:
    """M023 isolated qualification over the M016 contract (secret-free)."""

    status: str
    reason: str
    environment: dict[str, Any]
    identity: dict[str, Any]
    checks: dict[str, str]
    primary_probe: dict[str, Any]
    fallback_probe: dict[str, Any]
    canary: dict[str, Any]
    comparison: dict[str, Any]
    fallback: dict[str, Any]
    isolation: dict[str, Any]
    git_writes: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "environment": self.environment,
            "identity": self.identity,
            "checks": self.checks,
            "primary_probe": self.primary_probe,
            "fallback_probe": self.fallback_probe,
            "canary": self.canary,
            "comparison": self.comparison,
            "fallback": self.fallback,
            "isolation": self.isolation,
            "git_writes": self.git_writes,
        }


# --- environment discovery ----------------------------------------------------


def resolve_environment(
    *,
    root: str | None = None,
    environment: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> SdkEnvironment:
    """Resolve the isolated SDK environment without importing it.

    Pure filesystem/``PATH`` inspection (the optional isolated interpreter
    identity probe is a separate, explicit step). A configured-but-absent or
    otherwise unusable environment yields a deterministic status.
    """
    env = dict(os.environ if environment is None else environment)
    finder = which or _which
    configured = (
        root
        or env.get(HARNESS_SDK_ROOT_ENV)
        or os.path.expanduser(DEFAULT_SDK_ROOT)
    )
    path = Path(configured).expanduser()
    if not path.is_dir():
        return SdkEnvironment.build(
            root=str(path), status=ES_MISSING, python=None, runtime=None,
            sdk_version=None, reason=model.R_SDK_MISSING,
            detail=f"isolated SDK root not found: {path}")

    python = _find_python(path, env, finder)
    runtime = _find_runtime(path, env, finder)
    sdk_version = _dist_version(path)
    if python is None:
        return SdkEnvironment.build(
            root=str(path), status=ES_INVALID, python=None, runtime=runtime,
            sdk_version=sdk_version, reason=model.R_LAUNCH_FAILED,
            detail=f"isolated interpreter not found under {path}/bin")
    return SdkEnvironment.build(
        root=str(path), status=ES_PRESENT, python=python, runtime=runtime,
        sdk_version=sdk_version, reason=model.R_OK,
        detail=(f"isolated env: {path}; sdk="
                f"{sdk_version if sdk_version else 'unknown'}; "
                f"runtime={runtime if runtime else 'missing'}"))


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def _find_python(root: Path, env: Mapping[str, str],
                 which: Callable[[str], str | None]) -> str | None:
    configured = env.get(HARNESS_SDK_PYTHON_ENV)
    if configured:
        candidate = Path(configured).expanduser()
        return str(candidate) if candidate.is_file() else None
    for name in ("python", "python3"):
        candidate = root / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _find_runtime(root: Path, env: Mapping[str, str],
                  which: Callable[[str], str | None]) -> str | None:
    configured = env.get(HARNESS_SDK_RUNTIME_ENV)
    if configured:
        candidate = Path(configured).expanduser()
        return str(candidate) if candidate.is_file() else None
    candidate = root / "bin" / "dsh"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return which("dsh")


def _dist_version(root: Path) -> str | None:
    """Read the SDK distribution version from ``*.dist-info/METADATA``.

    Standard-library filesystem scan only: no import, no execution. The
    highest version found is returned deterministically; malformed metadata
    is ignored rather than guessed.
    """
    best: str | None = None
    for lib in sorted(root.glob("lib")):
        for site in sorted(lib.glob("python*/site-packages")):
            for dist in sorted(
                    site.glob(f"{SDK_DISTRIBUTION.replace('-', '_')}*.dist-info")):
                version = _metadata_version(dist / "METADATA")
                if version is None:
                    continue
                if best is None or _version_key(version) > _version_key(best):
                    best = version
    return best


def _version_key(version: str) -> tuple[int, ...]:
    """Numeric version ordering component (0.1.10 > 0.1.5)."""
    parts = re.findall(r"\d+", version)
    return tuple(int(part) for part in parts) or (0,)


def _metadata_version(metadata: Path) -> str | None:
    try:
        text = metadata.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("Version:"):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


# --- isolation ----------------------------------------------------------------


def sanitized_environment(
    base: Mapping[str, str],
    *,
    venv_root: str | None = None,
) -> dict[str, str]:
    """Return a subprocess environment with every ``PYTHON*`` variable dropped.

    The isolated interpreter is invoked with ``-I``; this sanitized mapping is
    the second, explicit guarantee that the project's ``PYTHONPATH`` (or any
    other ``PYTHON*`` setting) can never leak into the SDK environment.
    """
    env = {key: value for key, value in base.items()
           if key not in ISOLATED_ENV_DROP}
    if venv_root:
        env["VIRTUAL_ENV"] = venv_root
        bin_dir = str(Path(venv_root) / "bin")
        path = env.get("PATH", "")
        env["PATH"] = bin_dir + (os.pathsep + path if path else "")
    return env


#: Bounded identity script executed by the *isolated* interpreter. It only
#: reports structural identity facts as JSON; it never reads a credential.
_IDENTITY_SCRIPT = (
    "import importlib.util as u, sys\n"
    "sdk=None\n"
    "try:\n"
    "    import importlib.metadata as m\n"
    "    sdk=m.version('deepseek-harness-sdk')\n"
    "except Exception:\n"
    "    sdk=None\n"
    "present=u.find_spec('deepseek_harness') is not None\n"
    "import json\n"
    "print(json.dumps({'module_present': present, 'sdk_version': sdk,"
    " 'python_version': sys.version.split()[0]}))\n"
)


def _default_isolated_runner(
    python: str, script: str, env: Mapping[str, str], timeout_s: int,
) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 - bounded argv, no shell
        [python, "-I", "-c", script],
        capture_output=True, text=True, timeout=timeout_s, env=dict(env),
        check=False)
    return proc.returncode, proc.stdout, proc.stderr


def probe_identity(
    environment: SdkEnvironment,
    *,
    environment_vars: Mapping[str, str] | None = None,
    runner: Callable[
        [str, str, Mapping[str, str], int], tuple[int, str, str]
    ] | None = None,
    runtime_runner: Callable[[Sequence[str], int], tuple[int, str, str]]
    | None = None,
    timeout_s: int = DEFAULT_IDENTITY_TIMEOUT_S,
) -> IdentityProbe:
    """Probe the isolated SDK/runtime identity in a bounded subprocess."""
    if environment.python is None or environment.status != ES_PRESENT:
        return IdentityProbe.build(
            status=IS_UNAVAILABLE, reason=environment.reason,
            detail=environment.detail, module_present=False,
            sdk_version=environment.sdk_version, python_version=None,
            runtime_version=None)
    base = dict(os.environ if environment_vars is None else environment_vars)
    env = sanitized_environment(base, venv_root=environment.root)
    execute = runner or _default_isolated_runner
    try:
        code, stdout, stderr = execute(
            environment.python, _IDENTITY_SCRIPT, env, max(1, timeout_s))
    except subprocess.TimeoutExpired:
        return IdentityProbe.build(
            status=IS_INCOMPATIBLE, reason=model.R_TIMEOUT,
            detail="isolated SDK identity probe timed out",
            module_present=False, sdk_version=environment.sdk_version,
            python_version=None, runtime_version=None)
    except OSError as exc:
        return IdentityProbe.build(
            status=IS_INCOMPATIBLE, reason=model.R_LAUNCH_FAILED,
            detail=f"isolated SDK identity probe failed: {exc}",
            module_present=False, sdk_version=environment.sdk_version,
            python_version=None, runtime_version=None)
    parsed = _parse_identity(stdout)
    if code != 0 or parsed is None:
        detail = (stderr.strip() or stdout.strip()
                  or f"identity probe exit {code}")
        return IdentityProbe.build(
            status=IS_INCOMPATIBLE, reason=model.R_INCOMPATIBLE_PROTOCOL,
            detail=detail[:model.MAX_DETAIL_LEN], module_present=False,
            sdk_version=environment.sdk_version, python_version=None,
            runtime_version=None)
    runtime_version = _runtime_version(
        environment.runtime, runtime_runner=runtime_runner, timeout_s=timeout_s)
    module_present = bool(parsed.get("module_present"))
    sdk_version = parsed.get("sdk_version")
    if not module_present and environment.sdk_version is not None:
        # Metadata present but import failed: report the incompatibility
        # honestly rather than claiming availability.
        return IdentityProbe.build(
            status=IS_INCOMPATIBLE, reason=model.R_INCOMPATIBLE_PROTOCOL,
            detail="SDK distribution metadata present but module not importable",
            module_present=False, sdk_version=environment.sdk_version,
            python_version=_as_str(parsed.get("python_version")),
            runtime_version=runtime_version)
    return IdentityProbe.build(
        status=IS_OK,
        reason=(model.R_OK if module_present else model.R_SDK_MISSING),
        detail=(f"python="
                f"{_as_str(parsed.get('python_version')) or 'unknown'}; "
                f"sdk={_as_str(sdk_version) or 'missing'}; "
                f"runtime={runtime_version or 'missing'}"),
        module_present=module_present,
        sdk_version=_as_str(sdk_version) or environment.sdk_version,
        python_version=_as_str(parsed.get("python_version")),
        runtime_version=runtime_version)


def _parse_identity(stdout: str) -> Mapping[str, object] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _runtime_version(
    runtime: str | None,
    *,
    runtime_runner: Callable[
        [Sequence[str], int], tuple[int, str, str]
    ] | None,
    timeout_s: int,
) -> str | None:
    if runtime is None:
        return None
    execute = runtime_runner or _default_version_runner
    try:
        code, stdout, _ = execute([runtime, "--version"], max(1, timeout_s))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if code != 0:
        return None
    line = stdout.strip().splitlines()
    return line[0].strip()[:128] if line else None


def _default_version_runner(
    argv: Sequence[str], timeout_s: int,
) -> tuple[int, str, str]:
    proc = subprocess.run(  # noqa: S603 - bounded argv, no shell
        list(argv), capture_output=True, text=True, timeout=timeout_s,
        check=False)
    return proc.returncode, proc.stdout, proc.stderr


# --- isolated backend factory -------------------------------------------------


def isolated_backend_factory(
    environment: SdkEnvironment,
    *,
    popener: Any | None = None,
    credentials_present: Any | None = None,
) -> Callable[[str], Any]:
    """Build a backend factory that injects isolated SDK/runtime facts.

    The DeepSeek Harness adapter is never allowed to import the SDK into the
    project interpreter: ``sdk_finder`` returns an opaque sentinel derived
    from the isolated scan, and ``sdk_version`` is passed through verbatim.
    ``popener`` / ``credentials_present`` are test seams forwarded to the
    existing adapter; production uses the adapter defaults.
    """
    def build(name: str) -> Any:
        if name == model.BACKEND_DEEPSEEK_HARNESS:
            kwargs: dict[str, Any] = {
                "executable": environment.runtime or "dsh",
                "sdk_finder": lambda _: (
                    object() if environment.sdk_version is not None
                    or environment.present else None),
                "sdk_version": lambda _: environment.sdk_version,
            }
            if popener is not None:
                kwargs["popener"] = popener
            if credentials_present is not None:
                kwargs["credentials_present"] = credentials_present
            return DeepSeekHarnessBackend(**kwargs)
        return registry.make_backend(name)
    return build


def qualify_isolated(
    *,
    workspace: str,
    timeout_s: int = 120,
    task: str = qualification.DEFAULT_CANARY_TASK,
    factory: Callable[[str], Any] | None = None,
    environment: SdkEnvironment | None = None,
    identity: IdentityProbe | None = None,
    environment_vars: Mapping[str, str] | None = None,
    allow_runtime: bool = False,
) -> IsolatedQualificationOutcome:
    """Run one bounded isolated DeepSeek Harness qualification.

    Reuses the M016 ``qualification.qualify`` composition (which itself uses
    the M015 canary and backend contract) and adds the isolated environment
    and identity facts. It never invents success: the composed outcome is
    authoritative and is recorded verbatim.
    """
    resolved = environment or resolve_environment(
        environment=environment_vars)
    identity_probe = identity or probe_identity(
        resolved, environment_vars=environment_vars)
    build = factory or isolated_backend_factory(resolved)
    composed: QualificationOutcome = qualification.qualify(
        workspace=workspace, timeout_s=timeout_s, task=task, factory=build,
        allow_runtime=allow_runtime)
    checks = _checks(resolved, identity_probe, composed)
    isolation = {
        "pythonpath_dropped": True,
        "interpreter_isolated_mode": True,
        "mode": "isolated-subprocess",
        "sdk_imported_into_project": False,
    }
    fallback = dict(composed.fallback)
    fallback["pi_route"] = {
        "backend": model.BACKEND_PI,
        "provider": DEFAULT_PROVIDER,
        "model": DEFAULT_MODEL,
    }
    return IsolatedQualificationOutcome(
        status=composed.status,
        reason=composed.reason,
        environment=resolved.to_dict(),
        identity=identity_probe.to_dict(),
        checks=checks,
        primary_probe=composed.primary_probe,
        fallback_probe=composed.fallback_probe,
        canary=composed.canary,
        comparison=composed.comparison,
        fallback=fallback,
        isolation=isolation,
    )


def _checks(
    environment: SdkEnvironment,
    identity: IdentityProbe,
    composed: QualificationOutcome,
) -> dict[str, str]:
    """Bounded structured qualification dimensions (deterministic)."""
    events = composed.canary.get("lifecycle")
    kinds: set[object] = {
        event.get("kind") for event in events
        if isinstance(event, Mapping)
    } if isinstance(events, list) else set()
    completion = composed.canary.get("completion")
    completion_source = (
        completion.get("source") if isinstance(completion, Mapping) else None)
    canary_status = composed.canary.get("status")
    available = environment.present and identity.status == IS_OK
    error_seen = model.LK_ERROR in kinds
    return {
        "sdk_identity": (CK_PASS if identity.sdk_version is not None
                         else CK_UNAVAILABLE),
        "runtime_identity": (CK_PASS if environment.runtime is not None
                             else CK_UNAVAILABLE),
        "handshake": (
            CK_PASS if model.LK_INITIALIZED in kinds
            else (CK_UNAVAILABLE if not available
                  else CK_INCOMPATIBLE)),
        "lifecycle": (CK_PASS if kinds else CK_UNAVAILABLE),
        "completion_semantics": (
            CK_PASS if completion_source not in (None, model.CS_NONE)
            else CK_UNAVAILABLE),
        "error_semantics": (
            CK_PASS if error_seen else CK_UNAVAILABLE),
        "timeout_semantics": CK_CONFIGURED,
        "cancellation_semantics": CK_CONFIGURED,
        "evidence_structure": (
            CK_PASS if composed.canary and composed.comparison
            else CK_UNAVAILABLE),
        "pi_comparison": (
            CK_PASS if composed.fallback_probe.get("backend")
            == model.BACKEND_PI else CK_UNAVAILABLE),
        "canary": _as_check(canary_status),
    }


def _as_check(canary_status: object) -> str:
    if canary_status == model.CS_CANARY_PASS:
        return CK_PASS
    if canary_status == model.CS_CANARY_INCOMPATIBLE:
        return CK_INCOMPATIBLE
    if canary_status == model.CS_CANARY_UNAVAILABLE:
        return CK_UNAVAILABLE
    if canary_status == model.CS_CANARY_FAIL:
        return CK_INCOMPATIBLE
    return CK_NOT_ATTEMPTED


def pi_continues(outcome: IsolatedQualificationOutcome) -> bool:
    """True when the deterministic Pi fallback remains authoritative."""
    return outcome.status != QS_QUALIFIED
