"""V1.91 — Canonical job specification (bounded execution requests).

Every execution request that touches durable multi-run state is represented
by one canonical, strictly validated, deterministically serialized
:class:`JobSpec` object.  The spec declares everything an execution needs:

* the job identity (``job_id``, deterministic),
* the ``execution_class`` (``ad_hoc`` / ``read_only`` / ``mutating``),
* the exact ``command`` (argv list),
* the optional repository root, provenance revision, and source checkout,
* the ``workspace_policy`` (``isolated`` / ``shared_read_only``),
* the isolated final user query (inline content or materialized from a file),
* the bounded retry policy (``max_attempts``),
* the runner identity (``generic`` / ``trajectory-pi``).

Malformed or ambiguous specs are rejected fail-closed — the spec is the single
contract shared by the queue, admission, workspace materialization, launch,
retry, and inspection layers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from trajectory_os.runs import model
from trajectory_os.runs.resources import ResourceRequirement

SPEC_SCHEMA_VERSION = 1

# Canonical job identity: same character discipline as the durable queue
# (filesystem-safe, no path traversal, no whitespace, bounded length).
JOB_ID_SPEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Execution classes.
EXEC_AD_HOC = "ad_hoc"
EXEC_READ_ONLY = "read_only"
EXEC_MUTATING = "mutating"
EXECUTION_CLASSES = frozenset({EXEC_AD_HOC, EXEC_READ_ONLY, EXEC_MUTATING})

# Workspace policies.
WORKSPACE_ISOLATED = "isolated"
WORKSPACE_SHARED_READ_ONLY = "shared_read_only"
WORKSPACE_POLICIES = frozenset({WORKSPACE_ISOLATED, WORKSPACE_SHARED_READ_ONLY})

# Runner identities.
RUNNER_GENERIC = "generic"
RUNNER_TRAJECTORY_PI = "trajectory-pi"
RUNNERS = frozenset({RUNNER_GENERIC, RUNNER_TRAJECTORY_PI})

# V1.94 terminal outcomes (evidence taxonomy: re-exported for consumers).
TERMINAL_DONE = model.TERMINAL_DONE
TERMINAL_FAILED = model.TERMINAL_FAILED
TERMINAL_CRASHED = model.TERMINAL_CRASHED
TERMINAL_CANCELLED = model.TERMINAL_CANCELLED
TERMINAL_CANCEL_PENDING = model.TERMINAL_CANCEL_PENDING
TERMINAL_UNKNOWN = model.TERMINAL_UNKNOWN
TERMINALS = frozenset(
    {
        TERMINAL_DONE,
        TERMINAL_FAILED,
        TERMINAL_CRASHED,
        TERMINAL_CANCELLED,
        TERMINAL_CANCEL_PENDING,
        TERMINAL_UNKNOWN,
    }
)

# Bounded canonical limits (fail-closed beyond them).
MAX_JOB_ID_SPEC_LEN = 128
MAX_COMMAND_PARTS = 64
MAX_COMMAND_PART_LEN = 4096
MAX_REVISION_LEN = 256
MAX_RUNNER_LEN = 64
MAX_QUERY_LEN = 262_144
MAX_MAX_ATTEMPTS = model.MAX_ATTEMPTS_LIMIT  # canonical bounded retry cap
MAX_DEPENDENCIES_ON = model.MAX_DEPENDENCIES  # V2.03: bounded prerequisite list

# Provenance marker files (V1.92).
SOURCE_REVISION_MARKER_REL = ".trajectory/source.json"
RUN_PROVENANCE_MARKER_REL = ".trajectory/run-provenance.json"
WORKSPACE_MANIFEST_REL = ".trajectory/workspace-manifest.json"


class SpecValidationError(Exception):
    """A job spec is malformed, ambiguous, or inconsistent (fail-closed)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _clean(text: str, code: str) -> str:
    if (
        "\x00" in text
        or text != text.strip()
        or any(ord(ch) < 32 and ch not in (" ",) for ch in text)
    ):
        raise SpecValidationError(code, repr(text[:80]))
    return text


