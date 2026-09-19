"""``trajectory_os.release`` — M036–M039 human-gated release bundle.

A thin, operator-authorized release layer laid on top of the assembled
mission flow (M030–M035). It consumes the canonical mission evidence
(``status.json`` / ``closure.json`` / ``events.jsonl``) and adds:

* **M036** — a deterministic GO COMMIT handoff and an explicitly authorized
  commit/push;
* **M037** — exactly-one PR binding and exact-head CI watch;
* **M038** — an explicit GO MERGE gate with expected-head protection
  (default method ``squash``);
* **M039** — durable release closure and read-only reconstruction.

Trust boundary: implementation/review/runtime-control components never
commit, push or merge. Only the release commands in this package may, and
only after an explicit operator authorization
(:mod:`trajectory_os.release.authorization`). The semantic patch identity
remains authoritative end to end.
"""

from trajectory_os.release.model import (
    RELEASE_VERSION,
    SCHEMA_VERSION,
    CiRun,
    CiStatus,
    CommitHandoff,
    CommitResult,
    MergeHandoff,
    MergeResult,
    PullRequestBinding,
    ReleaseClosure,
    ReleaseError,
    ReleaseState,
    ReviewGateEvidence,
)

__all__ = [
    "RELEASE_VERSION",
    "SCHEMA_VERSION",
    "CiRun",
    "CiStatus",
    "CommitHandoff",
    "CommitResult",
    "MergeHandoff",
    "MergeResult",
    "PullRequestBinding",
    "ReleaseClosure",
    "ReleaseError",
    "ReleaseState",
    "ReviewGateEvidence",
]
