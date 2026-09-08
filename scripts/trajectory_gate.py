#!/usr/bin/env python3
"""TrajectoryOS decision-gated local gate runner (Process V2).

Michel decides. Agents execute, self-repair, verify, and produce evidence.

This tool is strictly READ-ONLY: it never runs `git add`, commit, push,
PR creation, or merge. Consequential transitions stay behind explicit human
GO decisions (`GO COMMIT` / `GO PUSH` / `GO PR` / `GO MERGE`, or `FIX` / `STOP`).

Canonical handoff protocol: docs/development/AGENT_HANDOFF.md

Exit codes:
  0  ready for the next human decision
  10 blocked by a gate (diagnostic evidence is printed)
  2  invalid invocation
"""

from __future__ import annotations

import argparse
import enum
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

EXIT_READY = 0
EXIT_BLOCKED = 10
EXIT_USAGE = 2

DEFAULT_TESTS_CMD = "uv run pytest tests/unit"
DEFAULT_RUFF_CMD = "uv run ruff check ."
DEFAULT_MYPY_CMD = "uv run mypy src"
DEFAULT_QUALITY_CMD = "bash scripts/quality.sh"

FIX = "FIX"
STOP = "STOP"
GO_MERGE_HUMAN = "GO MERGE (human)"


class PhaseOutcome(enum.StrEnum):
    """Outcome of a single gate phase."""

    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True)
class PhaseResult:
    """Result of one gate phase (fail-fast, ordered)."""

    name: str
    outcome: PhaseOutcome
    summary: str = ""
    detail: str = ""
    suggested_decision: str = FIX


@dataclass(frozen=True)
class Phase:
    """A named read-only gate phase with an injectable runner (testable)."""

    name: str
    run: Callable[[], PhaseResult]


@dataclass(frozen=True)
class GateOptions:
    """Input for one gate run. All values are explicit; nothing is implicit."""

    repo: Path
    allow: tuple[str, ...] = ()
    expected_branch: str | None = None
    tests_cmd: str = DEFAULT_TESTS_CMD
    ruff_cmd: str = DEFAULT_RUFF_CMD
    mypy_cmd: str = DEFAULT_MYPY_CMD
    quality_cmd: str = DEFAULT_QUALITY_CMD
    quality_evidence: str | None = None
    evidence_file: Path | None = None


@dataclass(frozen=True)
class GateReport:
    """Immutable, deterministic result of one gate run."""

    command: str
    ready_state: str
    success_decision: str
    repo: Path
    branch: str
    head: str
    base: str
    results: tuple[PhaseResult, ...]

    @property
    def failed(self) -> PhaseResult | None:
        for result in self.results:
            if result.outcome is PhaseOutcome.FAIL:
                return result
        return None

    @property
    def blocked(self) -> bool:
        return self.failed is not None