def _validated_absolute_path(value: Path, code: str) -> None:
    """Path discipline for canonical provenance paths (fail-closed).

    A repository root or source checkout accepted as canonical provenance
    must be **absolute and unambiguous** before acceptance:

    * absolute (rooted) — relative paths are caller-ambient and would make
      the provenance record dependent on the process cwd;
    * at least one root-relative component (``/`` alone names the root,
      which is never a sensible checkout/repo for a job spec);
    * no ``.`` / ``..`` components — an unnormalized path is ambiguous
      about which directory it denotes;
    * every component within a 255-byte filesystem name limit.

    Rejection is deterministic and uses the caller's predefined code.
    """
    if not value.is_absolute():
        raise SpecValidationError(code, f"must be absolute: {value}")
    parts = list(value.parts[1:])
    if not parts:
        raise SpecValidationError(code, f"must identify a real directory: {value}")
    if "." in parts or ".." in parts:
        raise SpecValidationError(code, f"must be unambiguous (no . / ..): {value}")
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise SpecValidationError(code, f"path component exceeds 255 bytes: {value}")


@dataclass(frozen=True)
class JobSpec:
    """One canonical, immutable execution request."""

    job_id: str
    execution_class: str
    command: tuple[str, ...]
    workspace_policy: str = WORKSPACE_ISOLATED
    runner: str = RUNNER_GENERIC
    max_attempts: int = 2
    schema_version: int = SPEC_SCHEMA_VERSION
    repo_root: Path | None = None
    source_revision: str | None = None
    source_checkout: Path | None = None
    query: str | None = None
    query_source: str | None = None  # provenance: where the query content came from
    # V2.03: simple bounded prerequisite dependencies (fail closed).
    depends_on: tuple[str, ...] = ()
    permit_failed_prereqs: bool = False
    # V2.04: explicit local resource requirements (all optional).
    resources: ResourceRequirement | None = None

    # -- validation (deterministic, strict, fail-closed) -------------------
    def validate(self) -> JobSpec:
        if self.schema_version != SPEC_SCHEMA_VERSION:
            raise SpecValidationError("SPEC_SCHEMA_UNSUPPORTED", str(self.schema_version))
        if (
            not isinstance(self.job_id, str)
            or not (1 <= len(self.job_id) <= MAX_JOB_ID_SPEC_LEN)
            or JOB_ID_SPEC_RE.fullmatch(self.job_id) is None
        ):
            raise SpecValidationError("SPEC_JOB_ID_INVALID", repr(self.job_id)[:100])
        _clean(self.job_id, "SPEC_JOB_ID_INVALID")
        if self.execution_class not in EXECUTION_CLASSES:
            raise SpecValidationError("SPEC_EXEC_CLASS_INVALID", str(self.execution_class))
        if self.workspace_policy not in WORKSPACE_POLICIES:
            raise SpecValidationError("SPEC_WORKSPACE_POLICY_INVALID", str(self.workspace_policy))
        if self.runner not in RUNNERS:
            raise SpecValidationError("SPEC_RUNNER_INVALID", str(self.runner))
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not (1 <= self.max_attempts <= MAX_MAX_ATTEMPTS)
        ):
            raise SpecValidationError("SPEC_MAX_ATTEMPTS_INVALID", str(self.max_attempts))
        # command: strict argv list of clean scalars
        if (
            not isinstance(self.command, tuple)
            or not (1 <= len(self.command) <= MAX_COMMAND_PARTS)
        ):
            raise SpecValidationError("SPEC_COMMAND_INVALID", repr(self.command))
        for part in self.command:
            if not isinstance(part, str) or not (1 <= len(part) <= MAX_COMMAND_PART_LEN):
                raise SpecValidationError("SPEC_COMMAND_PART_INVALID", repr(part)[:100])
            _clean(part, "SPEC_COMMAND_PART_INVALID")
        # provenance / policy coherence rules (fail-closed)
        if self.repo_root is not None and not isinstance(self.repo_root, Path):
            raise SpecValidationError("SPEC_REPO_ROOT_INVALID", repr(self.repo_root))
        if self.source_checkout is not None and not isinstance(self.source_checkout, Path):
            raise SpecValidationError("SPEC_SOURCE_CHECKOUT_INVALID", repr(self.source_checkout))
        # Provenance paths must be absolute and unambiguous before acceptance.
        if self.repo_root is not None:
            _validated_absolute_path(self.repo_root, "SPEC_REPO_ROOT_INVALID")
        if self.source_checkout is not None:
            _validated_absolute_path(self.source_checkout, "SPEC_SOURCE_CHECKOUT_INVALID")
        if (
            self.source_revision is not None
            and (not isinstance(self.source_revision, str) or not (
                1 <= len(self.source_revision) <= MAX_REVISION_LEN
            ))
        ):
            raise SpecValidationError("SPEC_REVISION_INVALID", str(self.source_revision))
        if self.source_revision is not None:
            _clean(self.source_revision, "SPEC_REVISION_INVALID")
        if self.query is not None and (
            not isinstance(self.query, str) or len(self.query) > MAX_QUERY_LEN
        ):
            raise SpecValidationError("SPEC_QUERY_INVALID", "")
        if self.query_source is not None and not (
            isinstance(self.query_source, str) and len(self.query_source) <= 1024
        ):
            raise SpecValidationError("SPEC_QUERY_SOURCE_INVALID", str(self.query_source))
        # consistency: a mutable job must materialize from a proven checkout
        has_source = self.repo_root is not None or self.source_checkout is not None
        if has_source and not self.source_revision:
            raise SpecValidationError(
                "SPEC_REVISION_REQUIRED",
                "source checkout requires an explicit source_revision (never guessed)",
            )
        if self.execution_class == EXEC_MUTATING:
            if not has_source:
                raise SpecValidationError(
                    "SPEC_MUTATING_NEEDS_SOURCE",
                    "mutating jobs require a repo_root or source_checkout",
                )
            if self.workspace_policy != WORKSPACE_ISOLATED:
                raise SpecValidationError(
                    "SPEC_MUTATING_NEEDS_ISOLATED",
                    "mutating jobs require the isolated workspace policy",
                )
        if (
            self.execution_class == EXEC_AD_HOC
            and self.workspace_policy == WORKSPACE_SHARED_READ_ONLY
        ):
            raise SpecValidationError(
                "SPEC_AD_HOC_SHARED_INVALID", "ad_hoc jobs never share a read-only checkout"
            )
        # V2.03: prerequisite dependencies (simple, bounded, fail closed).
        if isinstance(self.depends_on, bool) or not isinstance(
            self.depends_on, (tuple, list)
        ):
            raise SpecValidationError(
                "SPEC_DEPENDENCY_MALFORMED", f"malformed depends_on: {repr(self.depends_on)[:160]}"
            )
        deps = tuple(self.depends_on)
        if len(deps) > MAX_DEPENDENCIES_ON:
            raise SpecValidationError(
                "SPEC_DEPENDENCY_MALFORMED", f"more than {MAX_DEPENDENCIES_ON} prerequisites"
            )
        seen: set[str] = set()
        for dep in deps:
            if (
                not isinstance(dep, str)
                or not (1 <= len(dep) <= MAX_JOB_ID_SPEC_LEN)
                or JOB_ID_SPEC_RE.fullmatch(dep) is None
            ):
                raise SpecValidationError(
                    "SPEC_DEPENDENCY_MALFORMED", f"invalid prerequisite: {repr(dep)[:160]}"
                )
            if dep == self.job_id:
                raise SpecValidationError("SPEC_DEPENDENCY_SELF", dep)
            if dep in seen:
                raise SpecValidationError("SPEC_DEPENDENCY_MALFORMED", f"duplicate: {dep}")
            seen.add(dep)
        if not isinstance(self.permit_failed_prereqs, bool):
            raise SpecValidationError(
                "SPEC_PREREQ_POLICY_INVALID", repr(self.permit_failed_prereqs)
            )
        # V2.04: explicit resource requirements (validated by the resource layer).
        if self.resources is not None:
            if not isinstance(self.resources, ResourceRequirement):
                raise SpecValidationError("SPEC_RESOURCES_INVALID", repr(self.resources)[:80])
            self.resources.validate()
        return self

    # -- deterministic serialization ---------------------------------------
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "execution_class": self.execution_class,
            "command": list(self.command),
            "workspace_policy": self.workspace_policy,
            "runner": self.runner,
            "max_attempts": self.max_attempts,
            "repo_root": str(self.repo_root) if self.repo_root is not None else None,
            "source_revision": self.source_revision,
            "source_checkout": (
                str(self.source_checkout) if self.source_checkout is not None else None
            ),
            "query": self.query,
            "query_source": self.query_source,
            "depends_on": list(self.depends_on),
            "permit_failed_prereqs": self.permit_failed_prereqs,
            "resources": self.resources.to_dict() if self.resources is not None else None,
        }

    @classmethod
    def from_dict(cls, data: object) -> JobSpec:
        """Strict parse: any deviation from the canonical shape is rejected."""
        if not isinstance(data, dict):
            raise SpecValidationError("SPEC_NOT_OBJECT", type(data).__name__)
        known = {
            "schema_version", "job_id", "execution_class", "command",
            "workspace_policy", "runner", "max_attempts", "repo_root",
            "source_revision", "source_checkout", "query", "query_source",
            "depends_on", "permit_failed_prereqs", "resources",
        }
        extra = set(data) - known
        if extra:
            raise SpecValidationError("SPEC_UNKNOWN_FIELDS", ",".join(sorted(extra)))
        try:
            schema = data.get("schema_version", SPEC_SCHEMA_VERSION)
            if not isinstance(schema, int) or isinstance(schema, bool):
                raise SpecValidationError("SPEC_SCHEMA_INVALID", str(schema))
            job_id = data["job_id"]
            if not isinstance(job_id, str):
                raise SpecValidationError("SPEC_JOB_ID_INVALID", repr(job_id))
            exec_class = data["execution_class"]
            command = data.get("command")
            if (
                command is None
                or isinstance(command, str)
                or not isinstance(command, (list, tuple))
            ):
                raise SpecValidationError("SPEC_COMMAND_INVALID", repr(command))
            policy = data.get("workspace_policy", WORKSPACE_ISOLATED)
            runner = data.get("runner", RUNNER_GENERIC)
            max_attempts = data.get("max_attempts", 2)

            # Predefined per-field error codes (consistent with validate()).
            _path_codes = {
                "repo_root": "SPEC_REPO_ROOT_INVALID",
                "source_checkout": "SPEC_SOURCE_CHECKOUT_INVALID",
            }

            def _path(key: str) -> Path | None:
                value = data.get(key)
                if value is None:
                    return None
                if not isinstance(value, (str, Path)):
                    raise SpecValidationError(
                        _path_codes[key], repr(value)[:80]
                    )
                return Path(value)

            revision = data.get("source_revision")
            if revision is not None and not isinstance(revision, str):
                raise SpecValidationError("SPEC_REVISION_INVALID", repr(revision))
            query = data.get("query")
            if query is not None and not isinstance(query, str):
                raise SpecValidationError("SPEC_QUERY_INVALID", repr(type(query)))
            query_source = data.get("query_source")
            if query_source is not None and not isinstance(query_source, str):
                raise SpecValidationError("SPEC_QUERY_SOURCE_INVALID", repr(type(query_source)))
        except KeyError as exc:
            raise SpecValidationError("SPEC_FIELD_MISSING", str(exc)) from exc

        # V2.03/V2.04 optional canonical fields (strict; default to absence).
        depends_on_raw = data.get("depends_on", ())
        if depends_on_raw is None:
            depends_on_raw = ()
        if (
            isinstance(depends_on_raw, bool)
            or not isinstance(depends_on_raw, (list, tuple))
            or any(not isinstance(dep, str) for dep in depends_on_raw)
        ):
            raise SpecValidationError("SPEC_DEPENDENCY_MALFORMED", repr(depends_on_raw)[:80])
        permit = data.get("permit_failed_prereqs", False)
        if not isinstance(permit, bool):
            raise SpecValidationError("SPEC_PREREQ_POLICY_INVALID", repr(permit))
        resources_raw = data.get("resources")
        parsed_resources: ResourceRequirement | None = None
        if resources_raw is not None:
            try:
                parsed_resources = ResourceRequirement.from_dict(resources_raw)
            except Exception as exc:
                raise SpecValidationError("SPEC_RESOURCES_INVALID", str(exc)[:80]) from exc
        spec = cls(
            schema_version=schema,
            job_id=job_id,
            execution_class=exec_class,
            command=tuple(command),
            workspace_policy=policy,
            runner=runner,
            max_attempts=max_attempts,
            repo_root=_path("repo_root"),
            source_revision=revision,
            source_checkout=_path("source_checkout"),
            query=query,
            query_source=query_source,
            depends_on=tuple(depends_on_raw),
            permit_failed_prereqs=permit,
            resources=parsed_resources,
        )
        return spec.validate()

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())


