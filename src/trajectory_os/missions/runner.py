"""Mission 003 — bounded sub-run execution (fresh context per sub-run).

A *sub-run* is one bounded process execution of one phase's command.
Every sub-run:

* starts **fresh** — no inherited in-memory context; bounded by one process
  timeout, one worktree, and one evidence directory;
* persists its record **before** launch (``RUNNING``) and terminalizes it
  **after** — a crash in between leaves an explicit, fail-closed record
  (reconstruction never guesses);
* is classified deterministically and **fail closed** (see
  :func:`classify_subrun`):

  - a non-zero exit / timeout keep the process-evidence classification
    (FAILED / CRASHED) and are NEVER upgraded by semantic evidence;
  - a bare exit 0 is NOT sufficient for COMPLETED: it becomes COMPLETED
    only when a valid semantic result (see
    :mod:`trajectory_os.missions.semantic`) exists for this exact sub-run,
    is bound to its exact ``subrun_id``, and says SUCCESS;
  - missing / malformed / stale / subrun-mismatched semantic evidence,
    or a non-SUCCESS semantic status, classify fail closed (UNPROVEN /
    FAILED / CRASHED as the semantic status dictates).

:class:`ProcessPhaseRunner` is the production runner (subprocess).  Tests
use the scripted :class:`FakeRunner`.  Neither may exceed its bound.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from trajectory_os.missions import model, semantic


@dataclass(frozen=True)
class SubrunRequest:
    mission_id: str
    subrun_id: str
    phase_id: str
    kind: str
    mode: str
    round: int
    attempt: int
    command: tuple[str, ...]
    cwd: str | None
    timeout_s: int
    stdout_file: str
    stderr_file: str
    resources: dict[str, Any] | None
    # Mission 007: only model-heavy trajectory-pi sub-runs require the
    # structured semantic completion contract. Deterministic validation /
    # consolidation commands retain bounded process-exit semantics.
    semantic_required: bool = False


@dataclass(frozen=True)
class SubrunResult:
    exit_code: int | None       # process exit status, if one was observed
    classification: str         # model.CR_* (terminal)
    timed_out: bool = False
    # Mission 007 — structured semantic evidence (verified, bound to this
    # exact sub-run) for later persistence.  ``None`` means absent or
    # fail-closed-rejected (missing/malformed/stale/mismatched); the
    # stable reason code (``MISSING``, ``MALFORMED:...``, ``SUBRUN_MISMATCH``
    # ...) is preserved for audit.  These fields NEVER alter the
    # classification — that decision is already fail closed above.
    semantic_status: str | None = None
    semantic_error: str | None = None
    semantic_agent_classification: str | None = None
    semantic_readiness: str | None = None
    semantic_reason: str | None = None
    # Mission 010 — optional ``--require-changes`` provenance carried by the
    # producer's semantic document (``NOT_REQUIRED``/``SATISFIED``/
    # ``UNSATISFIED``/``UNPROVEN``). ``None`` on legacy evidence.
    semantic_require_changes: str | None = None
    # Mission 008 — exact execution attestation outcome.  ``attestation`` is
    # :data:`semantic.ATTESTATION_VERIFIED` only after the runner has
    # independently re-derived every identity; otherwise ``None`` and
    # ``attestation_error`` carries the stable fail-closed code
    # (``ATTESTATION_MISSING``/``..._MALFORMED``/``..._PARTIAL``/
    # ``..._MISMATCH``/``..._STALE``/``..._CONTRADICTORY``).
    attestation: str | None = None
    attestation_error: str | None = None

    @property
    def attestation_verified(self) -> bool:
        return self.attestation == semantic.ATTESTATION_VERIFIED


class PhaseRunner(Protocol):
    """Runs one bounded sub-run and returns deterministic evidence."""

    def run(self, request: SubrunRequest) -> SubrunResult:  # pragma: no cover
        ...


def classify_exit(exit_code: int | None, timed_out: bool = False) -> str:
    """Deterministic classification from process exit evidence (fail closed).

    NOTE: a bare exit 0 here means only "the process exited cleanly";
    it is NOT by itself sufficient for COMPLETED — semantic completion
    evidence is required (see :func:`classify_subrun`).
    """
    if timed_out or exit_code is None:
        return model.CR_CRASHED
    if exit_code == 0:
        return model.CR_COMPLETED
    if exit_code <= model.MAX_DETERMINISTIC_FAILURE_EXIT:
        return model.CR_FAILED
    return model.CR_CRASHED


#: exit 0 (process exited cleanly): verified semantic status -> terminal
#: sub-run classification.  Deterministic total mapping; SUCCESS is the
#: only status that maps to COMPLETED (everything else is fail closed).
_SEMANTIC_TO_CLASSIFICATION: dict[str, str] = {
    semantic.STATUS_SUCCESS: model.CR_COMPLETED,
    semantic.STATUS_INCOMPLETE: model.CR_UNPROVEN,
    semantic.STATUS_FAILED: model.CR_FAILED,
    semantic.STATUS_PROVIDER_FAILURE: model.CR_CRASHED,
    semantic.STATUS_UNKNOWN: model.CR_UNPROVEN,
}


def classify_subrun(exit_code: int | None,
                    *,
                    timed_out: bool = False,
                    semantic_status: str | None = None,
                    semantic_error: str | None = None,
                    semantic_agent_classification: str | None = None,
                    semantic_readiness: str | None = None,
                    semantic_reason: str | None = None,
                    semantic_require_changes: str | None = None,
                    semantic_mode: str | None = None,
                    attestation: str | None = None,
                    attestation_error: str | None = None) -> SubrunResult:
    """Deterministic, fail-closed sub-run classification (pure).

    * non-zero exit / timeout: the process-evidence classification is
      authoritative (FAILED / CRASHED); semantic + attestation evidence can
      annotate (the values are preserved for audit) but can NEVER upgrade
      a real process failure into COMPLETED;
    * exit 0: COMPLETED only if valid semantic evidence bound to this
      exact sub-run says SUCCESS **and** the exact execution attestation was
      independently verified (Mission 008). Any missing/malformed/stale/
      partial/contradictory/mismatched attestation — or a non-SUCCESS
      status — classifies fail closed (never COMPLETED);
    * Mission 010 defense-in-depth: a persisted SUCCESS with a KNOWN
      non-green readiness (``NEEDS_REVIEW``/``BLOCKED``) fails closed
      (FAILED); for writable modes an unrecognized readiness is UNPROVEN.
      A legacy record with absent readiness stays readable (unchanged).
    """
    base = classify_exit(exit_code, timed_out=timed_out)
    readiness_override = _readiness_override(
        semantic_mode, semantic_status, semantic_readiness)
    if base != model.CR_COMPLETED:
        classification = base
    elif readiness_override is not None:
        # Mission 010: a known non-green readiness can never be COMPLETED.
        classification = readiness_override
    elif semantic_status is None:
        classification = model.CR_UNPROVEN
    elif (semantic_status in semantic.SUCCESS_STATUSES
          and attestation != semantic.ATTESTATION_VERIFIED):
        # Attested success is the ONLY route to COMPLETED: an unattested
        # SUCCESS (legacy M007 record or any attestation rejection) is
        # explicitly UNPROVEN, never silently promoted.
        classification = model.CR_UNPROVEN
    else:
        classification = _SEMANTIC_TO_CLASSIFICATION[semantic_status]
    return SubrunResult(
        exit_code=exit_code,
        classification=classification,
        timed_out=timed_out,
        semantic_status=semantic_status,
        semantic_error=semantic_error,
        semantic_agent_classification=semantic_agent_classification,
        semantic_readiness=semantic_readiness,
        semantic_reason=semantic_reason,
        semantic_require_changes=semantic_require_changes,
        attestation=attestation,
        attestation_error=attestation_error,
    )


def _readiness_override(mode: str | None,
                        status: str | None,
                        readiness: str | None) -> str | None:
    """Mission 010 consumer rule (pure, fail closed).

    Returns the classification a persisted SUCCESS must take when its
    recorded readiness proves it is not green, or ``None`` when the normal
    attested-success path may proceed. Absent readiness is legacy evidence
    and is deliberately left to the normal path (readable, not re-derived).

    A KNOWN non-green readiness (``NEEDS_REVIEW``/``BLOCKED``) fails closed
    for every mode (defense-in-depth). An unrecognized readiness fails
    closed only for writable modes, matching the mandatory promotion
    contract; read-only modes keep their producer-derived status.
    """
    if status not in semantic.SUCCESS_STATUSES:
        return None
    if readiness is None:
        return None  # legacy evidence: no readiness claim to contradict
    if readiness in semantic.READINESS_FAILED:
        return model.CR_FAILED
    if readiness in semantic.READINESS_SUCCESS:
        return None
    if mode in semantic.WRITABLE_MODES:
        return model.CR_UNPROVEN  # unrecognized readiness: fail closed
    return None


def semantic_evidence_path(request: SubrunRequest) -> Path:
    """Unique, deterministic semantic evidence path for the exact sub-run.

    Derived from the sub-run's own evidence location and includes the
    exact ``subrun_id`` in the file name, so two sub-runs (or attempts)
    can never share a semantic result file.
    """
    if not request.subrun_id:
        raise ValueError("subrun_id is required (semantic evidence is "
                         "bound to the exact sub-run)")
    return Path(request.stdout_file).parent / f"{request.subrun_id}.semantic.json"


def attestation_launch_marker_path(request: SubrunRequest) -> Path:
    """Immutable per-subrun launch-order anchor for exact attestation.

    Unlike stdout/stderr, this marker is written exactly once immediately
    before the subprocess launch and is never touched by output capture.
    """
    if not request.subrun_id:
        raise ValueError("subrun_id is required (launch marker is bound "
                         "to the exact sub-run)")
    return Path(request.stdout_file).parent / f"{request.subrun_id}.launch"


def process_env(request: SubrunRequest) -> dict[str, str]:
    """Child environment for one sub-run.

    Inherited environment PLUS the semantic contract variables:

    * ``TRAJECTORY_SUBRUN_RESULT_FILE`` — where the producer must emit
      its structured result;
    * ``TRAJECTORY_SUBRUN_ID`` — the exact sub-run the result must bind to.

    Existing environment variables are preserved (only these two contract
    names are set), so operator-provided configuration is never clobbered.
    """
    env = dict(os.environ)
    if request.semantic_required:
        env[semantic.RESULT_FILE_ENV_VAR] = str(semantic_evidence_path(request))
        env[semantic.SUBRUN_ID_ENV_VAR] = request.subrun_id
    else:
        # These are internal per-subrun contract variables. Never leak a
        # parent process value into deterministic commands.
        env.pop(semantic.RESULT_FILE_ENV_VAR, None)
        env.pop(semantic.SUBRUN_ID_ENV_VAR, None)
    return env


def phase_state_for_classification(classification: str) -> str:
    if classification == model.CR_COMPLETED:
        return model.PS_PASSED
    if classification == model.CR_FAILED:
        return model.PS_FAILED
    return model.PS_UNPROVEN  # CRASHED / UNPROVEN: explicit, fail closed


# ---------------------------------------------------------------------------
# Mission 008 — independent attestation verification
# ---------------------------------------------------------------------------
# The runner NEVER trusts the producer's claims. It re-derives every identity
# from sources it can observe itself:
#   * the exact sub-run id it issued (``request.subrun_id``);
#   * the wrapper run directory + its ``meta.txt`` (run id, workspace, HEAD);
#   * a fresh ``git rev-parse HEAD`` in the repository (current HEAD must equal
#     both attested heads — the wrapper never commits);
#   * a fresh SHA-256 over the exact ``worktree.patch`` bytes;
#   * the launch ordering (``meta.txt`` must postdate a dedicated immutable
#     launch marker created by the runner immediately before subprocess launch).
# Any missing artifact, unreadable file, git failure, or disagreement fails
# closed with a stable code. This function performs I/O by design (it is the
# runner's job); the pure shape checks live in ``semantic``.

#: Bound for the read-only ``git rev-parse HEAD`` used for verification.
_GIT_PROBE_TIMEOUT_S = 10.0


def _git_head(repo_root: str) -> str | None:
    """Read-only, bounded ``git rev-parse HEAD`` (fail closed -> None)."""
    path_env = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    try:
        proc = subprocess.run(  # noqa: S603 (fixed argv, read-only)
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            env={"GIT_OPTIONAL_LOCKS": "0", "PATH": path_env},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    head = proc.stdout.decode("ascii", errors="replace").strip()
    return head if head else None


def _sha256_file(path: Path) -> str | None:
    """SHA-256 of the exact file bytes (fail closed -> None)."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _read_kv_file(path: Path) -> dict[str, str]:
    """Parse a wrapper ``key=value`` evidence file (bounded, fail closed)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


def verify_attestation(doc: Any,
                       request: SubrunRequest) -> tuple[bool, str | None]:
    """Independently verify a semantic doc's exact execution attestation.

    Returns ``(True, None)`` only when every identity is present, well-formed,
    and agrees with evidence the runner re-derives itself; otherwise
    ``(False, code)`` with a stable :mod:`semantic` failure code. Never raises.
    """
    att = semantic.extract_attestation(doc)
    code = semantic.validate_attestation(att)
    if code is not None:
        return (False, code)
    assert isinstance(att, dict)  # guaranteed by validate_attestation
    try:
        semantic.validate_attestation_binding(att, request.subrun_id)
    except semantic.SemanticError:
        return (False, semantic.ATT_MISMATCH)

    # Top-level M007 binding is authoritative and must agree as well.
    if not isinstance(doc, dict) or doc.get("subrun_id") != request.subrun_id:
        return (False, semantic.ATT_MISMATCH)

    run_id = semantic.attestation_field(att, "run_id")
    head_before = semantic.attestation_field(att, "repo_head_before")
    head_after = semantic.attestation_field(att, "repo_head_after")
    patch_sha = semantic.attestation_field(att, "patch_sha256")
    if None in (run_id, head_before, head_after, patch_sha):
        return (False, semantic.ATT_PARTIAL)
    assert run_id is not None and head_before is not None
    assert head_after is not None and patch_sha is not None

    if request.cwd is None:
        # No repository context means no independent anchor: fail closed.
        return (False, semantic.ATT_MISMATCH)
    try:
        repo_root = str(Path(request.cwd).resolve())
    except OSError:
        return (False, semantic.ATT_MISMATCH)
    run_dir = Path(repo_root) / ".trajectory-pi" / "runs" / run_id
    if not run_dir.is_dir():
        # The attested run did not happen here (or not at all): stale.
        return (False, semantic.ATT_STALE)

    meta_path = run_dir / "meta.txt"
    if not meta_path.is_file():
        return (False, semantic.ATT_STALE)

    # Launch-order staleness anchor: the wrapper meta must postdate the
    # runner's dedicated immutable launch marker. stdout/stderr are unsuitable
    # anchors because normal subprocess output updates their mtimes after the
    # wrapper has already created meta.txt.
    launch_path = attestation_launch_marker_path(request)
    try:
        meta_mtime_ns = meta_path.stat().st_mtime_ns
        launch_mtime_ns = launch_path.stat().st_mtime_ns
    except OSError:
        return (False, semantic.ATT_STALE)
    if meta_mtime_ns <= launch_mtime_ns:
        return (False, semantic.ATT_STALE)

    meta = _read_kv_file(meta_path)
    if meta.get("run_id") != run_id:
        return (False, semantic.ATT_MISMATCH)
    workspace = meta.get("workspace")
    if workspace is None:
        return (False, semantic.ATT_MISMATCH)
    try:
        if str(Path(workspace).resolve()) != repo_root:
            return (False, semantic.ATT_MISMATCH)
    except OSError:
        return (False, semantic.ATT_MISMATCH)
    if meta.get("head_before") != head_before:
        return (False, semantic.ATT_MISMATCH)

    # Independent repository anchor: current HEAD, re-derived by the runner.
    actual_head = _git_head(request.cwd)
    if actual_head is None:
        return (False, semantic.ATT_MISMATCH)
    if actual_head != head_after or actual_head != head_before:
        return (False, semantic.ATT_MISMATCH)

    # Exact patch: recompute the digest over the wrapper's exact patch bytes.
    actual_patch = _sha256_file(run_dir / "worktree.patch")
    if actual_patch is None or actual_patch != patch_sha:
        return (False, semantic.ATT_MISMATCH)

    return (True, None)


class ProcessPhaseRunner:
    """Bounded subprocess runner (one bounded process per sub-run).

    Before launch: clears any stale semantic evidence file for the exact
    sub-run and exports the semantic contract environment.  After
    completion: reads and validates that exact structured result and
    classifies fail closed — a bare exit 0 never becomes COMPLETED
    without valid, exact-subrun, SUCCESS semantic evidence.
    """

    def run(self, request: SubrunRequest) -> SubrunResult:
        out_path = Path(request.stdout_file)
        err_path = Path(request.stderr_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        err_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = list(request.command)
        if not cmd:
            return SubrunResult(exit_code=None, classification=model.CR_UNPROVEN)
        sem_path: Path | None = None
        if request.semantic_required:
            sem_path = semantic_evidence_path(request)
            launch_path = attestation_launch_marker_path(request)
            # Fail-closed hygiene: clear any stale per-subrun artifacts before
            # establishing this launch's unique ordering anchor.
            with contextlib.suppress(OSError):
                sem_path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                launch_path.unlink(missing_ok=True)
            try:
                launch_path.touch(exist_ok=False)
            except OSError:
                return SubrunResult(
                    exit_code=None,
                    classification=model.CR_CRASHED,
                    attestation_error=semantic.ATT_STALE,
                )
        timed_out = False
        exit_code: int | None = None
        with out_path.open("wb") as out_fh, err_path.open("wb") as err_fh:
            try:
                proc = subprocess.run(  # noqa: S603 (command is operator-fixed)
                    cmd,
                    cwd=request.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=out_fh,
                    stderr=err_fh,
                    env=process_env(request),
                    timeout=request.timeout_s,
                    check=False,
                )
                exit_code = int(proc.returncode)
            except subprocess.TimeoutExpired:
                timed_out = True
            except (OSError, subprocess.SubprocessError):
                exit_code = None

        # Deterministic phases (VALIDATE / CONSOLIDATE) do not invoke the
        # trajectory-pi semantic producer. Their historical bounded process
        # semantics remain authoritative.
        if not request.semantic_required:
            return SubrunResult(
                exit_code=exit_code,
                classification=classify_exit(exit_code, timed_out=timed_out),
                timed_out=timed_out,
            )

        assert sem_path is not None

        # Model-heavy trajectory-pi phases require exact semantic proof.
        # Read + verify the exact structured result (fail closed: any
        # rejection yields no usable status — never a guess).
        doc, read_error = semantic.read_semantic_file(str(sem_path))
        attestation: str | None = None
        attestation_error: str | None = None
        if read_error is not None:
            status: str | None = None
            error: str | None = read_error
            agent_classification = None
            readiness = None
            reason = None
            require_changes = None
        else:
            status, error = semantic.interpret_semantic(doc, request.subrun_id)
            if error is None and doc is not None:
                agent_classification = doc.get("agent_classification")
                readiness = doc.get("readiness")
                reason = doc.get("reason")
                require_changes = doc.get("require_changes")
                # Mission 008: independently verify the exact execution
                # attestation. Verification is attempted for every valid
                # document (so the outcome is recorded even for non-SUCCESS
                # statuses), but it only gates SUCCESS -> COMPLETED.
                verified, att_code = verify_attestation(doc, request)
                if verified:
                    attestation = semantic.ATTESTATION_VERIFIED
                else:
                    attestation_error = att_code
            else:
                agent_classification = None
                readiness = None
                reason = None
                require_changes = None
        return classify_subrun(
            exit_code,
            timed_out=timed_out,
            semantic_status=status,
            semantic_error=error,
            semantic_agent_classification=agent_classification,
            semantic_readiness=readiness,
            semantic_reason=reason,
            semantic_require_changes=require_changes,
            semantic_mode=request.mode,
            attestation=attestation,
            attestation_error=attestation_error,
        )


class FakeRunnerDeath(Exception):
    """Simulates the orchestrator process dying mid-mission (tests only)."""


class FakeRunner:
    """Deterministic scripted runner for tests.

    * ``exits``: phase_id -> ordered list of exit codes per attempt
      (falls back to 0 once the list is exhausted);
    * ``die_after_phase``: after returning a result for that phase, raise
      :class:`FakeRunnerDeath` — simulates the orchestrator crashing after
      the sub-run's terminal record was persisted but before the phase
      transition was written (the resume/reconstruction scenario);
    * ``die_after_subruns``: raise after the Nth sub-run completed.
    """

    def __init__(self,
                 exits: dict[str, list[int]] | None = None,
                 die_after_phase: str | None = None,
                 die_after_subruns: int | None = None) -> None:
        self.exits = {k: list(v) for k, v in (exits or {}).items()}
        self.die_after_phase = die_after_phase
        self.die_after_subruns = die_after_subruns
        self.calls: list[SubrunRequest] = []
        self._count = 0

    def run(self, request: SubrunRequest) -> SubrunResult:
        self.calls.append(request)
        self._count += 1
        if self.die_after_subruns is not None and self._count > self.die_after_subruns:
            raise FakeRunnerDeath(f"orchestrator crashed after sub-run {self._count - 1}")
        queue = self.exits.get(request.phase_id)
        exit_code = queue.pop(0) if queue else 0
        result = SubrunResult(exit_code=exit_code,
                              classification=classify_exit(exit_code))
        if self.die_after_phase is not None and request.phase_id == self.die_after_phase:
            raise FakeRunnerDeath("orchestrator crashed after sub-run for " + request.phase_id)
        return result
