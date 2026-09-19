"""M036–M039 — strict GitHub adapter for PR, exact-head CI and merge.

The release layer never talks to GitHub directly. It depends on the
:class:`GitHubAdapter` abstraction so that:

* deterministic fixtures prove the whole PR/CI/merge contract in tests and in
  CI without any credentials, network or repository mutation;
* a real ``gh``-CLI implementation provides the separate, optional GitHub
  dogfood evidence.

The adapter is deliberately narrow: discover/create exactly one PR, read the
exact PR head, read the exact-head CI checks, verify a target branch head, and
merge with expected-head protection. There is no background watcher and no
auto-merge anywhere in this module.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from trajectory_os.release import model

GH_TIMEOUT_S = 60

_PR_JSON_FIELDS = (
    "number,url,baseRefName,baseRefOid,headRefName,headRefOid,state,"
    "mergeable,mergeStateStatus,title,body,mergedAt,mergeCommit"
)


@dataclass(frozen=True)
class PullRequestInfo:
    """One pull request as observed from the GitHub adapter (never guessed)."""

    number: int
    url: str | None
    base_branch: str
    base_sha: str | None
    head_branch: str
    head_sha: str
    state: str
    mergeable: bool | None
    mergeable_state: str | None
    title: str = ""
    body: str = ""
    merge_commit_sha: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "url": self.url,
            "base_branch": self.base_branch,
            "base_sha": self.base_sha,
            "head_branch": self.head_branch,
            "head_sha": self.head_sha,
            "state": self.state,
            "mergeable": self.mergeable,
            "mergeable_state": self.mergeable_state,
            "title": self.title,
            "merge_commit_sha": self.merge_commit_sha,
        }


@dataclass(frozen=True)
class CiQueryResult:
    """One exact-head CI lookup (the head SHA is always the query key)."""

    head_sha: str
    runs: tuple[model.CiRun, ...]
    source: str
    missing_reason: str | None = None

    @property
    def state(self) -> str:
        return model.combine_ci_states([run.state for run in self.runs])

    def to_status(self, *, checked_at: str) -> model.CiStatus:
        return model.CiStatus(
            state=self.state, head_sha=self.head_sha, source=self.source,
            runs=self.runs, checked_at=checked_at,
            missing_reason=self.missing_reason).validate()


@dataclass(frozen=True)
class MergeOutcome:
    """The raw merge response (the service re-verifies it fail closed)."""

    merged: bool
    merge_sha: str | None
    message: str
    target_branch: str
    pr_number: int

    def to_dict(self) -> dict[str, object]:
        return {
            "merged": self.merged,
            "merge_sha": self.merge_sha,
            "message": self.message,
            "target_branch": self.target_branch,
            "pr_number": self.pr_number,
        }


class GitHubAdapter(Protocol):
    """Strict GitHub abstraction (one PR, exact-head CI, protected merge)."""

    def available(self) -> bool: ...

    def find_pull_requests(self, *, head_branch: str,
                           base_branch: str) -> tuple[PullRequestInfo, ...]: ...

    def create_pull_request(self, *, head_branch: str, base_branch: str,
                            title: str,
                            body: str) -> PullRequestInfo: ...

    def get_pull_request(self, number: int) -> PullRequestInfo: ...

    def checks_for_head(self, head_sha: str) -> CiQueryResult: ...

    def branch_head(self, branch: str) -> str | None: ...

    def merge_pull_request(self, number: int, *, expected_head_sha: str,
                           method: str) -> MergeOutcome: ...


def _map_mergeable(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value == "MERGEABLE":
        return True
    if value in ("CONFLICTING", "UNKNOWN"):
        return False
    return None


def _pull_request_from_payload(data: Mapping[str, Any]) -> PullRequestInfo:
    state = str(data.get("state", "")).upper()
    return PullRequestInfo(
        number=int(data.get("number", 0)),
        url=_opt_str(data.get("url")),
        base_branch=str(data.get("baseRefName", "")),
        base_sha=_opt_str(data.get("baseRefOid")),
        head_branch=str(data.get("headRefName", "")),
        head_sha=str(data.get("headRefOid", "")),
        state=state,
        mergeable=_map_mergeable(data.get("mergeable")),
        mergeable_state=_opt_str(data.get("mergeStateStatus")),
        title=str(data.get("title", "")),
        body=str(data.get("body", "")),
        merge_commit_sha=_opt_str(data.get("mergeCommit")),
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# --- deterministic fixture ----------------------------------------------------


@dataclass
class FixtureGitHubAdapter:
    """Deterministic GitHub adapter for tests and the acceptance matrix."""

    prs: list[PullRequestInfo] = field(default_factory=list)
    checks: dict[str, tuple[model.CiRun, ...]] = field(default_factory=dict)
    branch_heads: dict[str, str] = field(default_factory=dict)
    merged: list[tuple[int, str, str]] = field(default_factory=list)
    _next_number: int = 1
    is_available: bool = True

    def available(self) -> bool:
        return self.is_available

    def find_pull_requests(self, *, head_branch: str,
                           base_branch: str) -> tuple[PullRequestInfo, ...]:
        return tuple(
            pr for pr in self.prs
            if pr.head_branch == head_branch
            and pr.base_branch == base_branch
            and pr.state in (model.PR_OPEN,))

    def create_pull_request(self, *, head_branch: str, base_branch: str,
                            title: str,
                            body: str) -> PullRequestInfo:
        number = self._next_number
        self._next_number += 1
        head_sha = self.branch_heads.get(head_branch, "")
        base_sha = self.branch_heads.get(base_branch)
        pr = PullRequestInfo(
            number=number, url=f"https://example.invalid/pull/{number}",
            base_branch=base_branch, base_sha=base_sha,
            head_branch=head_branch, head_sha=head_sha, state=model.PR_OPEN,
            mergeable=True, mergeable_state="CLEAN", title=title, body=body)
        self.prs.append(pr)
        return pr

    def get_pull_request(self, number: int) -> PullRequestInfo:
        for pr in self.prs:
            if pr.number == number:
                return pr
        raise model.ReleaseError(model.R_PR_MISSING, str(number))

    def checks_for_head(self, head_sha: str) -> CiQueryResult:
        runs = self.checks.get(head_sha)
        if runs is None:
            return CiQueryResult(
                head_sha=head_sha, runs=(), source="fixture",
                missing_reason="no CI runs recorded for this exact head")
        return CiQueryResult(head_sha=head_sha, runs=runs, source="fixture")

    def branch_head(self, branch: str) -> str | None:
        return self.branch_heads.get(branch)

    def merge_pull_request(self, number: int, *, expected_head_sha: str,
                           method: str) -> MergeOutcome:
        pr = self.get_pull_request(number)
        if pr.head_sha != expected_head_sha:
            return MergeOutcome(
                merged=False, merge_sha=None,
                message="expected head does not match PR head",
                target_branch=pr.base_branch, pr_number=number)
        merge_sha = _fake_sha(f"merge:{number}:{expected_head_sha}:{method}")
        updated = PullRequestInfo(
            number=pr.number, url=pr.url, base_branch=pr.base_branch,
            base_sha=pr.base_sha, head_branch=pr.head_branch,
            head_sha=pr.head_sha, state=model.PR_MERGED,
            mergeable=True, mergeable_state="CLEAN", title=pr.title,
            body=pr.body, merge_commit_sha=merge_sha)
        self.prs[self.prs.index(pr)] = updated
        self.branch_heads[pr.base_branch] = merge_sha
        self.merged.append((number, expected_head_sha, merge_sha))
        return MergeOutcome(merged=True, merge_sha=merge_sha,
                            message="merged", target_branch=pr.base_branch,
                            pr_number=number)

    # -- test helpers --

    def add_pull_request(self, *, head_branch: str, base_branch: str,
                         number: int | None = None,
                         head_sha: str | None = None,
                         state: str = model.PR_OPEN) -> PullRequestInfo:
        number = number if number is not None else self._next_number
        self._next_number = max(self._next_number, number + 1)
        pr = PullRequestInfo(
            number=number, url=f"https://example.invalid/pull/{number}",
            base_branch=base_branch,
            base_sha=self.branch_heads.get(base_branch),
            head_branch=head_branch,
            head_sha=head_sha or self.branch_heads.get(head_branch, ""),
            state=state, mergeable=True, mergeable_state="CLEAN")
        self.prs.append(pr)
        return pr

    def set_checks(self, head_sha: str,
                   runs: Sequence[model.CiRun]) -> None:
        self.checks[head_sha] = tuple(runs)


def _fake_sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(
        b"trajectory-os.release.fixture\x00" + text.encode("utf-8")
    ).hexdigest()[:40]


# --- real gh-CLI implementation ----------------------------------------------


@dataclass
class GhCliGitHubAdapter:
    """Real GitHub adapter backed by the authenticated ``gh`` CLI."""

    repo: str = "."
    timeout_s: int = GH_TIMEOUT_S
    _slug: str | None = None

    def _gh(self, args: Sequence[str], *,
            check: bool = True) -> subprocess.CompletedProcess[str]:
        if shutil.which("gh") is None:
            raise model.ReleaseError(model.R_ADAPTER_UNAVAILABLE,
                                     "gh executable not found")
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["gh", *args], cwd=self.repo, capture_output=True,
                text=True, timeout=self.timeout_s, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise model.ReleaseError(
                model.R_ADAPTER_UNAVAILABLE,
                f"gh unavailable: {type(exc).__name__}") from exc
        if check and proc.returncode != 0:
            raise model.ReleaseError(
                model.R_ADAPTER_UNAVAILABLE,
                f"gh {' '.join(args[:2])} failed: "
                f"{proc.stderr.strip()[:256]}")
        return proc

    def available(self) -> bool:
        if shutil.which("gh") is None:
            return False
        proc = self._gh(["auth", "status"], check=False)
        return proc.returncode == 0

    def slug(self) -> str:
        if self._slug is None:
            proc = self._gh(["repo", "view", "--json", "nameWithOwner",
                             "--jq", ".nameWithOwner"])
            self._slug = proc.stdout.strip()
        return self._slug

    def _pr_list(self, *, head_branch: str,
                 base_branch: str) -> list[PullRequestInfo]:
        proc = self._gh([
            "pr", "list", "--head", head_branch, "--base", base_branch,
            "--state", "all", "--limit", "100", "--json", _PR_JSON_FIELDS])
        payload: Any = json.loads(proc.stdout or "[]")
        if not isinstance(payload, list):
            raise model.ReleaseError(model.R_ADAPTER_UNAVAILABLE,
                                     "unexpected gh pr list payload")
        return [_pull_request_from_payload(item) for item in payload
                if isinstance(item, Mapping)]

    def find_pull_requests(self, *, head_branch: str,
                           base_branch: str) -> tuple[PullRequestInfo, ...]:
        return tuple(self._pr_list(head_branch=head_branch,
                                   base_branch=base_branch))

    def create_pull_request(self, *, head_branch: str, base_branch: str,
                            title: str, body: str) -> PullRequestInfo:
        proc = self._gh(["pr", "create", "--head", head_branch, "--base",
                         base_branch, "--title", title, "--body", body])
        url = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        for pr in self._pr_list(head_branch=head_branch,
                                base_branch=base_branch):
            if pr.url == url or pr.head_branch == head_branch:
                return pr
        raise model.ReleaseError(model.R_PR_MISSING,
                                 "created pull request could not be re-read")

    def get_pull_request(self, number: int) -> PullRequestInfo:
        proc = self._gh(["pr", "view", str(number), "--json",
                         _PR_JSON_FIELDS])
        payload: Any = json.loads(proc.stdout)
        if not isinstance(payload, Mapping):
            raise model.ReleaseError(model.R_ADAPTER_UNAVAILABLE,
                                     "unexpected gh pr view payload")
        return _pull_request_from_payload(payload)

    def checks_for_head(self, head_sha: str) -> CiQueryResult:
        slug = self.slug()
        proc = self._gh([
            "api", "--paginate", "--slurp",
            f"repos/{slug}/commits/{head_sha}/check-runs"])
        pages: Any = json.loads(proc.stdout or "[]")
        if not isinstance(pages, list):
            raise model.ReleaseError(model.R_ADAPTER_UNAVAILABLE,
                                     "unexpected check-runs payload")
        runs: list[model.CiRun] = []
        for page in pages:
            if not isinstance(page, Mapping):
                continue
            checks = page.get("check_runs")
            if not isinstance(checks, Sequence):
                continue
            for check in checks:
                if not isinstance(check, Mapping):
                    continue
                run = _check_run_to_ci(check, head_sha)
                runs.append(run)
        if not runs:
            return CiQueryResult(
                head_sha=head_sha, runs=(), source="github",
                missing_reason="no check runs for this exact head")
        return CiQueryResult(head_sha=head_sha, runs=tuple(runs),
                             source="github")

    def branch_head(self, branch: str) -> str | None:
        slug = self.slug()
        proc = self._gh(["api", f"repos/{slug}/branches/{branch}",
                         "--jq", ".commit.sha"], check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def merge_pull_request(self, number: int, *, expected_head_sha: str,
                           method: str) -> MergeOutcome:
        slug = self.slug()
        proc = self._gh([
            "api", "-X", "PUT", f"repos/{slug}/pulls/{number}/merge",
            "-f", f"merge_method={method}",
            "-f", f"sha={expected_head_sha}"], check=False)
        payload: Any = {}
        if proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                payload = {}
        if not isinstance(payload, Mapping):
            payload = {}
        merged = bool(payload.get("merged", proc.returncode == 0))
        return MergeOutcome(
            merged=merged,
            merge_sha=_opt_str(payload.get("sha")),
            message=str(payload.get("message", proc.stderr.strip()[:256])),
            target_branch=str(payload.get("base", "")),
            pr_number=number)


def _check_run_to_ci(check: Mapping[str, Any], head_sha: str) -> model.CiRun:
    name = str(check.get("name", ""))
    status = str(check.get("status", ""))
    conclusion = _opt_str(check.get("conclusion"))
    details = _opt_str(check.get("details_url"))
    suite = check.get("check_suite")
    workflow = name
    if isinstance(suite, Mapping):
        workflow = _opt_str(suite.get("id")) or name
    run_id = str(check.get("id", ""))
    job = model.CiJob(
        name=name, status=status, conclusion=conclusion, url=details,
        log_ref=details)
    return model.CiRun(
        workflow=workflow, run_id=run_id, name=name, head_sha=head_sha,
        status=status, conclusion=conclusion, url=details, jobs=(job,))


__all__ = [
    "GH_TIMEOUT_S",
    "CiQueryResult",
    "FixtureGitHubAdapter",
    "GhCliGitHubAdapter",
    "GitHubAdapter",
    "MergeOutcome",
    "PullRequestInfo",
]