def _canonical_json(value: object) -> str:
    import json

    def _sort(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: _sort(obj[k]) for k in sorted(obj)}
        if isinstance(obj, (list, tuple)):
            return [_sort(v) for v in obj]
        return obj

    return json.dumps(_sort(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class SpecResult:
    """Result of building/validating specs (deterministic, bounded)."""

    accepted: int
    rejected: int
    spec: JobSpec | None = field(default=None)
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "spec": self.spec.to_dict() if self.spec is not None else None,
            "errors": list(self.errors),
        }


def build_spec(
    job_id: str,
    command: object,
    execution_class: str = EXEC_AD_HOC,
    *,
    workspace_policy: str = WORKSPACE_ISOLATED,
    runner: str = RUNNER_GENERIC,
    max_attempts: int = 2,
    repo_root: Path | str | None = None,
    source_revision: str | None = None,
    source_checkout: Path | str | None = None,
    query: str | None = None,
    query_file: Path | str | None = None,
    query_source: str | None = None,
    depends_on: tuple[str, ...] | list[str] | None = None,
    permit_failed_prereqs: bool = False,
    resources: ResourceRequirement | None = None,
) -> JobSpec:
    """Build and validate one canonical spec from validated inputs (fail-closed).

    An isolated final user query may be given inline (``query``) or read from a
    file (``query_file``); the final spec always carries the materialized
    content plus its provenance (``query_source``).
    """
    if command is None or isinstance(command, str) or not isinstance(command, (list, tuple)):
        raise SpecValidationError("SPEC_COMMAND_INVALID", repr(command)[:80])
    command = tuple(command)
    if query is None and query_file is not None:
        raw = Path(query_file).read_text(encoding="utf-8")
        query = raw
        query_source = query_source or f"file:{query_file}"
    if query is not None and query_source is None:
        query_source = "inline"
    spec = JobSpec(
        job_id=job_id,
        execution_class=execution_class,
        command=command,
        workspace_policy=workspace_policy,
        runner=runner,
        max_attempts=max_attempts,
        repo_root=Path(repo_root) if repo_root is not None else None,
        source_revision=source_revision,
        source_checkout=Path(source_checkout) if source_checkout is not None else None,
        query=query,
        query_source=query_source,
        depends_on=tuple(depends_on or ()),
        permit_failed_prereqs=permit_failed_prereqs,
        resources=resources,
    )
    return spec.validate()


__all__ = [
    "SPEC_SCHEMA_VERSION",
    "EXEC_AD_HOC",
    "EXEC_READ_ONLY",
    "EXEC_MUTATING",
    "EXECUTION_CLASSES",
    "WORKSPACE_ISOLATED",
    "WORKSPACE_SHARED_READ_ONLY",
    "WORKSPACE_POLICIES",
    "RUNNER_GENERIC",
    "RUNNER_TRAJECTORY_PI",
    "RUNNERS",
    "MAX_JOB_ID_SPEC_LEN",
    "MAX_COMMAND_PARTS",
    "MAX_COMMAND_PART_LEN",
    "MAX_REVISION_LEN",
    "MAX_RUNNER_LEN",
    "MAX_QUERY_LEN",
    "MAX_MAX_ATTEMPTS",
    "SOURCE_REVISION_MARKER_REL",
    "RUN_PROVENANCE_MARKER_REL",
    "WORKSPACE_MANIFEST_REL",
    "SpecValidationError",
    "JobSpec",
    "SpecResult",
    "build_spec",
]
