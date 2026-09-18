"""Mission 004 — production sub-run adapter (canonical trajectory-pi path).

Turns a bounded mission plan into *production* sub-run commands by mapping
each canonical phase kind onto the existing runtime surface:

* **MODEL-HEAVY** phases (``PLAN``, ``IMPLEMENT``, ``REPAIR``, ``REVIEW``)
  route to the ``scripts/trajectory-pi`` wrapper (or an operator-supplied
  adapter binary with the same contract) with the canonical Mission 002
  execution mode (``model.KIND_TO_MODE``), a bounded per-phase prompt
  file, and deterministic subprocess ownership.
  Every sub-run is a FRESH process/model context (no in-process agent
  state), so the same provider failure surface, review pipeline and
  evidence rules apply everywhere.
* **DETERMINISTIC** phases (``VALIDATE``, ``CONSOLIDATE``) run the
  operator's bounded validation / consolidation command directly — no
  model at all.

The mapping is deterministic (same inputs -> same argv) and fail-closed:
unknown kinds, empty objectives, or invalid wrappers raise
:class:`AdapterError` before any state is written.

Safety invariants (inherited from the runtime):

* the adapter builds argv only — it never executes, never touches git,
  and passes no git commit/push/PR/merge/reset/clean/stash/rebase or
  checkout/switch arguments;
* prompt files are bounded, explicit, persisted under the mission root
  (``prompts/<phase>.txt``) so every sub-run's context is inspectable
  evidence, never a hidden in-memory state;
* model-heavy phases are admitted under the resource policy (fail closed
  on missing/unknown capacity evidence) — one heavy GPU model at a time
  in the single-owner sequential design.
"""

from __future__ import annotations

import pathlib
import shlex
from collections.abc import Sequence

from trajectory_os.missions import model
from trajectory_os.missions.orchestrator import PhaseSpec

#: Default canonical wrapper (relative -> resolved against the mission
#: ``cwd``, which is the operator's working environment).
DEFAULT_PI_WRAPPER = "scripts/trajectory-pi"
DEFAULT_MODEL = "qwen3.8-dev3090"
#: Deterministic gate used for VALIDATE/CONSOLIDATE unless the operator
#: supplies their own command (default: the repository quality gate).
DEFAULT_VALIDATE_COMMAND = ("bash", "scripts/quality.sh")


