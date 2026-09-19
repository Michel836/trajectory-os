"""M025 — persistent workspace and artifact provenance domain (pure, strict).

An *artifact* is any file a goal/mission produces or imports: generated
files, reports, datasets, models or intermediate results. Every artifact
carries explicit, durable provenance:

* the owning goal (and optional mission/phase/sub-run);
* a closed kind (``FILE`` / ``REPORT`` / ``DATASET`` / ``MODEL`` /
  ``INTERMEDIATE``);
* the content SHA-256 and size;
* a producer label and optional parent artifact identities (lineage);
* optional M014 reuse input identity and M016 goal-proof/criterion binding,
  so artifact provenance integrates with the existing evidence semantics.

Identity is content + semantic provenance (no clock), so identical artifacts
deduplicate naturally. Implicit cross-goal leakage is impossible: the store
only resolves artifacts inside the requesting goal's namespace unless an
explicit ``shared_with`` grant exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

from trajectory_os.artifacts import identity

#: Schema version of every workspace/artifact document.
SCHEMA_VERSION = 1

#: Human/machine workspace/artifact version string (additive).
ARTIFACT_VERSION = "m025.1"

# --- artifact kinds (closed set) ---------------------------------------------

AK_FILE = "FILE"
AK_REPORT = "REPORT"
AK_DATASET = "DATASET"
AK_MODEL = "MODEL"
AK_INTERMEDIATE = "INTERMEDIATE"

ARTIFACT_KINDS = frozenset({
    AK_FILE, AK_REPORT, AK_DATASET, AK_MODEL, AK_INTERMEDIATE,
})

# --- producers (closed set) ---------------------------------------------------

PRODUCER_AGENT = "agent"
PRODUCER_REVIEWER = "reviewer"
PRODUCER_RUNNER = "runner"
PRODUCER_OPERATOR = "operator"
PRODUCER_IMPORT = "import"

PRODUCERS = frozenset({
    PRODUCER_AGENT, PRODUCER_REVIEWER, PRODUCER_RUNNER, PRODUCER_OPERATOR,
    PRODUCER_IMPORT,
})

# --- stable fail-closed error codes ------------------------------------------

E_MALFORMED = "MALFORMED_ARTIFACT"
E_UNSUPPORTED_VERSION = "UNSUPPORTED_ARTIFACT_SCHEMA"
E_IDENTITY_MISMATCH = "ARTIFACT_IDENTITY_MISMATCH"
E_CROSS_GOAL_LEAK = "ARTIFACT_CROSS_GOAL_LEAK"
E_LINEAGE_CYCLE = "ARTIFACT_LINEAGE_CYCLE"
E_LINEAGE_MISSING = "ARTIFACT_LINEAGE_PARENT_MISSING"
E_CROSS_GOAL_LEAK_PARENT = "ARTIFACT_CROSS_GOAL_LEAK_PARENT"
E_CONTENT_MISMATCH = "ARTIFACT_CONTENT_MISMATCH"
E_NOT_FOUND = "ARTIFACT_NOT_FOUND"

#: Bounded limits (hard caps; no configuration may exceed them).
MAX_NAME_LEN = 256
MAX_PRODUCER_LEN = 64
MAX_PARENTS = 256
MAX_SHARED_WITH = 64
MAX_ID_LEN = 128
MAX_REF_LEN = 256

#: Maximum artifact size materialized through the workspace manager (bytes).
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024


class ArtifactError(Exception):
    """Malformed or untrusted artifact/workspace state (fail closed)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: str = "") -> NoReturn:
    raise ArtifactError(code, detail)


