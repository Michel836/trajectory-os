"""M031 — read-only starting-baseline capture (never a Git trust write).

The mission baseline is captured before any phase runs and is preserved
verbatim in ``mission.json`` and ``closure.json``. It combines:

* the repository revision when the workspace is a Git checkout (read-only
  ``git rev-parse HEAD`` through the M029 helper), and
* a deterministic content digest of the isolated workspace (the M029
  Git-free semantic snapshot), which is always available.

No Git trust-boundary write (commit/push/merge/reset/restore/clean/stash/
rebase/checkout/switch) is ever issued here.
"""

from __future__ import annotations

from collections.abc import Callable

from trajectory_os.assembly import model
from trajectory_os.benchmark import patch as bench_patch
from trajectory_os.benchmark.engine import detect_repository_revision

#: Reason recorded when the workspace is not a readable Git checkout.
REASON_NO_REVISION = "workspace is not a readable Git checkout"


def capture_baseline(workspace: str, *,
                     clock: Callable[[], str] = model.utc_now,
                     ) -> model.MissionBaseline:
    """Capture the exact starting baseline (read-only, fail soft on Git)."""
    snapshot = bench_patch.capture(workspace)
    revision = detect_repository_revision(workspace)
    return model.MissionBaseline(
        revision=revision,
        workspace_digest=snapshot.digest(),
        captured_at=clock(),
        reason="" if revision else REASON_NO_REVISION,
    ).validate()


__all__ = ["REASON_NO_REVISION", "capture_baseline"]