COMMAND_SPECS: dict[str, tuple[str, str]] = {
    "implement": ("READY_FOR_COMMIT", "GO COMMIT"),
    "commit-check": ("READY_FOR_COMMIT", "GO COMMIT"),
    "push-check": ("READY_FOR_PUSH", "GO PUSH"),
    "pr-check": ("READY_FOR_PR", "GO PR"),
    "merge-check": ("READY_FOR_MERGE", GO_MERGE_HUMAN),
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(message or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def _git_rc(repo: Path, *args: str) -> int:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    return proc.returncode


def _local_ref_exists(repo: Path, ref: str) -> bool:
    return _git_rc(repo, "rev-parse", "--verify", "--quiet", ref) == 0


def repo_branch(repo: Path) -> str:
    try:
        return _git(repo, "symbolic-ref", "--short", "HEAD")
    except RuntimeError:
        return "(detached)"


def repo_head(repo: Path) -> str:
    try:
        return _git(repo, "rev-parse", "HEAD")
    except RuntimeError:
        return "(none)"


def repo_base(repo: Path) -> str:
    if _git_rc(repo, "rev-parse", "--verify", "--quiet", "refs/heads/main") != 0:
        return ""
    try:
        return _git(repo, "merge-base", "main", "HEAD")
    except RuntimeError:
        return ""


_STATUS_FIRST = set("MADRCU?! ")
_STATUS_SECOND = set("MADRCU?! ")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _split_dirty_records(raw: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain=v1 -z` output into (status, path) pairs.

    Bounded parser: NUL-delimited records of the form `XY<space>path`. For
    rename/copy, git puts the destination as the record path and emits the
    source as a bare NUL-delimited chunk without a status prefix; such
    continuation chunks are skipped (the destination is the relevant path).
    Paths with spaces, slashes, or other odd characters survive verbatim.
    """
    records: list[tuple[str, str]] = []
    for chunk in raw.split("\0"):
        if (
            len(chunk) >= 4
            and chunk[0] in _STATUS_FIRST
            and chunk[1] in _STATUS_SECOND
            and chunk[2] == " "
            and chunk[3:]
        ):
            records.append((chunk[:2], chunk[3:]))
    return records


def dirty_records(repo: Path) -> list[tuple[str, str]]:
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(message or "git status --porcelain=v1 -z failed")
    return _split_dirty_records(proc.stdout)


def dirty_files(repo: Path) -> list[str]:
    return [path for _, path in dirty_records(repo)]


def _exact_sha_match(actual: str, candidate: str) -> bool:
    """True only when both are full 40-hex commit IDs naming the same commit.

    Short prefixes, malformed values, or wrong SHAs never validate evidence;
    this is intentionally stricter than SHA abbreviation resolution.
    """
    a = actual.strip()
    c = candidate.strip()
    return bool(_HEX40.fullmatch(a)) and a.lower() == c.lower()


def _is_allowed(pattern: str, path: str) -> bool:
    p = pattern.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    return p != "" and (path == p or path.startswith(p + "/"))


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _tail(text: str, limit: int = 30) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= limit:
        return text.strip()
    return "\n".join(lines[-limit:])


def branch_phase(options: GateOptions) -> Phase:
    def run() -> PhaseResult:
        branch = repo_branch(options.repo)
        if branch == "main":
            return PhaseResult(
                name="branch",
                outcome=PhaseOutcome.FAIL,
                summary="wrong branch: main is protected; work happens on a feature branch",
                suggested_decision=STOP,
            )
        if options.expected_branch is not None and branch != options.expected_branch:
            return PhaseResult(
                name="branch",
                outcome=PhaseOutcome.FAIL,
                summary=f"expected branch {options.expected_branch!r}, got {branch!r}",
                suggested_decision=STOP,
            )
        return PhaseResult(name="branch", outcome=PhaseOutcome.PASS, summary=branch)

    return Phase("branch", run)


def ancestry_phase(options: GateOptions) -> Phase:
    def run() -> PhaseResult:
        repo = options.repo
        if _git_rc(repo, "rev-parse", "--verify", "--quiet", "refs/heads/main") != 0:
            return PhaseResult(
                name="ancestry",
                outcome=PhaseOutcome.FAIL,
                summary="main ref not found in this repository",
                suggested_decision=STOP,
            )
        if _git_rc(repo, "merge-base", "--is-ancestor", "main", "HEAD") != 0:
            return PhaseResult(
                name="ancestry",
                outcome=PhaseOutcome.FAIL,
                summary="main is not an ancestor of HEAD (branch/base mismatch)",
                suggested_decision=STOP,
            )
        return PhaseResult(name="ancestry", outcome=PhaseOutcome.PASS, summary="OK")

    return Phase("ancestry", run)


def scope_phase(options: GateOptions) -> Phase:
    def run() -> PhaseResult:
        dirty = dirty_files(options.repo)
        if not dirty:
            return PhaseResult(
                name="scope",
                outcome=PhaseOutcome.PASS,
                summary="EXPECTED (worktree clean)",
            )
        if not options.allow:
            return PhaseResult(
                name="scope",
                outcome=PhaseOutcome.FAIL,
                summary=f"UNEXPECTED: {len(dirty)} changed file(s) with no authorized scope",
                detail="\n".join(dirty) + "\n(pass --allow <path> for each in-scope file)",
                suggested_decision=STOP,
            )
        unexpected = [f for f in dirty if not any(_is_allowed(p, f) for p in options.allow)]
        if unexpected:
            return PhaseResult(
                name="scope",
                outcome=PhaseOutcome.FAIL,
                summary=f"UNEXPECTED: {len(unexpected)} file(s) outside authorized scope",
                detail="\n".join(f"- {item}" for item in unexpected),
                suggested_decision=STOP,
            )
        return PhaseResult(
            name="scope",
            outcome=PhaseOutcome.PASS,
            summary=f"EXPECTED ({len(dirty)} file(s) in scope)",
        )

    return Phase("scope", run)


def clean_phase(options: GateOptions) -> Phase:
    def run() -> PhaseResult:
        dirty = dirty_files(options.repo)
        if dirty:
            return PhaseResult(
                name="worktree",
                outcome=PhaseOutcome.FAIL,
                summary=f"DIRTY: {len(dirty)} uncommitted file(s); commit only after GO COMMIT",
                detail="\n".join(dirty),
                suggested_decision=STOP,
            )
        return PhaseResult(name="worktree", outcome=PhaseOutcome.PASS, summary="CLEAN")

    return Phase("worktree", run)


def nonempty_diff_phase(options: GateOptions) -> Phase:
    def run() -> PhaseResult:
        repo = options.repo
        if _git_rc(repo, "rev-parse", "--verify", "--quiet", "refs/heads/main") == 0:
            try:
                ahead = int(_git(repo, "rev-list", "--count", "main..HEAD"))
            except (RuntimeError, ValueError):
                ahead = 0
            if ahead > 0:
                summary = f"{ahead} commit(s) ahead of main"
                return PhaseResult(
                    name="diff", outcome=PhaseOutcome.PASS, summary=summary
                )
            if dirty_files(repo):
                summary = "uncommitted in-scope work present"
                return PhaseResult(
                    name="diff", outcome=PhaseOutcome.PASS, summary=summary
                )
        return PhaseResult(
            name="diff",
            outcome=PhaseOutcome.FAIL,
            summary="no intended diff against main (nothing committed or modified)",
            suggested_decision=STOP,
        )

    return Phase("diff", run)


def diff_check_phase(options: GateOptions) -> Phase:
    def run() -> PhaseResult:
        for args in (["diff", "--check"], ["diff", "--cached", "--check"]):
            proc = subprocess.run(
                ["git", *args], cwd=options.repo, capture_output=True, text=True, check=False
            )
            if proc.returncode != 0:
                return PhaseResult(
                    name="diff_check",
                    outcome=PhaseOutcome.FAIL,
                    summary="whitespace or control errors in diff",
                    detail=_tail(proc.stdout + "\n" + proc.stderr),
                    suggested_decision=FIX,
                )
        return PhaseResult(name="diff_check", outcome=PhaseOutcome.PASS, summary="OK")

    return Phase("diff_check", run)


def command_phase(name: str, command: str, repo: Path) -> Phase:
    def run() -> PhaseResult:
        proc = subprocess.run(
            shlex.split(command), cwd=repo, capture_output=True, text=True, check=False
        )
        ok = proc.returncode == 0
        stream = proc.stdout if ok else (proc.stderr or proc.stdout)
        summary = _last_line(stream)
        if not summary:
            summary = "OK" if ok else f"exit code {proc.returncode}"
        detail = "" if ok else _tail(proc.stdout + "\n" + proc.stderr)
        return PhaseResult(
            name=name,
            outcome=PhaseOutcome.PASS if ok else PhaseOutcome.FAIL,
            summary=summary,
            detail=detail,
            suggested_decision=FIX,
        )

    return Phase(name, run)


def quality_phase(options: GateOptions) -> Phase:
    if options.quality_evidence is None:
        return command_phase("quality_gate", options.quality_cmd, options.repo)

    evidence = options.quality_evidence

    def run() -> PhaseResult:
        head = repo_head(options.repo)
        if _exact_sha_match(head, evidence):
            return PhaseResult(
                name="quality_gate",
                outcome=PhaseOutcome.PASS,
                summary=f"reused full quality evidence @ {evidence.strip()}",
            )
        return PhaseResult(
            name="quality_gate",
            outcome=PhaseOutcome.FAIL,
            summary=(
                f"cached quality evidence {evidence.strip()[:12]} does not exactly match "
                f"HEAD {head[:12]}; an exact full 40-char commit ID of the current HEAD is required"
            ),
            suggested_decision=STOP,
        )

    return Phase("quality_gate", run)


def upstream_phase(options: GateOptions) -> Phase:
    def run() -> PhaseResult:
        repo = options.repo
        branch = repo_branch(repo)
        if not _local_ref_exists(repo, f"refs/heads/{branch}"):
            return PhaseResult(
                name="remote_head",
                outcome=PhaseOutcome.FAIL,
                summary=f"no local branch {branch} to push",
                suggested_decision=STOP,
            )
        if _git_rc(repo, "remote", "get-url", "origin") != 0:
            return PhaseResult(
                name="remote_head",
                outcome=PhaseOutcome.FAIL,
                summary="no origin remote configured; nothing to push to",
                suggested_decision=STOP,
            )
        # Authoritative read-only remote query. The absence of a local
        # refs/remotes/origin/<branch> tracking ref says nothing about the
        # remote; only `git ls-remote` is authoritative here.
        # Read-only: no fetch, no push, no update-ref, no remote mutation.
        try:
            listing = _git(
                repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
            )
        except RuntimeError as exc:
            return PhaseResult(
                name="remote_head",
                outcome=PhaseOutcome.FAIL,
                summary=(
                    f"git ls-remote failed; remote state for origin/{branch} cannot be verified"
                ),
                detail=f"git ls-remote --heads origin refs/heads/{branch}: {exc}",
                suggested_decision=STOP,
            )
        wanted = f"refs/heads/{branch}"
        remote_sha = ""
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == wanted:
                remote_sha = parts[0]
                break
        if not remote_sha:
            # Remote branch absent per authoritative query: nothing on origin
            # can diverge from local, so an initial plain push is safe to ready.
            summary = (
                f"initial push: origin/{branch} absent on remote "
                "(authoritative ls-remote; no remote history to reconcile)"
            )
            try:
                ahead = int(_git(repo, "rev-list", "--count", "main..HEAD"))
                if ahead > 0:
                    summary += f"; {ahead} commit(s) to push"
            except (RuntimeError, ValueError):
                pass
            return PhaseResult(
                name="remote_head",
                outcome=PhaseOutcome.PASS,
                summary=summary,
            )
        # Remote branch exists. A plain non-force push is safe only when the
        # remote commit is in local history and is an ancestor of HEAD
        # (i.e. a fast-forward). Anything else is diverged/advanced.
        if _git_rc(repo, "rev-parse", "--verify", "--quiet", f"{remote_sha}^{{commit}}") != 0:
            return PhaseResult(
                name="remote_head",
                outcome=PhaseOutcome.FAIL,
                summary=(
                    f"origin/{branch} @ {remote_sha[:12]} is not in local history; "
                    "a plain non-force push from this worktree is not safe"
                ),
                suggested_decision=STOP,
            )
        if _git_rc(repo, "merge-base", "--is-ancestor", remote_sha, "HEAD") != 0:
            return PhaseResult(
                name="remote_head",
                outcome=PhaseOutcome.FAIL,
                summary=(
                    f"origin/{branch} @ {remote_sha[:12]} advanced beyond local HEAD "
                    "(or diverged); a plain non-force push is not safe"
                ),
                suggested_decision=STOP,
            )
        try:
            ahead = int(_git(repo, "rev-list", "--count", f"{remote_sha}..HEAD"))
        except (RuntimeError, ValueError):
            ahead = 0
        return PhaseResult(
            name="remote_head",
            outcome=PhaseOutcome.PASS,
            summary=(
                f"origin/{branch} @ {remote_sha[:12]} (authoritative ls-remote); "
                f"{ahead} commit(s) to push"
            ),
        )

    return Phase("remote_head", run)


def external_evidence_phase(options: GateOptions) -> Phase:
    def run() -> PhaseResult:
        name = "external_evidence"
        if options.evidence_file is None:
            return PhaseResult(
                name=name,
                outcome=PhaseOutcome.FAIL,
                summary="no --evidence JSON supplied for GitHub CI/review/mergeability state",
                detail="collect evidence from GitHub (head, ci, reviews_resolved, mergeable), "
                "then rerun: python scripts/trajectory_gate.py merge-check --evidence <file.json>",
                suggested_decision=GO_MERGE_HUMAN,
            )
        try:
            data = json.loads(Path(options.evidence_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return PhaseResult(
                name=name,
                outcome=PhaseOutcome.FAIL,
                summary=f"failed to read evidence file: {exc}",
                suggested_decision=STOP,
            )
        if not isinstance(data, dict):
            return PhaseResult(
                name=name, outcome=PhaseOutcome.FAIL,
                summary="evidence file must be a JSON object", suggested_decision=STOP,
            )
        head = repo_head(options.repo)
        evidence_head = str(data.get("head", ""))
        if not evidence_head:
            return PhaseResult(
                name=name, outcome=PhaseOutcome.FAIL,
                summary="evidence missing 'head'; cannot verify GitHub state targets current HEAD",
                suggested_decision=STOP,
            )
        if not _exact_sha_match(head, evidence_head):
            summary = (
                f"evidence head {evidence_head[:12]} does not exactly match current HEAD "
                f"{head[:12]}; an exact full 40-char commit ID is required"
            )
            return PhaseResult(
                name=name, outcome=PhaseOutcome.FAIL,
                summary=summary, suggested_decision=STOP,
            )
        if str(data.get("ci", "")) != "green":
            return PhaseResult(
                name=name, outcome=PhaseOutcome.FAIL,
                summary="GitHub CI is not green", suggested_decision=FIX,
            )
        if data.get("reviews_resolved") is not True:
            return PhaseResult(
                name=name, outcome=PhaseOutcome.FAIL,
                summary="review threads are not all resolved", suggested_decision=FIX,
            )
        if data.get("mergeable") is not True:
            return PhaseResult(
                name=name, outcome=PhaseOutcome.FAIL,
                summary="PR is not mergeable (conflicts or state)", suggested_decision=FIX,
            )
        summary = (
            f"head-match OK; CI green; reviews resolved; mergeable (target {evidence_head[:12]})"
        )
        return PhaseResult(
            name=name, outcome=PhaseOutcome.PASS,
            summary=summary,
        )

    return Phase("external_evidence", run)


def default_phases(command: str, options: GateOptions) -> list[Phase]:
    commands = {
        "implement": (
            branch_phase(options),
            scope_phase(options),
            command_phase("focused_tests", options.tests_cmd, options.repo),
            command_phase("ruff", options.ruff_cmd, options.repo),
            command_phase("mypy", options.mypy_cmd, options.repo),
            diff_check_phase(options),
        ),
        "commit-check": (
            branch_phase(options),
            ancestry_phase(options),
            scope_phase(options),
            nonempty_diff_phase(options),
            command_phase("focused_tests", options.tests_cmd, options.repo),
            command_phase("ruff", options.ruff_cmd, options.repo),
            command_phase("mypy", options.mypy_cmd, options.repo),
            diff_check_phase(options),
        ),
        "push-check": (
            branch_phase(options),
            ancestry_phase(options),
            clean_phase(options),
            upstream_phase(options),
        ),
        "pr-check": (
            branch_phase(options),
            ancestry_phase(options),
            clean_phase(options),
            nonempty_diff_phase(options),
            quality_phase(options),
            diff_summary_phase(options),
        ),
        "merge-check": (
            branch_phase(options),
            ancestry_phase(options),
            clean_phase(options),
            external_evidence_phase(options),
        ),
    }
    if command not in commands:
        raise ValueError(f"unknown gate command: {command!r}")
    return list(commands[command])


def _diff_summary_runner(options: GateOptions) -> Callable[[], PhaseResult]:
    def run() -> PhaseResult:
        repo = options.repo
        records = dirty_records(repo)
        paths = [path for _, path in records]
        untracked = [path for status, path in records if status.startswith("??")]
        uncommitted_tracked = len(records) - len(untracked)
        detail_lines: list[str] = []
        notes: list[str] = []
        try:
            shortstat = _git(repo, "diff", "--shortstat", "main...HEAD") or "no committed diff yet"
            files = _git(repo, "diff", "--name-only", "main...HEAD").splitlines()
            detail_lines.extend(files)
        except RuntimeError:
            shortstat = "no diff against main (local work only)"
            detail_lines.extend(paths)
        if untracked:
            notes.append(
                f"+{len(untracked)} untracked file(s) NOT included in the committed diff above"
            )
            detail_lines.append("uncommitted (not in committed diff above):")
            detail_lines.extend(f"- {p}" for p in untracked)
        if uncommitted_tracked:
            notes.append(f"+{uncommitted_tracked} modified tracked file(s) uncommitted")
        summary = shortstat
        if notes:
            summary += "; INCOMPLETE WITH WORKTREE: " + "; ".join(notes)
        return PhaseResult(
            name="diff_summary",
            outcome=PhaseOutcome.PASS,
            summary=summary or "OK",
            detail="\n".join(detail_lines),
        )

    return run


def diff_summary_phase(options: GateOptions) -> Phase:
    return Phase("diff_summary", _diff_summary_runner(options))


def run_gate(
    command: str,
    options: GateOptions,
    phases: Sequence[Phase] | None = None,
) -> GateReport:
    """Run gate phases fail-fast (no later phase runs after a failure)."""
    ready_state, success_decision = COMMAND_SPECS[command]
    planned = list(phases) if phases is not None else default_phases(command, options)
    results: list[PhaseResult] = []
    for phase in planned:
        result = phase.run()
        results.append(result)
        if result.outcome is PhaseOutcome.FAIL:
            break
    return GateReport(
        command=command,
        ready_state=ready_state,
        success_decision=success_decision,
        repo=options.repo,
        branch=repo_branch(options.repo),
        head=repo_head(options.repo),
        base=repo_base(options.repo),
        results=tuple(results),
    )


def render_report(report: GateReport) -> str:
    """Render the compact, standardized handoff body (deterministic for a state)."""
    failed = report.failed
    lines = [
        f"STATE: {report.ready_state if failed is None else 'BLOCKED'}",
        f"PHASE: {report.command}",
        f"BRANCH: {report.branch}",
        f"HEAD: {report.head}",
    ]
    if report.base:
        lines.append(f"BASE: {report.base}")
    lines.append("")
    lines.append("EVIDENCE:")
    if report.results:
        for result in report.results:
            suffix = f" — {result.summary}" if result.summary else ""
            lines.append(f"- {result.name}: {result.outcome.value}{suffix}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("BLOCKERS:")
    if failed is None:
        lines.append("- None")
    else:
        lines.append(f"- {failed.name}: {failed.summary}")
        if failed.detail:
            lines.append("  detail:")
            for line in failed.detail.splitlines()[:25]:
                lines.append(f"  {line}")
    lines.append("")
    lines.append("DECISION REQUIRED:")
    lines.append(failed.suggested_decision if failed is not None else report.success_decision)
    return "\n".join(lines)


def write_artifact(repo: Path, body: str) -> Path:
    """Write the local handoff artifact (evidence only, not semantic authority)."""
    path = repo / ".artifacts" / "handoff" / "latest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = (
        "<!-- TrajectoryOS gate handoff artifact: evidence only; "
        "NOT semantic authority over Git/repository state -->\n"
        f"<!-- generated_at: {stamp} -->\n\n"
    )
    path.write_text(header + body + "\n", encoding="utf-8")
    return path


def _common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo", default=".", help="git repository root (default: current directory)"
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=None,
        metavar="PATH",
        help="authorized in-scope path (repeatable; prefix match)",
    )
    parser.add_argument("--expected-branch", default=None, metavar="BRANCH")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="trajectory_gate",
        description="TrajectoryOS decision-gated local gates (read-only; never "
        "commits, pushes, opens PRs, or merges).",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    for name in ("implement", "commit-check"):
        p = sub.add_parser(name, help=f"{name} gate")
        _common_args(p)
        p.add_argument("--tests", default=DEFAULT_TESTS_CMD, help="focused test command")

    p = sub.add_parser("push-check", help="read-only push readiness gate")
    _common_args(p)

    p = sub.add_parser("pr-check", help="branch-level PR readiness gate")
    _common_args(p)
    p.add_argument("--quality-cmd", default=DEFAULT_QUALITY_CMD)
    p.add_argument(
        "--quality-evidence",
        default=None,
        metavar="SHA",
        help="reuse a valid full quality gate run at this immutable HEAD",
    )

    p = sub.add_parser("merge-check", help="merge readiness gate (evidence-gated)")
    _common_args(p)
    p.add_argument(
        "--evidence",
        default=None,
        metavar="FILE",
        help="JSON with {head, ci, reviews_resolved, mergeable} gathered from GitHub",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    command = str(args.command)
    repo = Path(str(args.repo)).resolve()
    if not (repo / ".git").exists():
        print(f"error: not a git repository: {repo}", file=sys.stderr)
        return EXIT_USAGE

    allow = tuple(str(item) for item in (args.allow or ()))
    expected_branch = args.expected_branch if isinstance(args.expected_branch, str) else None
    evidence_file = Path(getattr(args, "evidence", "")) if getattr(args, "evidence", None) else None
    options = GateOptions(
        repo=repo,
        allow=allow,
        expected_branch=expected_branch,
        tests_cmd=str(args.tests) if hasattr(args, "tests") else DEFAULT_TESTS_CMD,
        ruff_cmd=str(getattr(args, "ruff_cmd", DEFAULT_RUFF_CMD)),
        mypy_cmd=str(getattr(args, "mypy_cmd", DEFAULT_MYPY_CMD)),
        quality_cmd=str(getattr(args, "quality_cmd", DEFAULT_QUALITY_CMD)),
        quality_evidence=args.quality_evidence
        if hasattr(args, "quality_evidence") and isinstance(args.quality_evidence, str)
        else None,
        evidence_file=evidence_file,
    )

    report = run_gate(command, options)
    body = render_report(report)
    artifact = write_artifact(repo, body)
    sys.stdout.write(body + "\n")
    print(f"artifact: {artifact}")
    return EXIT_BLOCKED if report.blocked else EXIT_READY


if __name__ == "__main__":
    raise SystemExit(main())