def _require_str(value: object, field: str, *, maximum: int,
                 optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        _fail(E_MALFORMED, f"{field} must be a non-empty string")
    if len(value) > maximum:
        _fail(E_MALFORMED, f"{field} exceeds {maximum} chars")
    return value


def _require_id_list(value: object, field: str, maximum: int) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        _fail(E_MALFORMED, f"{field} must be a list")
    if len(value) > maximum:
        _fail(E_MALFORMED, f"{field} exceeds {maximum} entries")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_ID_LEN:
            _fail(E_MALFORMED, f"{field} contains an invalid id")
        out.append(item)
    return tuple(sorted(set(out)))


@dataclass(frozen=True)
class WorkspaceRecord:
    """One persistent per-goal or per-mission workspace."""

    workspace_id: str
    goal_id: str
    mission_id: str | None
    path: str
    created_at: str
    kind: str  # "goal" | "mission"
    schema_version: int = SCHEMA_VERSION

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "goal_id": self.goal_id,
            "mission_id": self.mission_id,
            "kind": self.kind,
        }

    def compute_workspace_id(self) -> str:
        return identity.workspace_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "workspace_id": self.workspace_id,
            "path": self.path,
            "created_at": self.created_at,
        }

    @staticmethod
    def build(*, goal_id: str, mission_id: str | None, path: str,
              created_at: str, kind: str) -> WorkspaceRecord:
        _require_str(goal_id, "goal_id", maximum=MAX_ID_LEN, optional=False)
        _require_str(mission_id, "mission_id", maximum=MAX_ID_LEN)
        _require_str(path, "path", maximum=4096, optional=False)
        _require_str(created_at, "created_at", maximum=MAX_REF_LEN,
                     optional=False)
        if kind not in ("goal", "mission"):
            _fail(E_MALFORMED, f"invalid workspace kind {kind!r}")
        if kind == "mission" and mission_id is None:
            _fail(E_MALFORMED, "mission workspace requires mission_id")
        base = WorkspaceRecord(
            workspace_id="", goal_id=goal_id, mission_id=mission_id,
            path=path, created_at=created_at, kind=kind)
        return WorkspaceRecord(
            workspace_id=base.compute_workspace_id(), goal_id=goal_id,
            mission_id=mission_id, path=path, created_at=created_at,
            kind=kind)

    @classmethod
    def from_dict(cls, data: object) -> WorkspaceRecord:
        if not isinstance(data, dict):
            _fail(E_MALFORMED, f"workspace: {type(data).__name__}")
        if data.get("schema_version") != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, "workspace schema")
        record = cls.build(
            goal_id=data.get("goal_id"),  # type: ignore[arg-type]
            mission_id=data.get("mission_id"),
            path=data.get("path"),  # type: ignore[arg-type]
            created_at=data.get("created_at"),  # type: ignore[arg-type]
            kind=data.get("kind"))  # type: ignore[arg-type]
        stored = data.get("workspace_id")
        if stored is not None and stored != record.workspace_id:
            _fail(E_IDENTITY_MISMATCH, "workspace")
        return record


