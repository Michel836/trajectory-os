"""Mission 004 — operator CLI for production mission orchestration.

Canonical operator surface (mirrors the ``trajectory-pi-runs`` CLI style;
deterministic, fail-closed, no hidden state):

    create      create a mission (canonical five-phase bounded plan, no
                work is launched)
    start       one command (Mission 006): the create path above, and
                only if it fully validates and persists, followed by the
                run path below.  A create failure exits before any sub-run
                launches; an existing mission is rejected — start never
                silently resumes it
    run         run / resume the mission (bounded session; explicit
                workspace-conflict / resource / HEAD failures are
                fail-closed and machine-readable)
    continue    keep a non-terminal mission going (after a bounded
                session or RESOURCE_DEFERRED; re-checks resource
                admission)
    resume      explicit recovery: reconstruct (terminalize the
                ambiguous in-flight evidence, never guesses) then run
    status      benchmark + phase + sub-run state (human + machine output
                from the same canonical state)
    reconstruct deterministic reconstruction (never guesses)
    evidence    phase evidence (proven sub-run identity + worktree)
    summary     human/machine summary of one mission
    note        record a bounded human intervention (operator note)
    list        all missions under the root with state
    version     CLI version

Exit codes:
    0  OK / COMPLETE
    2  usage error (rejected before any state is written)
    3  mission rejected / BLOCKED / FAILED (explicit, machine-readable)
    4  mission not found
    5  mission in-flight / non-terminal (session bound, resource deferred,
       or still RUNNING)

Root resolution: ``--root PATH`` > ``$TRAJECTORY_MISSIONS_ROOT`` >
``<cwd>/.trajectory-pi``.

``--root`` and ``--json`` are accepted both globally (before the
subcommand) and per-subcommand (after the subcommand).  The per-subcommand
form is the natural operator convention (e.g. ``status ID --json``) and is
what ``scripts/mission004_proof.py``-style scripts and humans reach for first;
without it the same flag rejected after the command was indistinguishable
from a genuine usage error (same exit 2).  Precedence when both are given is
deterministic: per-subcommand value > global value.  ``--json`` is an
idempotent boolean, so giving it in either (or both) positions enables
machine-readable output.  This is pure CLI ergonomics (no state-machine,
resource, or git-safety change) and keeps every existing global-position
invocation byte-compatible.  ``start --json`` emits the create payload
followed by the run report (two JSON documents in sequence).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from trajectory_os import __version__
from trajectory_os.missions import (
    adapter,
    flow,
    identity,
    model,
    orchestrator,
    store,
    summary,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REJECTED = 3
EXIT_NOT_FOUND = 4
EXIT_IN_FLIGHT = 5

_CAPACITY_KEYS = frozenset({"cpu_slots", "ram_bytes", "gpu_slots",
                            "gpu_mem_bytes"})


def _fail(usage: str, code: int = EXIT_USAGE) -> int:
    print(f"error: {usage}", file=sys.stderr)
    return code


def _root_from(argv_root: str | None) -> str:
    if argv_root:
        return argv_root
    env_root = os.environ.get("TRAJECTORY_MISSIONS_ROOT")
    if env_root:
        return env_root
    return str(Path.cwd() / ".trajectory-pi")


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _state_exit(mission_state: str) -> int:
    if mission_state == model.MS_COMPLETE:
        return EXIT_OK
    if mission_state in (model.MS_BLOCKED, model.MS_FAILED):
        return EXIT_REJECTED
    # RUNNING / RESOURCE_DEFERRED / PLANNING
    return EXIT_IN_FLIGHT


def _print_report(report: orchestrator.MissionReport, as_json: bool) -> int:
    if as_json:
        _print_json(report.to_dict())
    else:
        print(f"mission  : {report.mission_id} -> {report.mission_state} "
              f"({report.mission_reason}) [{report.stop}]")
        if report.summary is not None:
            jobs = report.summary["jobs"]
            print(f"subruns  : {jobs['started']}/{jobs['created']} "
                  f"(completed {jobs['completed']}, budget {jobs['budget']})")
            automatic = report.summary["automatic"]
            print(f"repairs  : {automatic['repairs_used']}/"
                  f"{automatic['repair_budget']} "
                  f"(retries {automatic['retries']})")
            provider = report.summary["provider_failures"]
            print(f"provider : recovered={provider['recovered']} "
                  f"surfaced={provider['surfaced']}")
            heavy = report.summary["model_heavy"]
            print(f"model    : heavy_subruns={heavy['subrun_count']} "
                  f"heavy_phases={heavy['phase_count']}")
    return _state_exit(report.mission_state)


class UsageError(Exception):
    pass


def parse_policy(args: list[str]) -> dict[str, int]:
    """Deterministic operator resource policy (capacity keys only).

    Only validated capacity keys are accepted; unknown keys and non-integer
    values are usage errors (the orchestrator would otherwise fail closed
    with an opaque policy error *after* state inspection).
    """
    policy: dict[str, int] = {}
    for item in args:
        key, sep, value = item.partition("=")
        if not (sep and key in _CAPACITY_KEYS):
            raise UsageError(
                "policy expects KEY=INT with KEY in "
                f"{sorted(_CAPACITY_KEYS)}: {item!r}")
        try:
            value_int = int(value)
        except ValueError as exc:
            raise UsageError(f"policy value must be an integer: {value!r}") from exc
        if value_int < 0:
            raise UsageError(f"policy value must be >= 0: {item!r}")
        policy[key] = value_int
    return policy


def _run_mission(root: str, mission_id: str,
                 policy_args: list[str],
                 session: int | None, as_json: bool) -> int:
    try:
        policy = parse_policy(policy_args)
    except UsageError as exc:
        return _fail(str(exc))
    max_session = model.MAX_SESSION_SUBRUNS if session is None else session
    if not (1 <= max_session <= model.MAX_SESSION_SUBRUNS):
        return _fail(f"session bound out of bounds: {max_session}")
    try:
        report = orchestrator.run_mission(
            root, mission_id,
            orchestrator.new_runner("process"),
            max_session_subruns=max_session,
            resource_policy=policy)
    except store.MissionNotFound:
        print(f"error: mission not found in {root}: {mission_id}",
              file=sys.stderr)
        return EXIT_NOT_FOUND
    except (store.MalformedMissionError, orchestrator.ConfigError) as exc:
        print(f"error(rejected): {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except flow.IllegalTransitionError as exc:
        # Deterministic state-machine boundary (fail closed, explicit).
        print(f"error(transition): {exc.code}: {exc.message}", file=sys.stderr)
        return EXIT_REJECTED
    except Exception as exc:  # fail closed: bounded, machine-readable
        print(f"error(run): {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_REJECTED
    return _print_report(report, as_json)


def _print_reconstruction(recon: orchestrator.ReconstructionReport,
                          as_json: bool) -> None:
    if as_json:
        _print_json(recon.to_dict())
        return
    print(f"reconstruction: mission={recon.mission_id} "
          f"state={recon.mission_state} resumable={recon.resumable}")
    print(f"  proven        : {recon.proven_passed}")
    print(f"  unproven      : {recon.explicit_unproven}")
    print(f"  failed        : {recon.failed}")
    print(f"  pending       : {recon.pending}")
    if recon.next is not None:
        print(f"  next          : {recon.next}")


def _not_found(root: str, mission_id: str) -> int:
    print(f"error: mission not found in {root}: {mission_id}", file=sys.stderr)
    return EXIT_NOT_FOUND


# --- bounded declarative input (Mission 006 ``start``) ---------------------

#: Hard byte cap checked *before* JSON parsing (fail closed on overflow).
SPEC_MAX_BYTES = 65536
#: Hard byte cap for ``--objective-file``: a file beyond
#: ``MAX_OBJECTIVE_LEN`` characters cannot fit in more than 4 UTF-8 bytes
#: per character, so anything larger is oversized by construction and is
#: rejected at the byte level (no unbounded read).
OBJECTIVE_FILE_MAX_BYTES = model.MAX_OBJECTIVE_LEN * 4
#: The authoritative GPU-memory default (24 GiB), formerly the parser
#: default.  It stays the single default — the parser now reports *unset*
#: as ``None`` so a spec value can be merged in between CLI and default.
_DEFAULT_GPU_MEM_BYTES = 25769803776

#: Spec string fields (values flow into the existing create options).
_SPEC_STRING_FIELDS = ("objective", "repo", "head", "pi_wrapper",
                       "model", "validate", "consolidate")
_SPEC_INT_FIELDS = ("gpu_mem", "time_budget", "subrun_budget",
                    "repair_budget", "session_subruns")
_ALLOWED_SPEC_KEYS = (frozenset(_SPEC_STRING_FIELDS)
                      | frozenset(_SPEC_INT_FIELDS)
                      | {"gpu", "policy"})


def read_objective_file(path: str) -> str:
    """Read a bounded objective text file (fail closed, UTF-8, stripped).

    Missing / unreadable / non-regular / non-UTF-8 / oversized inputs are
    ``UsageError`` rejections; the result is whitespace-stripped text whose
    length is checked against the canonical objective bounds by the caller.
    """
    p = Path(path)
    if not p.is_file():
        raise UsageError("objective file missing or not a regular file: "
                         f"{path}")
    try:
        with p.open("rb") as handle:
            raw = handle.read(OBJECTIVE_FILE_MAX_BYTES + 1)
    except OSError as exc:
        raise UsageError(f"objective file unreadable: {exc}") from exc
    if len(raw) > OBJECTIVE_FILE_MAX_BYTES:
        raise UsageError(
            f"objective file exceeds {OBJECTIVE_FILE_MAX_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError("objective file must be UTF-8 text") from exc
    return text.strip()


def load_spec(path: str) -> dict[str, Any]:
    """Parse and validate a bounded declarative launch spec (fail closed).

    Strict JSON object only: a hard byte cap applies before parsing, and
    malformed JSON, non-object JSON, unknown keys and wrong types are
    ``UsageError`` rejections.  Only fields relevant to the existing
    create/run options are accepted; their downstream validation (adapter,
    mission config, session bound) stays authoritative.
    """
    p = Path(path)
    try:
        with p.open("rb") as handle:
            raw = handle.read(SPEC_MAX_BYTES + 1)
    except OSError as exc:
        raise UsageError(f"spec unreadable: {exc}") from exc
    if len(raw) > SPEC_MAX_BYTES:
        raise UsageError(
            f"spec exceeds the hard cap of {SPEC_MAX_BYTES} bytes (before "
            "JSON parsing)")
    try:
        obj: Any = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise UsageError("spec must be UTF-8 encoded JSON") from exc
    except json.JSONDecodeError as exc:
        raise UsageError(
            f"spec is not valid JSON: {exc.msg} (line {exc.lineno}, "
            f"column {exc.colno})") from exc
    if not isinstance(obj, dict):
        raise UsageError("spec must be a JSON object")
    unknown = sorted(set(obj) - _ALLOWED_SPEC_KEYS)
    if unknown:
        raise UsageError(f"spec unknown key(s): {', '.join(unknown)}")
    for field in _SPEC_STRING_FIELDS:
        if field in obj and (not isinstance(obj[field], str)
                             or not obj[field].strip()):
            raise UsageError(f"spec {field} must be a non-empty string")
    if "gpu" in obj and not isinstance(obj["gpu"], bool):
        raise UsageError("spec gpu must be a boolean")
    for field in _SPEC_INT_FIELDS:
        if field in obj:
            value = obj[field]
            if isinstance(value, bool) or not isinstance(value, int) \
                    or value < 0:
                raise UsageError(f"spec {field} must be an integer >= 0")
    if "session_subruns" in obj and not (
            1 <= obj["session_subruns"] <= model.MAX_SESSION_SUBRUNS):
        raise UsageError(
            f"spec session_subruns must be 1..{model.MAX_SESSION_SUBRUNS}")
    if "policy" in obj:
        entries = obj["policy"]
        if not isinstance(entries, list) or \
                not all(isinstance(e, str) for e in entries):
            raise UsageError("spec policy must be a list of KEY=INT strings")
        parse_policy(entries)  # canonical policy validation (fail closed)
    return obj


# --- commands ------------------------------------------------------------------


def _cmd_create(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    mission_id = args.id
    if not model.ID_RE.fullmatch(mission_id or ""):
        return _fail("invalid mission id (use 2..64 [a-z0-9._-], starting "
                     "[a-z0-9])")
    if args.objective is None:
        return _fail("objective is required (--objective, --objective-file "
                     "or spec \"objective\")")
    objective = args.objective.strip()
    if not (1 <= len(objective) <= model.MAX_OBJECTIVE_LEN):
        return _fail(f"objective must be 1..{model.MAX_OBJECTIVE_LEN} chars "
                     "(trim spaces)")
    repo = args.repo or os.getcwd()
    paths = store.mission_paths(root, mission_id)
    if paths["mission"].is_file():
        return _fail(f"mission already exists: {mission_id}", EXIT_REJECTED)

    try:
        validate_cmd = (adapter.split_command(args.validate)
                        or adapter.DEFAULT_VALIDATE_COMMAND)
        consolidate_cmd = (adapter.split_command(args.consolidate)
                           if args.consolidate else None)
    except adapter.AdapterError as exc:
        return _fail(f"adapter: {exc}", EXIT_USAGE)
    try:
        specs = adapter.build_canonical_specs(
            root=root,
            mission_id=mission_id,
            objective=objective,
            pi_wrapper=args.pi_wrapper or adapter.DEFAULT_PI_WRAPPER,
            model_name=args.model or adapter.DEFAULT_MODEL,
            validate_command=validate_cmd,
            consolidate_command=consolidate_cmd,
            repair_budget=(args.repair_budget
                           if args.repair_budget is not None
                           else model.MAX_REPAIR_ROUNDS),
            gpu=args.gpu,
            gpu_mem_bytes=(args.gpu_mem
                           if args.gpu_mem is not None
                           else _DEFAULT_GPU_MEM_BYTES),
        )
    except adapter.AdapterError as exc:
        return _fail(f"adapter: {exc}")

    config = orchestrator.MissionConfig(
        mission_id=mission_id,
        objective=objective,
        phase_specs=specs,
        repo_root=repo,
        cwd=repo,
        baseline_revision=args.head,
        time_budget_s=(args.time_budget
                       if args.time_budget is not None
                       else model.DEFAULT_TIME_BUDGET_S),
        repair_budget=(args.repair_budget
                       if args.repair_budget is not None
                       else model.MAX_REPAIR_ROUNDS),
        subrun_budget=args.subrun_budget,
    )
    try:
        store.ensure_layout(paths)
        orchestrator.create_mission(root, config)
    except (ValueError, orchestrator.ConfigError,
            store.MalformedMissionError) as exc:
        return _fail(f"create rejected: {exc}", EXIT_REJECTED)

    head = orchestrator.git_head(repo)
    if args.head is not None and head != args.head:
        check = "HEAD_DRIFT (running will fail closed until resolved)"
    elif args.head is None and head is not None:
        check = "OK (no explicit review baseline)"
    elif args.head is None and head is None:
        check = "NO_REPOSITORY (HEAD probe failed; review fails closed)"
    else:
        check = "OK"
    payload = {
        "status": "CREATED",
        "mission_id": mission_id,
        "mission_root": str(paths["root"]),
        "baseline_revision": args.head,
        "head_probe": head,
        "head_check": check,
        "phases": [s.phase_id for s in specs],
        "model_heavy_phases": sorted(model.MODEL_HEAVY_KINDS),
        "deterministic_phases": sorted(model.DETERMINISTIC_KINDS),
        "prompt_files": {
            s.phase_id: str(paths["root"] / "prompts" / f"{s.phase_id}.txt")
            for s in specs if model.is_model_heavy(s.kind)
        },
    }
    if args.json:
        _print_json(payload)
        return EXIT_OK
    print(f"created  : {mission_id} at {paths['root']}")
    print(f"baseline : {args.head} (head probe: {head}) [{check}]")
    print(f"plan     : {' -> '.join(s.phase_id for s in specs if s.phase_id)}")
    print(f"model    : {args.model or adapter.DEFAULT_MODEL} "
          f"via {args.pi_wrapper or adapter.DEFAULT_PI_WRAPPER}")
    return EXIT_OK


def _cmd_start(args: argparse.Namespace) -> int:
    """Mission 006 — one-command start: create followed by run.

    Pure composition of the existing handlers — no second orchestration
    path.  The create path must fully validate and persist (exit 0)
    before the run path is entered, so any create failure exits before a
    sub-run launches.  An already-existing mission is rejected by the
    create path (fail closed) — start never silently resumes it.

    Bounded declarative input: ``--objective-file`` and ``--spec`` supply
    launch values; precedence is explicit CLI value > spec value > existing
    parser/create defaults.  All file/spec *and* run-stage usage validation
    (policy entries, session bounds) is a fail-closed usage rejection
    *before* the create path, so a rejection writes no mission state and
    launches no provider.
    """
    spec_file: str | None = args.spec
    objective_file: str | None = args.objective_file
    if spec_file is not None and objective_file is not None:
        return _fail("--spec and --objective-file are contradictory; use "
                     "one or the other")
    if args.objective is not None and objective_file is not None:
        return _fail("--objective and --objective-file are contradictory; "
                     "use one or the other")

    spec: dict[str, Any] | None = None
    if spec_file is not None:
        try:
            spec = load_spec(spec_file)
        except UsageError as exc:
            return _fail(f"spec: {exc}")
    if objective_file is not None:
        try:
            objective = read_objective_file(objective_file)
        except UsageError as exc:
            return _fail(f"objective-file: {exc}")
        args.objective = objective  # bounds checked by the create path

    objective_source = "cli"
    if spec is not None:
        if args.objective is not None:
            objective_source = "cli"          # explicit CLI wins over spec
        elif "objective" in spec:
            objective_source = "spec"
            args.objective = spec["objective"]
        else:
            return _fail("objective is required (CLI --objective or the "
                         "spec \"objective\" field)")
        for field in _SPEC_STRING_FIELDS:
            if field == "objective" or getattr(args, field) is not None:
                continue
            if field in spec:
                setattr(args, field, spec[field])
        if not args.gpu and bool(spec.get("gpu")):
            args.gpu = True
        for field in _SPEC_INT_FIELDS:
            if getattr(args, field) is None and field in spec:
                setattr(args, field, spec[field])
        if not args.policy and "policy" in spec:
            args.policy = list(spec["policy"])
    elif not args.objective:
        return _fail("objective is required (CLI --objective or "
                     "--objective-file)")
    elif objective_file is not None:
        objective_source = "objective_file"

    # Fail closed on invalid run-stage usage *before* the create path:
    # a rejected ``start`` must leave no mission state and launch no
    # provider.  The canonical validators are reused (no parser logic
    # duplication); the run path re-validates authoritatively later.
    try:
        parse_policy(args.policy)
    except UsageError as exc:
        return _fail(str(exc))
    if args.session_subruns is not None and not (
            1 <= args.session_subruns <= model.MAX_SESSION_SUBRUNS):
        return _fail(f"session bound out of bounds: {args.session_subruns}")

    created = _cmd_create(args)
    if created != EXIT_OK:
        return created
    if spec is not None or objective_file is not None:
        # Persist how the mission was launched (existing canonical event
        # log — no second state engine).  Plain start/create invocations
        # remain byte-identical to before.
        root = _root_from(args.root)
        paths = store.mission_paths(root, args.id)
        # Bounded, deterministic normalized launch configuration: the
        # effective values actually passed to create/run after
        # CLI > spec > default resolution.  Persisting them (not the
        # external file paths alone) makes launch evidence independent
        # of later mutation/deletion of the spec or objective files.
        # The canonical objective text is deliberately *not* duplicated
        # here — mission state is authoritative; only objective_source.
        # Values are stable primitives/lists only.
        normalized = {
            "repo": args.repo or os.getcwd(),
            "head": args.head,
            "pi_wrapper": args.pi_wrapper or adapter.DEFAULT_PI_WRAPPER,
            "model": args.model or adapter.DEFAULT_MODEL,
            "validate": list(adapter.split_command(args.validate)
                             or adapter.DEFAULT_VALIDATE_COMMAND),
            "consolidate": (
                list(adapter.split_command(args.consolidate) or ())
                if args.consolidate
                else None
            ),
            "gpu": bool(args.gpu),
            "gpu_mem": (args.gpu_mem if args.gpu_mem is not None
                        else _DEFAULT_GPU_MEM_BYTES),
            "time_budget": (args.time_budget if args.time_budget is not None
                            else model.DEFAULT_TIME_BUDGET_S),
            "subrun_budget": args.subrun_budget,
            "repair_budget": (args.repair_budget
                              if args.repair_budget is not None
                              else model.MAX_REPAIR_ROUNDS),
            "policy": list(args.policy),
            "session_subruns": args.session_subruns,
        }
        try:
            store.append_event(paths, {
                "ts": orchestrator.utc_now_iso(),
                "event": "launch",
                "objective_source": objective_source,
                "spec_file": spec_file,
                "objective_file": objective_file,
                "normalized": normalized,
            })
        except OSError as exc:
            # Controlled fail-closed result (same rejection model as the
            # create path): the persisted mission state stays intact and
            # reconstructable (no rollback — valid persisted state is never
            # deleted); start does not claim success and no sub-run is
            # launched.
            print(f"error(rejected): launch provenance persistence "
                  f"failed: {exc}", file=sys.stderr)
            return EXIT_REJECTED
    return _cmd_run(args)


def _cmd_run(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    if not store.mission_paths(root, args.id)["mission"].is_file():
        return _not_found(root, args.id)
    if args.session_subruns is not None and \
            not (1 <= args.session_subruns <= model.MAX_SESSION_SUBRUNS):
        return _fail(f"session bound out of bounds: {args.session_subruns}")
    return _run_mission(root, args.id, args.policy, args.session_subruns,
                        args.json)


def _cmd_continue(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        mission, _ = store.load_mission(root, args.id)
    except store.MissionNotFound:
        return _not_found(root, args.id)
    except store.MalformedMissionError as exc:
        print(f"error(malformed): {exc}", file=sys.stderr)
        return EXIT_REJECTED
    if mission.terminal():
        print(f"mission {args.id} is terminal ({mission.mission_state}; "
              f"{mission.mission_reason}) — nothing to continue")
        return _state_exit(mission.mission_state)
    session = args.session_subruns
    if session is None:
        session = min(mission.subrun_budget, model.MAX_SESSION_SUBRUNS)
    return _run_mission(root, args.id, args.policy, session, args.json)


def _cmd_resume(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        recon = orchestrator.reconstruct(root, args.id)
    except store.MissionNotFound:
        return _not_found(root, args.id)
    except store.MalformedMissionError as exc:
        print(f"error(malformed): {exc}", file=sys.stderr)
        return EXIT_REJECTED
    _print_reconstruction(recon, args.json)
    if not recon.resumable:
        # Deterministic mission state: resume is only meaningful when the
        # mission is non-terminal.
        return _state_exit(recon.mission_state)
    return _run_mission(root, args.id, args.policy, args.session_subruns,
                        args.json)


def _cmd_status(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        doc = summary.mission_status(root, args.id)
    except store.MissionNotFound:
        return _not_found(root, args.id)
    except store.MalformedMissionError as exc:
        print(f"error(malformed): {exc}", file=sys.stderr)
        return EXIT_REJECTED
    if getattr(args, "json", False):
        _print_json(doc)
        return EXIT_OK
    print(doc["rendered"])
    return _state_exit(doc["summary"]["mission_state"])


def _cmd_reconstruct(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        recon = orchestrator.reconstruct(root, args.id)
    except store.MissionNotFound:
        return _not_found(root, args.id)
    except store.MalformedMissionError as exc:
        print(f"error(malformed): {exc}", file=sys.stderr)
        return EXIT_REJECTED
    _print_reconstruction(recon, args.json)
    return EXIT_OK


def _cmd_evidence(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        mission, paths = store.load_mission(root, args.id)
        phase = mission.phase(args.phase)
    except store.MissionNotFound:
        return _not_found(root, args.id)
    except store.MalformedMissionError as exc:
        print(f"error(malformed): {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except KeyError:
        return _fail(f"phase not found: {args.phase}")
    evidence: dict[str, Any] | None = None
    try:
        evidence = store.load_phase_evidence(paths, args.phase)
    except store.MalformedMissionError:
        evidence = None  # persisted only for proven PASSED phases
    payload = {
        "mission_id": args.id,
        "phase_id": args.phase,
        "kind": phase.kind,
        "phase_state": phase.state,
        "evidence": evidence,
        "subruns": [
            store.load_subrun(paths, subrun_id).to_dict()
            for subrun_id in phase.subrun_ids
        ],
    }
    if args.json:
        _print_json(payload)
        return EXIT_OK
    print(f"phase  : {args.phase} ({phase.kind}) -> {phase.state}")
    if evidence is not None:
        worktree = evidence.get("worktree")
        if isinstance(worktree, dict):
            print(f"head   : {worktree.get('head')}")
            print(f"patch  : {worktree.get('worktree_patch_sha256')}")
            print(f"domain : {worktree.get('domain', identity.MISSION_WORKTREE_DOMAIN)}"
                  f" (field {worktree.get('field', identity.MISSION_WORKTREE_FIELD)})"
                  " — independent from the wrapper snapshot digest; never "
                  "compared")
    else:
        print("evidence: not persisted (phase did not pass) — sub-run "
              "records below are the canonical evidence")
    for subrun in payload["subruns"]:
        print(f"subrun : {subrun['subrun_id']} exit={subrun['exit_code']} "
              f"-> {subrun['classification']}")
        require_changes = subrun.get("semantic_require_changes")
        if require_changes is not None:
            print(f"  require-changes: {require_changes}")
        if subrun.get("attestation") is not None or subrun.get(
                "attestation_error") is not None:
            print(f"  patch-domain   : {identity.WRAPPER_SNAPSHOT_DOMAIN} "
                  f"(field {identity.WRAPPER_SNAPSHOT_FIELD}) — independent "
                  "from the mission worktree digest; never compared")
    return EXIT_OK


def _cmd_summary(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    try:
        mission, paths = store.load_mission(root, args.id)
    except store.MissionNotFound:
        return _not_found(root, args.id)
    except store.MalformedMissionError as exc:
        print(f"error(malformed): {exc}", file=sys.stderr)
        return EXIT_REJECTED
    doc = summary.mission_summary(mission, paths)
    if getattr(args, "json", False):
        _print_json(doc)
        return EXIT_OK
    print(summary.render_summary(doc))
    heavy = doc["model_heavy"]
    print(f"model-heavy subruns: {heavy['subrun_count']} "
          f"(phases: {heavy['phase_count']})")
    return EXIT_OK


def _cmd_note(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    text = args.text.strip()
    if not text:
        return _fail("note text must be non-empty")
    try:
        orchestrator.record_human_note(root, args.id, text)
    except store.MissionNotFound:
        return _not_found(root, args.id)
    except store.MalformedMissionError as exc:
        print(f"error(malformed): {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except ValueError as exc:
        return _fail(str(exc), EXIT_REJECTED)
    print(f"note recorded for {args.id} ({len(text)} chars)")
    return EXIT_OK


def _cmd_list(args: argparse.Namespace) -> int:
    root = _root_from(args.root)
    base = Path(root) / "missions"
    entries: list[dict[str, Any]] = []
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            try:
                mission, _ = store.load_mission(root, child.name)
            except store.MalformedMissionError:
                entries.append({"mission_id": child.name, "state": "MALFORMED",
                                "reason": None, "phases_total": 0,
                                "phases_passed": 0, "subruns": 0,
                                "terminal": False})
                continue
            entries.append({
                "mission_id": mission.mission_id,
                "state": mission.mission_state,
                "reason": mission.mission_reason,
                "phases_total": len(mission.phases),
                "phases_passed": sum(
                    1 for p in mission.phases if p.state == model.PS_PASSED),
                "subruns": len(mission.subruns),
                "terminal": mission.terminal(),
            })
    if args.json:
        _print_json({"root": root, "missions": entries})
        return EXIT_OK
    if not entries:
        print(f"no missions under {root}")
        return EXIT_OK
    for entry in entries:
        print(f"{entry['mission_id']}  {entry['state']} ({entry['reason']}) "
              f"phases={entry['phases_passed']}/{entry['phases_total']} "
              f"subruns={entry['subruns']} terminal={entry['terminal']}")
    return EXIT_OK


def _cmd_version() -> int:
    print(f"trajectory-pi-missions {__version__}")
    return EXIT_OK


# --- parser --------------------------------------------------------------------


def _add_shared_flags(subparser: argparse.ArgumentParser) -> None:
    """Register per-subcommand ``--root`` / ``--json`` (distinct dests).

    Separate dests (``sub_root`` / ``sub_json``) avoid the classic argparse
    subparser-default-overwrite pitfall and keep the global ``--root`` /
    ``--json`` flags byte-compatible; ``main()`` merges them with the per-
    subcommand value winning.  ``version`` is intentionally excluded.
    """
    subparser.add_argument("--root", default=None, dest="sub_root",
                           help="missions root for this command (overrides "
                                "the global --root; default: "
                                "$TRAJECTORY_MISSIONS_ROOT or <cwd>/.trajectory-pi)")
    subparser.add_argument("--json", action="store_true", default=False,
                           dest="sub_json",
                           help="machine-readable output (same effect in the "
                                "global or per-subcommand position)")


def _add_create_options(p: argparse.ArgumentParser, *,
                        objective_required: bool = True) -> None:
    """Mission-creation options shared by ``create`` and ``start``."""
    p.add_argument("--objective", required=objective_required)
    p.add_argument("--repo", default=None,
                   help="repository worktree (default: cwd)")
    p.add_argument("--head", default=None,
                   help="known-good baseline revision (default: none — review "
                        "baseline is explicit)")
    p.add_argument("--pi-wrapper", default=None,
                   help=f"canonical wrapper (default {adapter.DEFAULT_PI_WRAPPER})")
    p.add_argument("--model", default=None,
                   help=f"model name (default {adapter.DEFAULT_MODEL})")
    p.add_argument("--validate", default=None,
                   help="deterministic VALIDATE/CONSOLIDATE command string "
                        "(default: 'bash scripts/quality.sh'), e.g. --validate "
                        "\"bash -c 'test -f artifact'\"")
    p.add_argument("--consolidate", default=None,
                   help="CONSOLIDATE command override (default: validate)")
    p.add_argument("--gpu", action="store_true", default=False,
                   help="declare a GPU slot for model-heavy phases (admitted "
                        "only with gpu capacity evidence)")
    p.add_argument("--gpu-mem", type=int, default=None,
                   help=f"declared GPU memory bytes "
                        f"(default {_DEFAULT_GPU_MEM_BYTES})")
    p.add_argument("--time-budget", type=int, default=None)
    p.add_argument("--subrun-budget", type=int, default=None)
    p.add_argument("--repair-budget", type=int, default=None)


def _add_run_options(p: argparse.ArgumentParser) -> None:
    """Run-stage options shared by ``run``/``continue``/``resume``/``start``."""
    p.add_argument("--policy", nargs="*", default=[],
                   help="capacity evidence KEY=INT (cpu_slots, ram_bytes, "
                        "gpu_slots, gpu_mem_bytes)")
    p.add_argument("--session-subruns", type=int, default=None)


def build_parser(prog: str = "trajectory-pi-missions") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="TrajectoryOS operator CLI — production mission "
                    "orchestration (bounded, fail-closed, fresh-context "
                    "sub-runs).")
    parser.add_argument("--root", default=None,
                        help="missions root (default: $TRAJECTORY_MISSIONS_ROOT "
                             "or <cwd>/.trajectory-pi); accepted globally "
                             "(before the subcommand) or per-subcommand "
                             "(after it, where it wins)")
    parser.add_argument("--json", action="store_true", default=False,
                        help="machine-readable output (accepted globally or "
                             "per-subcommand)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="create a mission (no work launched)")
    _add_shared_flags(p)
    p.add_argument("id")
    _add_create_options(p)

    p = sub.add_parser(
        "start",
        help="one command: create (validate + persist) then run "
             "(existing mission rejected — never resumes)")
    _add_shared_flags(p)
    p.add_argument("id")
    _add_create_options(p, objective_required=False)
    _add_run_options(p)
    p.add_argument("--objective-file", default=None,
                   help="read the (bounded) objective from a UTF-8 text "
                        "file; conflicting with --objective and --spec")
    p.add_argument("--spec", default=None,
                   help="bounded declarative launch spec (strict JSON "
                        "object); explicit CLI values win, then spec, then "
                        "defaults; conflicting with --objective-file")

    for name, help_text in (
        ("run", "run/resume the mission (bounded session)"),
        ("continue", "continue a non-terminal mission"),
        ("resume", "reconstruct (never guesses) then run"),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_shared_flags(p)
        p.add_argument("id")
        _add_run_options(p)

    p = sub.add_parser("status", help="benchmark/status (same state)")
    _add_shared_flags(p)
    p.add_argument("id")

    p = sub.add_parser("evidence", help="phase evidence (proven sub-run)")
    _add_shared_flags(p)
    p.add_argument("id")
    p.add_argument("phase")

    p = sub.add_parser("summary", help="mission summary (bounded output)")
    _add_shared_flags(p)
    p.add_argument("id")

    p = sub.add_parser("reconstruct", help="deterministic reconstruction")
    _add_shared_flags(p)
    p.add_argument("id")

    p = sub.add_parser("note", help="record a bounded human intervention")
    _add_shared_flags(p)
    p.add_argument("id")
    p.add_argument("--text", required=True)

    p = sub.add_parser("list", help="all missions under the root")
    _add_shared_flags(p)
    sub.add_parser("version", help="CLI version")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (canonical exit codes; deterministic)."""
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse usage errors -> canonical code
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return EXIT_OK if code in (0, EXIT_OK) else EXIT_USAGE
    if args.command == "version":
        return _cmd_version()
    # --root / --json are accepted after the subcommand too (the natural
    # operator position).  Distinct dests keep the global flags intact; the
    # per-subcommand value wins on collision, and --json is OR-combined.
    if getattr(args, "sub_root", None) is not None:
        args.root = args.sub_root
        args.sub_root = None
    args.json = bool(args.json or getattr(args, "sub_json", False))
    handler = {
        "create": _cmd_create,
        "start": _cmd_start,
        "run": _cmd_run,
        "continue": _cmd_continue,
        "resume": _cmd_resume,
        "status": _cmd_status,
        "reconstruct": _cmd_reconstruct,
        "evidence": _cmd_evidence,
        "summary": _cmd_summary,
        "note": _cmd_note,
        "list": _cmd_list,
    }[args.command]
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