class AdapterError(Exception):
    """Invalid adapter input (fail closed before any file is written)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


#: Canonical Mission 002 mode -> wrapper ``--class`` (deterministic).
#: The mode carries the execution semantics; the class carries the coarse
#: workload profile the wrapper expects (both must be consistent).
MODE_TO_CLASS: dict[str, str] = {
    "SMOKE": "smoke",
    "PLAN": "smoke",
    "IMPLEMENT": "feature",
    "REPAIR": "repair",
    "VERIFY": "smoke",
    "REVIEW": "feature",
    "RECOVERY": "recovery",
}

#: Deterministic per-kind phase instructions (prompt evidence).
PHASE_INSTRUCTIONS: dict[str, str] = {
    model.PH_PLAN: (
        "Produce a minimal execution plan for the objective: the concrete "
        "change to make, the evidence that proves it, and the validation "
        "command that will be run. No implementation, no repository change."
    ),
    model.PH_IMPLEMENT: (
        "Implement exactly the objective with the smallest correct change "
        "to the worktree. Emit the canonical completion marker on success. "
        "Do not run git commit, push, reset, clean, stash, rebase or any "
        "checkout/switch of branches."
    ),
    model.PH_REPAIR: (
        "Bounded repair: make the smallest fix that lets the deterministic "
        "validation pass. Single repair round; emit the canonical "
        "completion marker on success."
    ),
    model.PH_REVIEW: (
        "Read-only independent review of the exact worktree patch against "
        "the objective. No repository change; report findings only."
    ),
}


def is_model_heavy(kind: str) -> bool:
    """Delegated deterministic classification (kind-based)."""
    return model.is_model_heavy(kind)


def split_command(text: str | None) -> tuple[str, ...] | None:
    """Bounded deterministic split of an operator command string.

    Single space-separated string (CLI-friendly: no argparse ``-flag``
    ambiguity), shell-lexed; empty or unbounded commands are usage errors.
    """
    if text is None:
        return None
    try:
        parts = shlex.split(text)
    except ValueError as exc:
        raise AdapterError("COMMAND_SPLIT_INVALID", str(exc)) from exc
    if not (1 <= len(parts) <= model.MAX_COMMAND_PARTS):
        raise AdapterError("COMMAND_EMPTY", repr(text))
    for part in parts:
        if len(part) > model.MAX_COMMAND_PART_LEN:
            raise AdapterError("COMMAND_PART_TOO_LONG", repr(part[:24]))
    return tuple(parts)


def phase_mode(kind: str) -> str:
    """Canonical Mission 002 execution mode for a phase kind."""
    if kind not in model.KIND_TO_MODE:
        raise AdapterError("PHASE_KIND_INVALID", kind)
    return model.KIND_TO_MODE[kind]


def phase_class(kind: str) -> str:
    """Deterministic wrapper class matching the canonical mode."""
    mode = phase_mode(kind)
    if mode not in MODE_TO_CLASS:
        raise AdapterError("MODE_CLASS_INVALID", mode)
    return MODE_TO_CLASS[mode]


def render_phase_prompt(mission_id: str, phase_id: str, kind: str,
                        objective: str) -> str:
    """Bounded, deterministic prompt content for one phase (evidence)."""
    if not (1 <= len(objective) <= model.MAX_OBJECTIVE_LEN):
        raise AdapterError("OBJECTIVE_INVALID", str(len(objective)))
    if kind not in PHASE_INSTRUCTIONS:
        raise AdapterError("PROMPT_KIND_INVALID", kind)
    mode = phase_mode(kind)
    completion_marker = f"TRAJECTORY_{kind}_COMPLETE"
    parts = [
        "# TrajectoryOS mission sub-run context (bounded, deterministic)",
        "",
        f"mission   : {mission_id}",
        f"phase     : {phase_id}",
        f"kind      : {kind}",
        f"mode      : {mode}",
        f"[phase:{kind}]",
        "",
        "objective :",
        objective,
        "",
        "instructions",
        PHASE_INSTRUCTIONS[kind],
        "",
        "completion contract",
        "When the phase is actually complete, include a HANDOFF line that",
        "briefly states the result and evidence for the next phase.",
        "The exact final non-blank line of your response MUST be:",
        completion_marker,
        "Do not emit that marker unless the phase is genuinely complete.",
        "",
    ]
    return "\n".join(parts)


def materialize_phase_prompts(mission_root: pathlib.Path,
                              mission_id: str, objective: str,
                              phases: list[tuple[str, str]]) -> dict[str, str]:
    """Write one bounded prompt file per model-heavy phase (idempotent).

    ``phases`` is the canonical ``(phase_id, kind)`` sequence; only
    model-heavy kinds receive a prompt file (deterministic phases run the
    operator command verbatim and have no model context).
    Returns ``{phase_id: prompt_file}`` (string paths, stable order).
    """
    prompts_dir = mission_root / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}
    for phase_id, kind in phases:
        if not model.is_model_heavy(kind):
            continue
        if not phase_id:
            raise AdapterError("PROMPT_PHASE_ID_INVALID", phase_id)
        payload = render_phase_prompt(mission_id, phase_id, kind, objective)
        path = prompts_dir / f"{phase_id}.txt"
        path.write_text(payload, encoding="utf-8")
        out[phase_id] = str(path)
    return out


def phase_command(kind: str, *, pi_wrapper: str, prompt_file: str,
                  model_name: str, phase_id: str, objective: str) -> list[str]:
    """Deterministic production argv for one MODEL-HEAVY phase.

    Bounded and inspectable: fixed flag order, a non-empty query after
    ``--`` (wrapper contract), the canonical mode from
    ``model.KIND_TO_MODE`` and the matching class.  ``--dirty-ok`` is set
    because sequential phases legitimately accumulate a dirty worktree in
    the single-owner design; ``--no-notify`` keeps the path non-interactive.
    The canonical ``IMPLEMENT`` phase additionally carries the existing
    wrapper operator contract ``--require-changes`` so an empty
    implementation run cannot be treated as ready; PLAN, REVIEW and REPAIR
    commands never carry it.
    """
    if kind not in model.MODEL_HEAVY_KINDS:
        raise AdapterError("COMMAND_KIND_INVALID", kind)
    if not pi_wrapper or len(pi_wrapper) > model.MAX_COMMAND_PART_LEN:
        raise AdapterError("PI_WRAPPER_INVALID", pi_wrapper)
    if not prompt_file or len(prompt_file) > model.MAX_COMMAND_PART_LEN:
        raise AdapterError("PROMPT_FILE_INVALID", prompt_file)
    if not model_name or len(model_name) > model.MAX_COMMAND_PART_LEN:
        raise AdapterError("MODEL_INVALID", model_name)
    if not (1 <= len(objective) <= model.MAX_OBJECTIVE_LEN):
        raise AdapterError("OBJECTIVE_INVALID", str(len(objective)))

    mode = phase_mode(kind)
    class_name = phase_class(kind)
    tail = f"TrajectoryOS {phase_id}: {objective}"
    if len(tail) > model.MAX_COMMAND_PART_LEN:
        tail = tail[: model.MAX_COMMAND_PART_LEN]
    contract_flags = (["--require-changes"]
                      if kind == model.PH_IMPLEMENT else [])
    return [
        pi_wrapper,
        "--no-notify",
        "--dirty-ok",
        *contract_flags,
        "--class", class_name,
        "--mode", mode,
        "--model", model_name,
        "--prompt-file", prompt_file,
        "--", tail,
    ]


def build_canonical_specs(
    *,
    root: str,
    mission_id: str,
    objective: str,
    pi_wrapper: str = DEFAULT_PI_WRAPPER,
    model_name: str = DEFAULT_MODEL,
    validate_command: tuple[str, ...] = DEFAULT_VALIDATE_COMMAND,
    consolidate_command: tuple[str, ...] | None = None,
    repair_budget: int = model.MAX_REPAIR_ROUNDS,
    gpu: bool = False,
    gpu_mem_bytes: int = 0,
) -> tuple[PhaseSpec, ...]:
    """The canonical five-phase production plan (deterministic).

    Model-heavy phases -> canonical trajectory-pi path (see
    :func:`phase_command`); deterministic phases -> the operator's bounded
    command (default: the repository quality gate).  Optional GPU
    resource declaration gates the model-heavy phases under the resource
    policy (one heavy GPU model at a time; fail-closed without capacity
    evidence).
    """
    if not (1 <= len(objective) <= model.MAX_OBJECTIVE_LEN):
        raise AdapterError("OBJECTIVE_INVALID", str(len(objective)))
    # Absent validator (None) falls back to the canonical default (the
    # repository quality gate); an explicit empty/invalid command fails
    # closed instead (bounded, deterministic).
    if validate_command is None:
        validate_command = DEFAULT_VALIDATE_COMMAND
    if not (1 <= len(validate_command) <= model.MAX_COMMAND_PARTS):
        raise AdapterError("VALIDATE_COMMAND_INVALID", repr(validate_command))
    if consolidate_command is None:
        consolidate_cmd = tuple(validate_command)
    else:
        if not (1 <= len(consolidate_command) <= model.MAX_COMMAND_PARTS):
            raise AdapterError("CONSOLIDATE_COMMAND_INVALID",
                               repr(consolidate_command))
        consolidate_cmd = tuple(consolidate_command)

    mission_root = pathlib.Path(root) / "missions" / mission_id
    sequence = [(model.KIND_TO_PHASE_ID[k], k) for k in model.CANONICAL_SEQUENCE]

    # REPAIR is created dynamically by the orchestrator and therefore is not
    # part of the canonical five-phase sequence.  Still materialize its
    # prompt at mission creation so a later bounded repair has a fresh,
    # explicit REPAIR context instead of inheriting the IMPLEMENT prompt.
    prompt_sequence = [*sequence, ("repair", model.PH_REPAIR)]
    prompts = materialize_phase_prompts(
        mission_root, mission_id, objective, prompt_sequence
    )

    gpu_resources: dict[str, object] | None = (
        {"gpu": True, "gpu_mem_bytes": gpu_mem_bytes} if gpu else None
    )
    specs: list[PhaseSpec] = []
    prev: str | None = None
    for phase_id, kind in sequence:
        if model.is_model_heavy(kind):
            command: Sequence[str] = phase_command(
                kind,
                pi_wrapper=pi_wrapper,
                prompt_file=prompts[phase_id],
                model_name=model_name,
                phase_id=phase_id,
                objective=objective,
            )
            resources = gpu_resources
        else:
            command = (tuple(validate_command)
                       if kind == model.PH_VALIDATE
                       else tuple(consolidate_cmd))
            resources = None
        specs.append(PhaseSpec(
            kind=kind,
            command=command,
            phase_id=phase_id,
            depends_on=(prev,) if prev else (),
            resources=resources,
        ))
        prev = phase_id
    return tuple(specs)