@dataclass(frozen=True)
class ArtifactRecord:
    """One persistent artifact with explicit provenance and lineage."""

    artifact_id: str
    goal_id: str
    mission_id: str | None
    phase_id: str | None
    subrun_id: str | None
    kind: str
    name: str
    rel_path: str
    size_bytes: int
    content_sha256: str
    producer: str
    created_at: str
    parent_ids: tuple[str, ...] = ()
    shared_with: tuple[str, ...] = ()
    reuse_input_id: str | None = None
    proof_id: str | None = None
    criterion_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "goal_id": self.goal_id,
            "mission_id": self.mission_id,
            "phase_id": self.phase_id,
            "subrun_id": self.subrun_id,
            "kind": self.kind,
            "name": self.name,
            "rel_path": self.rel_path,
            "size_bytes": self.size_bytes,
            "content_sha256": self.content_sha256,
            "producer": self.producer,
            "parent_ids": list(self.parent_ids),
        }

    def compute_artifact_id(self) -> str:
        return identity.artifact_id(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_payload(),
            "artifact_id": self.artifact_id,
            "created_at": self.created_at,
            "shared_with": list(self.shared_with),
            "reuse_input_id": self.reuse_input_id,
            "proof_id": self.proof_id,
            "criterion_id": self.criterion_id,
        }

    @staticmethod
    def build(
        *,
        goal_id: str,
        kind: str,
        name: str,
        rel_path: str,
        size_bytes: int,
        content_sha256: str,
        producer: str,
        created_at: str,
        mission_id: str | None = None,
        phase_id: str | None = None,
        subrun_id: str | None = None,
        parent_ids: tuple[str, ...] = (),
        shared_with: tuple[str, ...] = (),
        reuse_input_id: str | None = None,
        proof_id: str | None = None,
        criterion_id: str | None = None,
    ) -> ArtifactRecord:
        _require_str(goal_id, "goal_id", maximum=MAX_ID_LEN, optional=False)
        _require_str(mission_id, "mission_id", maximum=MAX_ID_LEN)
        _require_str(phase_id, "phase_id", maximum=MAX_ID_LEN)
        _require_str(subrun_id, "subrun_id", maximum=MAX_ID_LEN)
        _require_str(name, "name", maximum=MAX_NAME_LEN, optional=False)
        _require_str(rel_path, "rel_path", maximum=4096, optional=False)
        _require_str(created_at, "created_at", maximum=MAX_REF_LEN,
                     optional=False)
        _require_str(reuse_input_id, "reuse_input_id", maximum=MAX_REF_LEN)
        _require_str(proof_id, "proof_id", maximum=MAX_REF_LEN)
        _require_str(criterion_id, "criterion_id", maximum=MAX_REF_LEN)
        if kind not in ARTIFACT_KINDS:
            _fail(E_MALFORMED, f"invalid artifact kind {kind!r}")
        if producer not in PRODUCERS:
            _fail(E_MALFORMED, f"invalid producer {producer!r}")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) \
                or size_bytes < 0:
            _fail(E_MALFORMED, "size_bytes must be an integer >= 0")
        if not isinstance(content_sha256, str) \
                or len(content_sha256) != 64 \
                or any(ch not in "0123456789abcdef" for ch in content_sha256):
            _fail(E_MALFORMED, "content_sha256 must be 64 lowercase hex chars")
        parents = tuple(sorted(set(parent_ids)))
        if any(not pid or len(pid) > MAX_ID_LEN for pid in parents):
            _fail(E_MALFORMED, "parent_ids contains an invalid id")
        if len(parents) > MAX_PARENTS:
            _fail(E_MALFORMED, "too many parent_ids")
        shared = tuple(sorted(set(shared_with)))
        if len(shared) > MAX_SHARED_WITH:
            _fail(E_MALFORMED, "too many shared_with entries")
        for shared_goal in shared:
            if not shared_goal or len(shared_goal) > MAX_ID_LEN:
                _fail(E_MALFORMED, "shared_with contains an invalid id")
        base = ArtifactRecord(
            artifact_id="", goal_id=goal_id, mission_id=mission_id,
            phase_id=phase_id, subrun_id=subrun_id, kind=kind, name=name,
            rel_path=rel_path, size_bytes=size_bytes,
            content_sha256=content_sha256, producer=producer,
            created_at=created_at, parent_ids=parents, shared_with=shared,
            reuse_input_id=reuse_input_id, proof_id=proof_id,
            criterion_id=criterion_id)
        return ArtifactRecord(
            artifact_id=base.compute_artifact_id(), goal_id=goal_id,
            mission_id=mission_id, phase_id=phase_id, subrun_id=subrun_id,
            kind=kind, name=name, rel_path=rel_path, size_bytes=size_bytes,
            content_sha256=content_sha256, producer=producer,
            created_at=created_at, parent_ids=parents, shared_with=shared,
            reuse_input_id=reuse_input_id, proof_id=proof_id,
            criterion_id=criterion_id)

    @classmethod
    def from_dict(cls, data: object) -> ArtifactRecord:
        if not isinstance(data, dict):
            _fail(E_MALFORMED, f"artifact: {type(data).__name__}")
        if data.get("schema_version") != SCHEMA_VERSION:
            _fail(E_UNSUPPORTED_VERSION, "artifact schema")
        record = cls.build(
            goal_id=data.get("goal_id"),  # type: ignore[arg-type]
            mission_id=data.get("mission_id"),
            phase_id=data.get("phase_id"),
            subrun_id=data.get("subrun_id"),
            kind=data.get("kind"),  # type: ignore[arg-type]
            name=data.get("name"),  # type: ignore[arg-type]
            rel_path=data.get("rel_path"),  # type: ignore[arg-type]
            size_bytes=data.get("size_bytes"),  # type: ignore[arg-type]
            content_sha256=data.get("content_sha256"),  # type: ignore[arg-type]
            producer=data.get("producer"),  # type: ignore[arg-type]
            created_at=data.get("created_at"),  # type: ignore[arg-type]
            parent_ids=tuple(data.get("parent_ids") or ()),
            shared_with=tuple(data.get("shared_with") or ()),
            reuse_input_id=data.get("reuse_input_id"),
            proof_id=data.get("proof_id"),
            criterion_id=data.get("criterion_id"),
        )
        stored = data.get("artifact_id")
        if stored is not None and stored != record.artifact_id:
            _fail(E_IDENTITY_MISMATCH, "artifact")
        return record
