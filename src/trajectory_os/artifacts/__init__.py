"""``trajectory_os.artifacts`` — M025 persistent workspaces and artifacts.

Per-goal and per-mission workspaces, explicit content-addressed artifact
identity/provenance (generated files, reports, datasets, models, intermediate
results), leak-proof per-goal namespacing, and lineage integrated with the
M014 reuse / M016 goal-proof evidence semantics.

The store is product-owned state only: it performs no Git trust-boundary write.
"""

from trajectory_os.artifacts.engine import WorkspaceManager, WorkspaceState
from trajectory_os.artifacts.model import (
    AK_DATASET,
    AK_FILE,
    AK_INTERMEDIATE,
    AK_MODEL,
    AK_REPORT,
    ARTIFACT_KINDS,
    PRODUCER_AGENT,
    PRODUCER_IMPORT,
    PRODUCER_OPERATOR,
    PRODUCER_REVIEWER,
    PRODUCER_RUNNER,
    ArtifactError,
    ArtifactRecord,
    WorkspaceRecord,
)

__all__ = [
    "AK_DATASET", "AK_FILE", "AK_INTERMEDIATE", "AK_MODEL", "AK_REPORT",
    "ARTIFACT_KINDS", "PRODUCER_AGENT", "PRODUCER_IMPORT",
    "PRODUCER_OPERATOR", "PRODUCER_REVIEWER", "PRODUCER_RUNNER",
    "ArtifactError", "ArtifactRecord", "WorkspaceRecord", "WorkspaceManager",
    "WorkspaceState",
]
